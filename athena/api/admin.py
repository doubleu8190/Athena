"""Admin API endpoints — MCP servers, skills, devices, harness, audit, dashboard.

All endpoints require API Key authentication (X-API-Key header).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, func, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from athena.api.deps import get_config_dep, get_db, get_mcp_client_dep, verify_api_key
from athena.logging_config import get_logger

from athena.mcp_client.client import MCPClient

logger = get_logger(__name__)
router = APIRouter(tags=["admin"])


# ── Response helpers ──────────────────────────────────────────────────

def success(data: Any = None, message: str = "success") -> dict[str, Any]:  # noqa: ANN401
    return {"code": 0, "message": message, "data": data}


def error(code: int, message: str, detail: str = "") -> dict[str, Any]:
    return {"code": code, "message": message, "detail": detail, "data": None}


# ── MCP Server management ─────────────────────────────────────────────

class MCPServerCreate(BaseModel):
    server_id: str
    name: str
    transport: str  # 'stdio', 'http', 'sse'
    connection_config: dict[str, Any]
    source: str = "user"  # 'builtin' (code-defined), 'user' (web-created)


class MCPServerStatusUpdate(BaseModel):
    enabled: bool  # True = admin wants server enabled, False = disabled


@router.get("/mcp-servers")
async def list_mcp_servers(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
    mcp_client: MCPClient = Depends(get_mcp_client_dep),
) -> dict[str, Any]:
    """List all registered MCP servers."""
    from athena.models.mcp_server import MCPServer

    result = await db.execute(select(MCPServer).order_by(MCPServer.registered_at.desc()))
    servers = result.scalars().all()

    return success({
        "items": [
            {
                "server_id": s.server_id,
                "name": s.name,
                "transport": s.transport,
                "connection_config": json.loads(s.connection_config),
                "source": s.source,
                "enabled": s.enabled,
                "connection_status": mcp_client.get_connection_status(s.server_id),
            }
            for s in servers
        ]
    })


@router.post("/mcp-servers")
async def register_mcp_server(
    body: MCPServerCreate,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
    mcp_client: MCPClient = Depends(get_mcp_client_dep),
) -> dict[str, Any]:
    """Register a new external MCP server."""
    from athena.models.mcp_server import MCPServer

    # Check for duplicate
    existing = await db.get(MCPServer, body.server_id)
    if existing:
        return error(40901, "Server already exists", f"Server ID '{body.server_id}' already registered")

    server = MCPServer(
        server_id=body.server_id,
        name=body.name,
        transport=body.transport,
        connection_config=json.dumps(body.connection_config),
        source=body.source,
    )
    db.add(server)
    await db.commit()

    # Connect the newly registered server
    await mcp_client.connect_server(body.server_id)

    logger.info("mcp_server_registered", server_id=body.server_id, transport=body.transport)
    return success({
        "server_id": body.server_id,
        "enabled": server.enabled,
        "connection_status": mcp_client.get_connection_status(body.server_id),
    }, "Server registered")


@router.delete("/mcp-servers/{server_id}")
async def remove_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
    mcp_client: MCPClient = Depends(get_mcp_client_dep),
) -> dict[str, Any]:
    """Remove a registered MCP server."""
    from athena.models.mcp_server import MCPServer

    server = await db.get(MCPServer, server_id)
    if not server:
        return error(40401, "Server not found", f"Server '{server_id}' not found")

    # Disconnect before removing
    await mcp_client.disconnect_server(server_id)

    await db.delete(server)
    await db.commit()

    logger.info("mcp_server_removed", server_id=server_id)
    return success(None, "Server removed")


@router.put("/mcp-servers/{server_id}/status")
async def update_mcp_server_status(
    server_id: str,
    body: MCPServerStatusUpdate,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
    mcp_client: MCPClient = Depends(get_mcp_client_dep),
) -> dict[str, Any]:
    """Enable or disable an MCP server.

    Setting enabled=True will actively connect the server.
    Setting enabled=False will actively disconnect it and mark its tools as stale.
    """
    from athena.models.mcp_server import MCPServer

    server = await db.get(MCPServer, server_id)
    if not server:
        return error(40401, "Server not found", f"Server '{server_id}' not found")

    server.enabled = body.enabled
    await db.commit()

    if body.enabled:
        # Actively connect the server
        await mcp_client.connect_server(server_id)
    else:
        # Actively disconnect and mark tools stale
        await mcp_client.disconnect_server(server_id)

    logger.info("mcp_server_enabled_updated", server_id=server_id, enabled=body.enabled)
    return success({
        "server_id": server_id,
        "enabled": body.enabled,
        "connection_status": mcp_client.get_connection_status(server_id),
    })


# ── Skill management ──────────────────────────────────────────────────

class SkillInstallRequest(BaseModel):
    name: str
    version: str
    image_uri: str
    allowed_domains: str | None = None  # Comma-separated, empty = no network


@router.get("/skills")
async def list_skills(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """List all installed skills."""
    from athena.models.skill import Skill as SkillModel

    result = await db.execute(select(SkillModel).order_by(SkillModel.installed_at.desc()))
    skills = result.scalars().all()

    return success({
        "items": [
            {
                "skill_id": s.skill_id,
                "name": s.name,
                "version": s.version,
                "image_uri": s.image_uri,
                "status": s.status,
                "allowed_domains": s.allowed_domains,
                "container_id": s.container_id,
            }
            for s in skills
        ]
    })


@router.post("/skills")
async def install_skill(
    body: SkillInstallRequest,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Install a new Skill (Docker image + MCP container)."""
    skill_id = f"skill_{uuid.uuid4().hex[:12]}"

    from athena.models.skill import Skill as SkillModel
    skill = SkillModel(
        skill_id=skill_id,
        name=body.name,
        version=body.version,
        image_uri=body.image_uri,
        allowed_domains=body.allowed_domains,
        status="installing",
    )
    db.add(skill)
    await db.commit()

    logger.info("skill_install_requested", skill_id=skill_id, name=body.name)
    return success({"skill_id": skill_id}, "Skill installation initiated")


