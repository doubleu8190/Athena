"""PostgreSQL acceptance for the shared execution lease center."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from domain.leases import LeaseKey, LeaseLostError
from infrastructure.persistence.postgres.models import Base, ExecutionLeaseModel
from infrastructure.persistence.postgres.repositories.lease_center import (
    PostgresLeaseCenter,
)


@pytest_asyncio.fixture
async def lease_centers():
    """Create two independent PostgreSQL clients in a disposable schema."""
    database_url = os.getenv("ATHENA_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")

    schema = f"lease_center_{uuid4().hex}"
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(url)
    engine_a = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    engine_b = create_async_engine(
        url.update_query_dict({"options": f"-c search_path={schema}"})
    )
    schema_created = False
    try:
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
        async with engine_a.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[ExecutionLeaseModel.__table__]
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE lease_business_probe "
                    "(id TEXT PRIMARY KEY, status TEXT NOT NULL)"
                )
            )

        sessions_a = async_sessionmaker(engine_a, expire_on_commit=False)
        sessions_b = async_sessionmaker(engine_b, expire_on_commit=False)
        center = PostgresLeaseCenter(sessions_a)
        async with sessions_a() as session, session.begin():
            for resource_id in ("shared-resource", "resource-left", "resource-right",
                "takeover-resource", "revoked-resource", "transaction-resource"):
                await center.ensure_in_transaction(session, LeaseKey("sample_worker", resource_id))
        yield (
            PostgresLeaseCenter(sessions_a),
            PostgresLeaseCenter(sessions_b),
            sessions_a,
            sessions_b,
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
        if schema_created:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_independent_connections_compete_atomically_and_distinct_keys_run_in_parallel(
    lease_centers,
) -> None:
    """Only one client owns a key, while unrelated resources can both be claimed."""
    center_a, center_b, _, _ = lease_centers
    shared_key = LeaseKey("sample_worker", "shared-resource")

    claims = await asyncio.gather(
        center_a.acquire(shared_key, owner_id="worker-a", lease_seconds=30),
        center_b.acquire(shared_key, owner_id="worker-b", lease_seconds=30),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1

    left, right = await asyncio.gather(
        center_a.acquire(
            LeaseKey("sample_worker", "resource-left"),
            owner_id="worker-a",
            lease_seconds=30,
        ),
        center_b.acquire(
            LeaseKey("sample_worker", "resource-right"),
            owner_id="worker-b",
            lease_seconds=30,
        ),
    )
    assert left is not None and right is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_lease_takeover_increments_token_and_isolates_old_grant(
    lease_centers,
) -> None:
    """An expired owner cannot renew, finish, revoke, or acknowledge a newer grant."""
    center_a, center_b, _, _ = lease_centers
    key = LeaseKey("sample_worker", "takeover-resource")
    old = await center_a.acquire(key, owner_id="old-worker", lease_seconds=0.15)
    assert old is not None

    await asyncio.sleep(0.25)
    current = await center_b.acquire(
        key, owner_id="new-worker", lease_seconds=30
    )
    assert current is not None
    assert current.fencing_token == old.fencing_token + 1
    assert await center_a.renew(old, lease_seconds=30) is None
    assert await center_a.finish(old) is False
    assert await center_a.revoke(key, expected_token=old.fencing_token) is False
    assert await center_a.acknowledge_stopped(old) is False
    await center_b.assert_current(current)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revocation_blocks_writes_but_accepts_matching_stop_acknowledgement(
    lease_centers,
) -> None:
    """Revoked authority is immediately fenced and only its own stop ack releases it."""
    center_a, center_b, _, _ = lease_centers
    key = LeaseKey("sample_worker", "revoked-resource")
    revoked = await center_a.acquire(key, owner_id="worker-a", lease_seconds=30)
    assert revoked is not None

    assert await center_b.revoke(key, expected_token=revoked.fencing_token)
    assert await center_a.renew(revoked, lease_seconds=30) is None
    assert await center_a.finish(revoked) is False
    with pytest.raises(LeaseLostError):
        await center_a.assert_current(revoked)
    assert await center_a.acknowledge_stopped(revoked)

    current = await center_b.acquire(key, owner_id="worker-b", lease_seconds=30)
    assert current is not None
    assert current.fencing_token == revoked.fencing_token + 1
    assert await center_a.acknowledge_stopped(revoked) is False
    assert await center_a.revoke(key, expected_token=revoked.fencing_token) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_business_changes_and_lease_finish_share_one_rollback_boundary(
    lease_centers,
) -> None:
    """A failed transaction rolls back both business data and lease release."""
    center_a, _, sessions_a, _ = lease_centers
    key = LeaseKey("sample_worker", "transaction-resource")
    grant = await center_a.acquire(key, owner_id="worker-a", lease_seconds=30)
    assert grant is not None

    async with sessions_a() as session, session.begin():
        await session.execute(
            text("INSERT INTO lease_business_probe (id, status) VALUES ('row-1', 'ready')")
        )

    with pytest.raises(RuntimeError, match="rollback probe"):
        async with sessions_a() as session, session.begin():
            await center_a.assert_current_in_transaction(session, grant)
            await session.execute(
                text("UPDATE lease_business_probe SET status='completed' WHERE id='row-1'")
            )
            assert await center_a.finish_in_transaction(session, grant)
            raise RuntimeError("rollback probe")

    async with sessions_a() as session:
        status = await session.scalar(
            text("SELECT status FROM lease_business_probe WHERE id='row-1'")
        )
    assert status == "ready"
    await center_a.assert_current(grant)

    async with sessions_a() as session, session.begin():
        await center_a.assert_current_in_transaction(session, grant)
        await session.execute(
            text("UPDATE lease_business_probe SET status='completed' WHERE id='row-1'")
        )
        assert await center_a.finish_in_transaction(session, grant)

    assert await center_a.finish(grant) is False
    async with sessions_a() as session:
        status = await session.scalar(
            text("SELECT status FROM lease_business_probe WHERE id='row-1'")
        )
        lease_row = await session.scalar(
            select(ExecutionLeaseModel).where(
                ExecutionLeaseModel.resource_type == key.resource_type,
                ExecutionLeaseModel.resource_id == key.resource_id,
            )
        )
    assert status == "completed"
    assert lease_row is not None
    assert lease_row.state == "released"
    assert lease_row.fencing_token == grant.fencing_token
