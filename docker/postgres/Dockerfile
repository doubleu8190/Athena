# PostgreSQL 18 + pgvector 0.8.7，上游基础镜像提供对应扩展版本。
FROM pgvector/pgvector:0.8.7-pg18-bookworm@sha256:c29f30688a9ad961691bdf0ec1650717cc132a5814619d80f80463d6e4c42e47

ARG ZHPARSER_COMMIT=21ee29212e9f3b3c34bafb096e184f26c84111b5
ARG SCWS_SHA256=60d50ac3dc42cff3c0b16cb1cfee47d8cb8c8baa142a58bc62854477b81f1af5

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential ca-certificates curl git \
        postgresql-server-dev-18; \
    curl -fsSL https://www.xunsearch.com/scws/down/scws-1.2.3.tar.bz2 \
        -o /tmp/scws-1.2.3.tar.bz2; \
    echo "${SCWS_SHA256}  /tmp/scws-1.2.3.tar.bz2" | sha256sum -c -; \
    mkdir /tmp/scws; \
    tar -xjf /tmp/scws-1.2.3.tar.bz2 --strip-components=1 -C /tmp/scws; \
    cd /tmp/scws; \
    ./configure; \
    make -j"$(nproc)"; \
    make install; \
    git clone --filter=blob:none https://github.com/amutu/zhparser.git /tmp/zhparser; \
    cd /tmp/zhparser; \
    git checkout "${ZHPARSER_COMMIT}"; \
    make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config; \
    make PG_CONFIG=/usr/lib/postgresql/18/bin/pg_config install; \
    ldconfig; \
    apt-get purge -y --auto-remove build-essential curl git postgresql-server-dev-18; \
    rm -rf /var/lib/apt/lists/* /tmp/scws /tmp/zhparser /tmp/scws-1.2.3.tar.bz2

COPY init/10-create-databases.sh /docker-entrypoint-initdb.d/10-create-databases.sh
COPY healthcheck.sh /usr/local/bin/athena-postgres-healthcheck
RUN chmod +x /usr/local/bin/athena-postgres-healthcheck