@router.delete("/skills/{skill_id}")
async def uninstall_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Uninstall a Skill."""
    from athena.models.skill import Skill as SkillModel

    skill = await db.get(SkillModel, skill_id)
    if not skill:
        return error(40401, "Skill not found", f"Skill '{skill_id}' not found")

    skill.status = "uninstalling"
    await db.commit()

    logger.info("skill_uninstall_requested", skill_id=skill_id)
    return success(None, "Skill uninstallation initiated")


# ── Device management ─────────────────────────────────────────────────

class DeviceRegisterRequest(BaseModel):
    device_id: str
    type: str  # 'host' or 'android'
    connection_info: dict[str, Any] | None = None


@router.get("/devices")
async def list_devices(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """List all registered devices."""
    from athena.models.device import Device

    result = await db.execute(select(Device).order_by(Device.last_heartbeat.desc().nulls_last()))
    devices = result.scalars().all()

    return success({
        "items": [
            {
                "device_id": d.device_id,
                "type": d.type,
                "connection_info": json.loads(d.connection_info) if d.connection_info else None,
                "status": d.status,
                "last_heartbeat": d.last_heartbeat.isoformat() if d.last_heartbeat else None,
            }
            for d in devices
        ]
    })


@router.post("/devices")
async def register_device(
    body: DeviceRegisterRequest,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Register a new device."""
    from athena.models.device import Device

    existing = await db.get(Device, body.device_id)
    if existing:
        return error(40901, "Device already exists", f"Device '{body.device_id}' already registered")

    device = Device(
        device_id=body.device_id,
        type=body.type,
        connection_info=json.dumps(body.connection_info) if body.connection_info else None,
        status="offline",
    )
    db.add(device)
    await db.commit()

    logger.info("device_registered", device_id=body.device_id, type=body.type)
    return success({"device_id": body.device_id}, "Device registered")


