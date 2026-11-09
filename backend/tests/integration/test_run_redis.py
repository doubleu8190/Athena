"""Run Redis Pub/Sub 实际收发集成测试。"""

from __future__ import annotations

import asyncio
import os
from secrets import token_hex

import pytest
from redis.asyncio import Redis

from adapter.run.redis_transport import RedisRunTransport
from domain.run.model import RunTextDeltaEvent

REDIS_URL = os.getenv("ATHENA_REDIS_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not REDIS_URL, reason="ATHENA_REDIS_URL is not configured"),
]


def test_run_redis_pubsub_validates_and_delivers_typed_events() -> None:
    """随机频道隔离并通过实际 Redis 收发类型化增量。"""
    redis_url = REDIS_URL
    if redis_url is None:
        pytest.skip("ATHENA_REDIS_URL is not configured")

    async def scenario() -> None:
        run_id = f"run_redis_test_{token_hex(8)}"
        client = Redis.from_url(redis_url, decode_responses=False)
        transport = RedisRunTransport(client)
        try:
            async with transport.subscribe(run_id) as subscription:
                await transport.publish_text(run_id, 4, "redis delivery", round=3)
                received = await subscription.get(timeout=2)
                assert isinstance(received, RunTextDeltaEvent)
                assert received.run_id == run_id
                assert received.execution_epoch == 4
                assert received.stream_sequence == 1
                assert received.payload.text == "redis delivery"
                assert received.payload.round == 3

                large_text = "界🙂" * 40_000
                await transport.publish_text(run_id, 4, large_text, round=5)
                chunks: list[str] = []
                while sum(len(chunk) for chunk in chunks) < len(large_text):
                    chunk_event = await subscription.get(timeout=2)
                    assert isinstance(chunk_event, RunTextDeltaEvent)
                    assert chunk_event.payload.round == 5
                    assert len(chunk_event.model_dump_json().encode("utf-8")) <= 64 * 1024
                    chunks.append(chunk_event.payload.text)
                assert "".join(chunks) == large_text

                await client.publish(
                    f"athena:run:events:{run_id}",
                    b'{"type":"run_event","payload":{"event":null}}',
                )
                assert await subscription.get(timeout=0.1) is None
        finally:
            await transport.aclose()

    asyncio.run(scenario())
