"""Small deterministic lease fixtures for unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from domain.leases import LeaseGrant, LeaseKey, LeaseRecord


def lease_grant(
    resource_type: str,
    resource_id: str,
    *,
    owner_id: str = "unit-test-owner",
    fencing_token: int = 1,
) -> LeaseGrant:
    """Build a stable grant whose expiry is far beyond the test clock."""
    return LeaseGrant(
        key=LeaseKey(resource_type, resource_id),
        owner_id=owner_id,
        fencing_token=fencing_token,
        lease_until=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )


def lease_context(grant: LeaseGrant) -> dict[str, object]:
    """Serialize a grant using the stable Root graph execution shape."""
    return {
        "key": {
            "resource_type": grant.key.resource_type,
            "resource_id": grant.key.resource_id,
        },
        "owner_id": grant.owner_id,
        "fencing_token": grant.fencing_token,
        "lease_until": grant.lease_until.isoformat(),
    }


async def acquire_postgres_lease(session_factory, resource_type, resource_id):
    """Acquire a persisted lease for PostgreSQL integration tests."""
    from infrastructure.persistence.postgres.repositories.lease_center import (
        PostgresLeaseCenter,
    )

    leases = PostgresLeaseCenter(session_factory)
    key = LeaseKey(resource_type, resource_id)
    async with session_factory() as session:
        async with session.begin():
            await leases.ensure_in_transaction(session, key)
            grant = await leases.acquire_in_transaction(session, key,
                owner_id=f"integration-test:{uuid4().hex}", lease_seconds=900)
    if grant is None:
        raise AssertionError("test lease was unexpectedly unavailable")
    return grant


class LeaseCenter:
    """A permissive LeaseCenter fake for tests that focus on business behavior."""

    def __init__(self, candidate_keys=(), records=()):
        self._candidate_keys = tuple(candidate_keys)
        self._records = {record.key: record for record in records}

    async def ensure_in_transaction(self, _session, key):
        """为当前测试队列初始化待领取租约。"""
        self._records.setdefault(key, LeaseRecord(key=key, state="pending", owner_id=None, fencing_token=None, lease_until=None))

    async def schedule_in_transaction(self, _session, key):
        """将测试任务重新置为可领取状态。"""
        self._records[key] = LeaseRecord(key=key, state="pending", owner_id=None, fencing_token=None, lease_until=None)

    async def acquire(self, key, *, owner_id, lease_seconds):
        return lease_grant(key.resource_type, key.resource_id, owner_id=owner_id)

    async def renew(self, grant, *, lease_seconds):
        return grant

    async def assert_current(self, grant):
        return None

    async def acquire_in_transaction(
        self, _session, key, *, owner_id, lease_seconds
    ):
        grant = lease_grant(key.resource_type, key.resource_id, owner_id=owner_id)
        self._records[key] = LeaseRecord(
            key=key,
            state="active",
            owner_id=grant.owner_id,
            fencing_token=grant.fencing_token,
            lease_until=grant.lease_until,
        )
        return grant

    async def claimable_keys_in_transaction(
        self, _session, resource_type, *, limit, after_resource_id=None
    ):
        return self._claimable_keys(
            resource_type,
            limit=limit,
            after_resource_id=after_resource_id,
        )

    async def claimable_keys(
        self, resource_type, *, limit, after_resource_id=None
    ):
        return self._claimable_keys(
            resource_type,
            limit=limit,
            after_resource_id=after_resource_id,
        )

    async def get(self, key):
        return self._records.get(key)

    def _claimable_keys(self, resource_type, *, limit, after_resource_id):
        keys = [
            key
            for key in self._candidate_keys
            if key.resource_type == resource_type
            and (after_resource_id is None or key.resource_id > after_resource_id)
        ]
        return keys[:limit]

    async def assert_current_in_transaction(self, _session, _grant):
        return None

    async def finish_in_transaction(self, _session, _grant, *, completed=False):
        await self._release_record(_grant, completed=completed)
        return True

    async def acknowledge_stopped_in_transaction(
        self, _session, _grant, *, completed=False
    ):
        await self._release_record(_grant, completed=completed)
        return True

    async def complete_unclaimed_in_transaction(self, _session, _key):
        self._records.pop(_key, None)
        return True

    async def get_in_transaction(self, _session, key):
        return self._records.get(key)

    async def get_many_in_transaction(self, _session, resource_type, resource_ids):
        return {
            resource_id: record
            for resource_id in resource_ids
            if (record := self._records.get(LeaseKey(resource_type, resource_id)))
            is not None
        }

    async def revoke_in_transaction(
        self, _session, key, *, expected_token
    ):
        record = self._records.get(key)
        if record is None or record.fencing_token != expected_token:
            return False
        self._records[key] = LeaseRecord(
            key=record.key,
            state="revoked",
            owner_id=record.owner_id,
            fencing_token=record.fencing_token,
            lease_until=record.lease_until,
        )
        return True

    async def finish(self, grant, *, completed=False):
        await self._release_record(grant, completed=completed)
        return True

    async def revoke(self, key, *, expected_token):
        return True

    async def acknowledge_stopped(self, grant, *, completed=False):
        await self._release_record(grant, completed=completed)
        return True

    async def _release_record(self, grant, *, completed):
        self._records[grant.key] = LeaseRecord(
            key=grant.key,
            state="completed" if completed else "released",
            owner_id=None,
            fencing_token=grant.fencing_token,
            lease_until=None,
        )