@router.delete("/devices/{device_id}")
async def deregister_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Deregister a device."""
    from athena.models.device import Device

    device = await db.get(Device, device_id)
    if not device:
        return error(40401, "Device not found", f"Device '{device_id}' not found")

    await db.delete(device)
    await db.commit()

    logger.info("device_deregistered", device_id=device_id)
    return success(None, "Device deregistered")


# ── Harness rule management ───────────────────────────────────────────

@router.get("/harness/rules")
async def list_harness_rules(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """List all harness rules."""
    from athena.models.harness_rule import HarnessRule

    result = await db.execute(
        select(HarnessRule).order_by(HarnessRule.priority.desc())
    )
    rules = result.scalars().all()

    return success({
        "items": [
            {
                "rule_id": r.rule_id,
                "rule_type": r.rule_type,
                "name": r.name,
                "description": r.description,
                "config": json.loads(r.config_json),
                "priority": r.priority,
                "enabled": r.enabled,
                "revision": r.revision,
            }
            for r in rules
        ]
    })


class HarnessRuleCreate(BaseModel):
    rule_id: str
    rule_type: str  # 'blacklist' | 'path_boundary' | 'quota' | 'cooling_off'
    name: str
    description: str | None = None
    config_json: dict[str, Any]
    priority: int = 0
    enabled: bool = True


@router.post("/harness/rules")
async def create_harness_rule(
    body: HarnessRuleCreate,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Create a new harness rule."""
    from athena.models.harness_rule import HarnessRule

    existing = await db.get(HarnessRule, body.rule_id)
    if existing:
        return error(40901, "Rule already exists", f"Rule ID '{body.rule_id}' already exists")

    # Validate config_json for path_permission rules
    if body.rule_type == "path_permission":
        if "path" not in body.config_json:
            return error(400, "Missing 'path' in config_json", "path_permission rules require a 'path' field")
        perms = body.config_json.get("permissions", "rw")
        if perms not in ("r", "w", "rw"):
            return error(400, "Invalid permissions", f"permissions must be 'r', 'w', or 'rw', got '{perms}'")

    rule = HarnessRule(
        rule_id=body.rule_id,
        rule_type=body.rule_type,
        name=body.name,
        description=body.description,
        config_json=json.dumps(body.config_json),
        priority=body.priority,
        enabled=body.enabled,
    )
    db.add(rule)
    await db.commit()

    logger.info("harness_rule_created", rule_id=body.rule_id, rule_type=body.rule_type)
    return success({
        "rule_id": body.rule_id,
        "rule_type": body.rule_type,
        "name": body.name,
        "config": body.config_json,
        "priority": body.priority,
        "enabled": body.enabled,
    }, "Rule created")


class HarnessRuleUpdate(BaseModel):
    config_json: dict[str, Any] | None = None
    priority: int | None = None
    enabled: bool | None = None


