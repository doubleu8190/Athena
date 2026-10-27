"""Small deterministic lease fixtures for unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from domain.leases import LeaseGrant, LeaseKey


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

    grant = await PostgresLeaseCenter(session_factory).acquire(
        LeaseKey(resource_type, resource_id),
        owner_id=f"integration-test:{uuid4().hex}",
        lease_seconds=900,
    )
    if grant is None:
        raise AssertionError("test lease was unexpectedly unavailable")
    return grant


class LeaseCenter:
    """A permissive LeaseCenter fake for tests that focus on business behavior."""

    async def acquire(self, key, *, owner_id, lease_seconds):
        return lease_grant(key.resource_type, key.resource_id, owner_id=owner_id)

    async def renew(self, grant, *, lease_seconds):
        return grant

    async def assert_current(self, grant):
        return None

    async def acquire_in_transaction(
        self, _session, key, *, owner_id, lease_seconds
    ):
        return lease_grant(key.resource_type, key.resource_id, owner_id=owner_id)

    async def assert_current_in_transaction(self, _session, _grant):
        return None

    async def finish_in_transaction(self, _session, _grant):
        return True

    async def acknowledge_stopped_in_transaction(self, _session, _grant):
        return True

    async def finish(self, grant):
        return True

    async def revoke(self, key, *, expected_token):
        return True

    async def acknowledge_stopped(self, grant):
        return True
