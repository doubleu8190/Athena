# PostgreSQL 18 with Chinese full-text search

This Compose project builds a PostgreSQL 18.6 image with SCWS and `zhparser`.
It is intentionally isolated from the repository's existing PostgreSQL
container: it binds to `127.0.0.1:5433` and uses a separate named volume.

## Build

From the repository root:

```sh
docker compose -f deploy/postgres-fts/compose.yaml build
```

If Docker Hub is unavailable but the matching PostgreSQL base image is already
cached locally, pass its local tag:

```sh
POSTGRES_BASE_IMAGE=postgres:latest docker compose -f deploy/postgres-fts/compose.yaml build
```

The default base is pinned to `postgres:18.6-trixie`. The override is only for
local builds and should be used after confirming that the cached image is
PostgreSQL 18.6 on Debian 13.

## Start and verify

Create the local Docker secret file first. It is intentionally ignored by Git
and must never be committed:

```sh
mkdir -p deploy/postgres-fts/secrets
umask 077
openssl rand -base64 32 > deploy/postgres-fts/secrets/postgres_password
```

Set the same password in the repository `.env` as `POSTGRES_PASSWORD` for the
Athena application, then start the isolated service:

```sh
docker compose -f deploy/postgres-fts/compose.yaml up -d
docker compose -f deploy/postgres-fts/compose.yaml exec postgres \
  psql -U doubleu -d athena -c \
  "SELECT to_tsvector('chinese', '用户偏好使用 PostgreSQL 数据库');"
```

The init SQL runs only when the named volume is first initialized. It creates
the `zhparser` extension and the `chinese` text-search configuration.

The container receives the database password through the mounted Compose
secret at `/run/secrets/postgres_password`; it is not stored in this Compose
file.

This project uses its own named volume, `postgres_data`; it does not mount or
remove `data/postgresql` from the existing local database container.