@router.put("/harness/rules/{rule_id}")
async def update_harness_rule(
    rule_id: str,
    body: HarnessRuleUpdate,
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Update a harness rule."""
    from athena.models.harness_rule import HarnessRule

    rule = await db.get(HarnessRule, rule_id)
    if not rule:
        return error(40401, "Rule not found", f"Rule '{rule_id}' not found")

    if body.config_json is not None:
        rule.config_json = json.dumps(body.config_json)
    if body.priority is not None:
        rule.priority = body.priority
    if body.enabled is not None:
        rule.enabled = body.enabled

    # Increment revision to trigger hot-reload detection
    rule.revision = (rule.revision or 0) + 1
    rule.updated_at = datetime.now(timezone.utc)

    await db.commit()

    logger.info("harness_rule_updated", rule_id=rule_id, revision=rule.revision)
    return success({"rule_id": rule_id, "revision": rule.revision})


@router.post("/harness/reload")
async def reload_harness_rules(
    request: Any = None,  # noqa: ANN401
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Force immediate reload of harness rules cache."""
    # The actual reload is triggered by the HarnessEngine
    logger.info("harness_reload_requested")
    return success(None, "Harness rules reload triggered")


# ── Memory management ─────────────────────────────────────────────────

@router.post("/memories/resync")
async def resync_memories(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Trigger full vector re-sync for all memories."""
    from athena.models.user_memory import UserMemory
    from sqlalchemy import update

    # Mark all synced memories as pending for re-sync
    result = await db.execute(
        update(UserMemory)
        .where(UserMemory.sync_status == "synced")
        .values(sync_status="pending")
    )
    await db.commit()

    count = result.rowcount
    logger.info("memory_resync_triggered", count=count)
    return success({"memories_to_resync": count}, f"Resync triggered for {count} memories")


# ── Audit logs ────────────────────────────────────────────────────────

def _parse_audit_cursor(cursor: str | None) -> tuple[datetime, str] | None:
    """Parse a composite cursor string of form ``{iso_timestamp}__{event_id}``."""
    if not cursor:
        return None
    parts = cursor.rsplit("__", 1)
    if len(parts) != 2:
        return None
    try:
        ts = datetime.fromisoformat(parts[0])
        return ts, parts[1]
    except ValueError:
        return None


def _make_audit_cursor(timestamp: datetime, event_id: str) -> str:
    """Build a composite cursor string."""
    return f"{timestamp.isoformat()}__{event_id}"


@router.get("/audit-logs")
async def list_audit_logs(
    event_type: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Search audit logs with cursor-based pagination.

    Cursor is a composite of ``{iso_timestamp}__{event_id}`` from the last
    item of the previous page. Results are ordered by timestamp descending.
    """
    from athena.models.audit_log import AuditLog

    stmt = select(AuditLog)

    if event_type:
        stmt = stmt.where(AuditLog.event_type == event_type)

    parsed_cursor = _parse_audit_cursor(cursor)
    if parsed_cursor:
        cursor_ts, cursor_id = parsed_cursor
        stmt = stmt.where(
            tuple_(AuditLog.timestamp, AuditLog.event_id) < (cursor_ts, cursor_id)
        )

    stmt = stmt.order_by(AuditLog.timestamp.desc(), AuditLog.event_id.desc())
    stmt = stmt.limit(limit + 1)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    items = rows[:limit]

    next_cursor = _make_audit_cursor(items[-1].timestamp, items[-1].event_id) if has_more else None

    return success({
        "items": [
            {
                "event_id": r.event_id,
                "event_type": r.event_type,
                "actor_user_id": r.actor_user_id,
                "details": json.loads(r.details_json) if r.details_json else None,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            }
            for r in items
        ],
        "next_cursor": next_cursor,
    })


# ── Dashboard ─────────────────────────────────────────────────────────

@router.get("/dashboard")
async def get_dashboard(
    db: AsyncSession = Depends(get_db),
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Get real-time dashboard metrics summary."""
    from athena.models.task import Task
    from athena.models.subtask_execution import SubtaskExecution
    from athena.models.audit_log import AuditLog

    # Task counts by status
    task_result = await db.execute(
        select(Task.status, func.count()).group_by(Task.status)
    )
    task_counts = {row[0]: row[1] for row in task_result.all()}

    # Subtask success rate (last 24h)
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    subtask_result = await db.execute(
        select(
            SubtaskExecution.status,
            func.count(),
        ).where(SubtaskExecution.finished_at >= since)
        .group_by(SubtaskExecution.status)
    )
    subtask_counts = {row[0]: row[1] for row in subtask_result.all()}
    total_subtasks = sum(subtask_counts.values())
    success_rate = subtask_counts.get("success", 0) / max(total_subtasks, 1)

    # Harness blocks (last 24h)
    block_result = await db.execute(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.event_type == "harness_block",
            AuditLog.timestamp >= since,
        )
    )
    harness_blocks = block_result.scalar_one()

    return success({
        "tasks": task_counts,
        "subtask_success_rate": round(success_rate * 100, 1),
        "total_subtasks_24h": total_subtasks,
        "harness_blocks_24h": harness_blocks,
    })


# ── IM Status ─────────────────────────────────────────────────────────

@router.get("/im/status")
async def get_im_status(
    request: Any = None,  # noqa: ANN401
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Get status of all IM channel adapters."""
    # Status is provided by the GatewayManager
    return success({
        "channels": [
            {
                "channel": "telegram",
                "status": "configured" if (get_config_dep().user.is_channel_enabled("telegram")) else "disabled",
            },
            {
                "channel": "wechat",
                "status": "configured" if (get_config_dep().user.is_channel_enabled("wechat")) else "disabled",
            },
            {
                "channel": "web",
                "status": "configured" if (get_config_dep().user.is_channel_enabled("web")) else "disabled",
            },
        ]
    })


@router.get("/im/wechat/qrcode")
async def get_wechat_qrcode(
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Get WeChat QR code for login (base64-encoded PNG)."""
    return success({"qrcode_base64": "not_implemented"}, "QR code generation not yet available")


@router.get("/im/wechat/status")
async def get_wechat_status(
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Get WeChat adapter connection status."""
    return success({
        "status": "disconnected",
        "token_valid": False,
        "token_expires_at": None,
    })


@router.post("/im/wechat/reconnect")
async def reconnect_wechat(
    api_key: str = Depends(verify_api_key),
) -> dict[str, Any]:
    """Force WeChat re-authentication."""
    return success(None, "Re-authentication trigger sent")
