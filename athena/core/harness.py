"""Harness Engine — deterministic safety rule evaluation.

Independent of LLM. Provides synchronous evaluate(action, context) →
(allow, reason) with rule types:

- blacklist: regex-based command/path deny lists
- path_boundary: enforce /workspace/ prefix for filesystem operations
- quota: concurrent task limits, daily operation caps
- cooling_off: mandatory wait periods before high-risk confirmations

Rules are stored in SQLite with hot-reload via 30s DB poll on MAX(revision)
and POST /admin/harness/reload for instant refresh.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from athena.config import Config
from athena.logging_config import get_logger
from athena.models import get_session_maker

logger = get_logger(__name__)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class HarnessResult:
    """Result of a harness rule evaluation."""
    allowed: bool = True
    reason: str | None = None
    cooling_off_seconds: int = 0
    timeout_seconds: int = 120
    requires_confirmation: bool = False
    risk_level: RiskLevel = RiskLevel.LOW
    blocked_by_rule: str | None = None


@dataclass
class HarnessAction:
    """An action to be evaluated by the harness."""
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    user_id: str = ""
    channel: str = ""
    session_id: str = ""
    task_id: str = ""


class HarnessEngine:
    """Deterministic rule evaluation engine.

    All execution paths are checked through this engine before any tool
    invocation. Rules are cached in memory and hot-reloaded.
    """

    # Built-in blacklist patterns (always active, not in DB)
    STATIC_BLACKLIST: list[tuple[str, str]] = [
        # (pattern, description)
        (r"rm\s+-rf\s+/", "Recursive root deletion"),
        (r"sudo\s+", "Privilege escalation via sudo"),
        (r"mkfs\.", "Filesystem formatting"),
        (r"dd\s+if=", "Raw disk operations"),
        (r">\s*/dev/sd", "Write to block device"),
        (r"chmod\s+777", "World-writable permissions"),
        (r"wget\s+.*\|\s*sh", "Pipe to shell from network"),
        (r"curl\s+.*\|\s*sh", "Pipe to shell from network"),
    ]

    # Sensitive paths outside /workspace
    SENSITIVE_PATHS: list[str] = [
        "/etc/", "/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/",
        "/boot/", "/dev/", "/proc/", "/sys/", "/root/",
    ]

    def __init__(self, config: Config):
        self.config = config
        self._rules: list[dict[str, Any]] = []
        self._last_revision: int = 0
        self._rules_lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load rules and start the background poll loop."""
        self._running = True
        await self.reload_rules()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("harness_engine_started", rule_count=len(self._rules))

    async def stop(self) -> None:
        """Stop the poll loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("harness_engine_stopped")

    async def _poll_loop(self) -> None:
        """Background task: poll DB every 30s for rule changes."""
        while self._running:
            await asyncio.sleep(30)
            try:
                await self._check_and_reload()
            except Exception as e:
                logger.error("harness_poll_error", error=str(e))

    async def _check_and_reload(self) -> None:
        """Check if max revision changed and reload if so."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select, func
            from athena.models.harness_rule import HarnessRule
            result = await session.execute(
                select(func.max(HarnessRule.revision))
            )
            max_rev = result.scalar_one() or 0

        if max_rev > self._last_revision:
            await self.reload_rules()

    # ── Rule management ───────────────────────────────────────────────

    async def reload_rules(self) -> None:
        """Force reload all rules from the database."""
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select
            from athena.models.harness_rule import HarnessRule
            result = await session.execute(
                select(HarnessRule).where(HarnessRule.enabled == True)  # noqa: E712
                .order_by(HarnessRule.priority.desc())
            )
            db_rules = result.scalars().all()

        rules = []
        for r in db_rules:
            rules.append({
                "rule_id": r.rule_id,
                "rule_type": r.rule_type,
                "name": r.name,
                "config": json.loads(r.config_json),
                "priority": r.priority,
                "revision": r.revision,
            })

        async with self._rules_lock:
            self._rules = rules
            self._last_revision = max(
                (r["revision"] for r in rules), default=0
            )

        logger.info("harness_rules_reloaded", count=len(rules))

    # ── Core evaluation ───────────────────────────────────────────────

    async def evaluate(self, action: HarnessAction) -> HarnessResult:
        """Evaluate an action against all active rules.

        Rules are evaluated in priority order (highest first). The first
        blocking rule result is returned.
        """
        async with self._rules_lock:
            rules = list(self._rules)

        result = HarnessResult()

        # 1. Blacklist check (static + dynamic)
        result = self._check_blacklist(action, result)
        if not result.allowed:
            return result

        # 2. Path boundary check
        result = self._check_path_boundary(action, result)
        if not result.allowed:
            return result

        # 3. Quota check
        result = await self._check_quota(action, result)
        if not result.allowed:
            return result

        # 4. Risk assessment and cooling-off
        result = self._assess_risk(action, result)

        # Apply dynamic rules from DB
        for rule in rules:
            result = self._apply_rule(rule, action, result)
            if not result.allowed:
                return result

        return result

    async def pre_check(self, tool_name: str, rendered_args: dict[str, Any]) -> HarnessResult:
        """Pre-check: validate rendered (actual runtime) arguments.

        Called after template rendering but before tool invocation.
        Checks the actual parameter values, not template variables.
        """
        action = HarnessAction(tool_name=tool_name, arguments=rendered_args)
        return await self.evaluate(action)

    def get_risk_level(
        self,
        tool_name: str,
        rendered_args: dict[str, Any] | None = None,
    ) -> RiskLevel:
        """Determine the risk level for a tool call.

        Write operations and device control are inherently higher risk.
        Read operations are low risk.
        """
        write_tools = {
            "file_write", "file_delete", "adb_shell", "adb_install",
            "adb_tap", "run_script", "simulate_keystroke",
        }
        read_tools = {
            "file_read", "web_search", "adb_screenshot", "screenshot",
        }

        if tool_name in write_tools:
            if tool_name in ("file_delete", "adb_install"):
                return RiskLevel.CRITICAL
            return RiskLevel.HIGH
        elif tool_name in read_tools:
            return RiskLevel.LOW
        return RiskLevel.MEDIUM

    def get_cooling_off(self, risk_level: RiskLevel) -> int:
        """Get cooling-off seconds for a risk level."""
        defaults = self.config.system.cooling_off_defaults
        return defaults.get(risk_level.value, 0)

    # ── Rule evaluation helpers ───────────────────────────────────────

    def _check_blacklist(
        self, action: HarnessAction, result: HarnessResult
    ) -> HarnessResult:
        """Check static blacklist patterns against tool name and arguments."""
        # Check tool name
        for pattern, description in self.STATIC_BLACKLIST:
            if re.search(pattern, action.tool_name, re.IGNORECASE):
                result.allowed = False
                result.reason = f"Blacklisted tool: {description}"
                result.blocked_by_rule = "static_blacklist"
                return result

        # Check arguments for dangerous patterns
        args_str = json.dumps(action.arguments)
        for pattern, description in self.STATIC_BLACKLIST:
            if re.search(pattern, args_str, re.IGNORECASE):
                result.allowed = False
                result.reason = f"Blacklisted argument pattern: {description}"
                result.blocked_by_rule = "static_blacklist"
                return result

        return result

    def _check_path_boundary(
        self, action: HarnessAction, result: HarnessResult
    ) -> HarnessResult:
        """Enforce /workspace/ prefix for filesystem operations."""
        if action.tool_name not in ("file_read", "file_write", "file_delete"):
            return result

        path = action.arguments.get("path", "")
        if not path:
            return result

        # Normalize and check
        import os
        normalized = os.path.normpath(path)

        # Must be under /workspace/
        if not normalized.startswith("/workspace/"):
            # Check if it resolves outside workspace
            if ".." in normalized or normalized.startswith("/"):
                # Check if it's explicitly /workspace itself
                if normalized != "/workspace":
                    result.allowed = False
                    result.reason = (
                        f"Path '{path}' is outside allowed /workspace/ boundary"
                    )
                    result.blocked_by_rule = "path_boundary"
                    return result

        return result

    async def _check_quota(
        self, action: HarnessAction, result: HarnessResult
    ) -> HarnessResult:
        """Check concurrent task and daily operation limits."""
        # Concurrent tasks check
        db_path = self.config.sqlite_db_path
        session_maker = get_session_maker(db_path)

        async with session_maker() as session:
            from sqlalchemy import select, func
            from athena.models.task import Task
            result_ = await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.user_id == action.user_id,
                    Task.status.in_(["running", "recovering"]),
                )
            )
            concurrent = result_.scalar_one()

        if concurrent >= 2:
            result.allowed = False
            result.reason = f"Concurrent task limit reached ({concurrent}/2)"
            result.blocked_by_rule = "quota"
            return result

        # Global concurrent check
        global_max = self.config.system.global_max_concurrent_tasks
        async with session_maker() as session:
            from sqlalchemy import select, func
            from athena.models.task import Task
            result_ = await session.execute(
                select(func.count()).select_from(Task).where(
                    Task.status.in_(["running", "recovering"]),
                )
            )
            global_concurrent = result_.scalar_one()

        if global_concurrent >= global_max:
            result.allowed = False
            result.reason = (
                f"Global concurrent task limit reached ({global_concurrent}/{global_max})"
            )
            result.blocked_by_rule = "quota"
            return result

        return result

    def _assess_risk(
        self, action: HarnessAction, result: HarnessResult
    ) -> HarnessResult:
        """Determine risk level, cooling-off, and confirmation requirements."""
        risk_level = self.get_risk_level(action.tool_name, action.arguments)
        result.risk_level = risk_level
        result.cooling_off_seconds = self.get_cooling_off(risk_level)

        # Determine timeout based on risk
        timeout_map = {
            RiskLevel.LOW: 120,
            RiskLevel.MEDIUM: 120,
            RiskLevel.HIGH: 60,
            RiskLevel.CRITICAL: 30,
        }
        result.timeout_seconds = timeout_map.get(risk_level, 120)

        # Confirmation required for write operations and above
        if risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL):
            result.requires_confirmation = True
        elif action.tool_name not in ("file_read", "web_search", "adb_screenshot", "screenshot"):
            # Any non-read operation
            result.requires_confirmation = True

        return result

    def _apply_rule(
        self,
        rule: dict[str, Any],
        action: HarnessAction,
        result: HarnessResult,
    ) -> HarnessResult:
        """Apply a single dynamic rule from the database."""
        rule_type = rule["rule_type"]
        config = rule["config"]

        if rule_type == "blacklist":
            return self._apply_blacklist_rule(config, action, result)
        elif rule_type == "path_boundary":
            return self._apply_path_rule(config, action, result)
        elif rule_type == "quota":
            # DB quota rules augment static checks
            return result
        elif rule_type == "cooling_off":
            return self._apply_cooling_off_rule(config, action, result)

        return result

    def _apply_blacklist_rule(
        self,
        config: dict[str, Any],
        action: HarnessAction,
        result: HarnessResult,
    ) -> HarnessResult:
        """Apply a dynamic blacklist rule."""
        patterns = config.get("patterns", [])
        args_str = json.dumps(action.arguments)
        for pattern in patterns:
            if re.search(pattern, action.tool_name, re.IGNORECASE):
                result.allowed = False
                result.reason = f"Blocked by rule: matches pattern '{pattern}'"
                result.blocked_by_rule = "dynamic_blacklist"
                return result
            if re.search(pattern, args_str, re.IGNORECASE):
                result.allowed = False
                result.reason = f"Arguments blocked by rule: matches pattern '{pattern}'"
                result.blocked_by_rule = "dynamic_blacklist"
                return result
        return result

    def _apply_path_rule(
        self,
        config: dict[str, Any],
        action: HarnessAction,
        result: HarnessResult,
    ) -> HarnessResult:
        """Apply a dynamic path boundary rule."""
        allowed_prefixes = config.get("allowed_prefixes", ["/workspace/"])
        path = action.arguments.get("path", "")
        if not path:
            return result

        import os
        normalized = os.path.normpath(path)
        allowed = any(normalized.startswith(p) for p in allowed_prefixes)
        if not allowed:
            result.allowed = False
            result.reason = f"Path '{path}' not in allowed prefixes: {allowed_prefixes}"
            result.blocked_by_rule = "path_boundary"
        return result

    def _apply_cooling_off_rule(
        self,
        config: dict[str, Any],
        action: HarnessAction,
        result: HarnessResult,
    ) -> HarnessResult:
        """Apply a dynamic cooling-off rule, overriding defaults."""
        applies_to = config.get("applies_to_tools", "*")
        if applies_to != "*" and action.tool_name not in applies_to:
            return result

        channels = config.get("applies_to_channels", [])
        if channels and action.channel not in channels:
            return result

        # Override cooling-off from rule config
        override_level = config.get("risk_level", result.risk_level.value)
        result.risk_level = RiskLevel(override_level)
        result.cooling_off_seconds = config.get(
            "cooling_off_seconds",
            self.get_cooling_off(result.risk_level),
        )
        result.timeout_seconds = config.get("timeout_seconds", result.timeout_seconds)
        result.requires_confirmation = True
        return result
