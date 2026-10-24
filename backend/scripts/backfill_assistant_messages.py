"""Safely backfill missing final assistant messages from persisted Run output.

Run from any directory with the backend environment configured:
    python backend/scripts/backfill_assistant_messages.py
Add ``--apply`` only after reviewing the dry-run counts. The script refuses to
write if it finds conflicting durable sources or target messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from collections import defaultdict
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from configuration.settings import get_settings


_CANDIDATE_QUERY = text(
    """
    SELECT r.run_id, r.session_id, r.request_json, r.response_json, r.updated_at,
           j.assistant_text
    FROM agent_runs AS r
    LEFT JOIN memory_processing_jobs AS j
      ON j.turn_id = r.run_id AND j.session_id = r.session_id
    WHERE r.status = 'completed'
    ORDER BY r.updated_at, r.run_id
    """
)
_EXISTING_QUERY = text(
    """
    SELECT id, session_id, role, content, run_id
    FROM messages
    WHERE role = 'assistant' OR id LIKE 'assistant:%'
    """
)
_INSERT_QUERY = text(
    """
    INSERT INTO messages (
        id, session_id, role, content, tool_calls_json, run_id, timestamp
    ) VALUES (
        :id, :session_id, 'assistant', :content, '[]', :run_id, :timestamp
    )
    ON CONFLICT (id) DO NOTHING
    RETURNING id
    """
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, dict) else {}


def _answer_text(value: Any) -> str:
    response = _json_object(value)
    content = response.get("content")
    if isinstance(content, str):
        return content
    nested = response.get("result")
    if isinstance(nested, dict) and isinstance(nested.get("content"), str):
        return str(nested["content"])
    return ""


async def _run(apply: bool) -> int:
    engine = create_async_engine(get_settings().postgres_url)
    try:
        async with engine.begin() as connection:
            if apply:
                await connection.execute(
                    text("LOCK TABLE messages IN SHARE ROW EXCLUSIVE MODE")
                )
            candidate_rows = (await connection.execute(_CANDIDATE_QUERY)).mappings().all()
            existing_rows = (await connection.execute(_EXISTING_QUERY)).mappings().all()

            by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
            by_id = {str(row["id"]): row for row in existing_rows}
            for row in existing_rows:
                if row["run_id"]:
                    by_run[str(row["run_id"])].append(dict(row))

            counters = {
                "completed_runs": len(candidate_rows),
                "missing_request_message_id": 0,
                "missing_answer_source": 0,
                "source_conflicts": 0,
                "target_conflicts": 0,
                "already_present": 0,
                "would_insert": 0,
            }
            conflict_run_ids: list[str] = []
            inserts: list[dict[str, str]] = []

            for row in candidate_rows:
                run_id = str(row["run_id"])
                request = _json_object(row["request_json"])
                message_id = str(request.get("message_id") or "").strip()
                if not message_id:
                    counters["missing_request_message_id"] += 1
                    continue

                response_text = _answer_text(row["response_json"])
                memory_text = str(row["assistant_text"] or "")
                if response_text and memory_text and response_text != memory_text:
                    counters["source_conflicts"] += 1
                    conflict_run_ids.append(run_id)
                    continue
                answer = response_text or memory_text
                if not answer.strip():
                    counters["missing_answer_source"] += 1
                    continue

                stable_id = f"assistant:{run_id}"
                stable_row = by_id.get(stable_id)
                other_run_rows = [
                    item for item in by_run.get(run_id, []) if item["id"] != stable_id
                ]
                if stable_row is not None:
                    if (
                        stable_row["session_id"] == row["session_id"]
                        and stable_row["role"] == "assistant"
                        and stable_row["run_id"] == run_id
                        and stable_row["content"] == answer
                        and not other_run_rows
                    ):
                        counters["already_present"] += 1
                        continue
                    counters["target_conflicts"] += 1
                    conflict_run_ids.append(run_id)
                    continue
                if other_run_rows:
                    counters["target_conflicts"] += 1
                    conflict_run_ids.append(run_id)
                    continue

                inserts.append(
                    {
                        "id": stable_id,
                        "session_id": str(row["session_id"]),
                        "content": answer,
                        "run_id": run_id,
                        "timestamp": str(row["updated_at"]),
                    }
                )

            counters["would_insert"] = len(inserts)
            report = {
                **counters,
                "conflict_run_ids": sorted(set(conflict_run_ids))[:50],
                "conflict_run_ids_truncated": len(set(conflict_run_ids)) > 50,
                "mode": "apply" if apply else "dry_run",
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))

            if not apply:
                return 0
            if counters["source_conflicts"] or counters["target_conflicts"]:
                print("Refusing to apply while any source or target conflicts exist.")
                return 2

            inserted = 0
            for item in inserts:
                inserted_id = await connection.scalar(_INSERT_QUERY, item)
                inserted += int(inserted_id is not None)
            print(json.dumps({"inserted": inserted}, ensure_ascii=False))
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="insert missing assistant rows after a conflict-free preflight",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
