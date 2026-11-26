# PostgreSQL 18 + pgvector 0.8.7，上游基础镜像提供对应扩展版本。
FROM pgvector/pgvector:0.8.7-pg18-bookworm@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47

COPY init/10-create-databases.sh /docker-entrypoint-initdb.d/10-create-databases.sh
COPY healthcheck.sh /usr/local/bin/athena-postgres-healthcheck
RUN chmod +x /usr/local/bin/athena-postgres-healthcheck
