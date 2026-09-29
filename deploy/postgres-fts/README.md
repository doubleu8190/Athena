# PostgreSQL 18 with Chinese full-text search

This Compose project builds a PostgreSQL 18.6 image with SCWS, `zhparser`, and
`pgvector`.
It is intentionally isolated from the repository's existing PostgreSQL
container: it binds to `127.0.0.1:5433` and uses a separate named volume.

## Build

From the repository root:

```sh
docker compose -f deploy/compose.yaml build postgres
```

If Docker Hub is unavailable but the matching PostgreSQL base image is already
cached locally, pass its local tag:

```sh
POSTGRES_BASE_IMAGE=postgres:latest docker compose -f deploy/compose.yaml build postgres
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
docker compose -f deploy/compose.yaml up -d postgres
docker compose -f deploy/compose.yaml exec postgres \
  psql -U doubleu -d athena -c \
  "SELECT to_tsvector('chinese', '用户偏好使用 PostgreSQL 数据库');"
```

The init SQL runs only when the named volume is first initialized. It creates
the `vector` and `zhparser` extensions and the `chinese` text-search
configuration. Set `PGVECTOR_VERSION` when building to select a different
pgvector release; the default is `0.8.1`.

The container receives the database password through the mounted Compose
secret at `/run/secrets/postgres_password`; it is not stored in this Compose
file.

This project uses its own named volume, `postgres_data`; it does not mount or
remove `data/postgresql` from the existing local database container.

## Neo4j

The repository-level Compose file also provides the optional Neo4j service. Its
authentication is read from `deploy/neo4j/secrets/neo4j_auth`:

```sh
docker compose -f deploy/compose.yaml up -d --force-recreate neo4j
```

The secret can contain either `neo4j/<password>` or only `<password>`; the
startup wrapper handles both formats. Keep the file readable by Docker and
recreate the container after changing it:

```sh
test -r deploy/neo4j/secrets/neo4j_auth
docker compose -f deploy/compose.yaml config >/dev/null
docker compose -f deploy/compose.yaml up -d --force-recreate neo4j
```
