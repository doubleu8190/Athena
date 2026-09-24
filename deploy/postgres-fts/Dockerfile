ARG POSTGRES_BASE_IMAGE=postgres:18.6-trixie

FROM ${POSTGRES_BASE_IMAGE} AS builder

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update -o Acquire::Retries=5 \
    && apt-get install -y -o Acquire::Retries=5 --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        ca-certificates \
        curl \
        libtool \
        postgresql-server-dev-18 \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src

RUN curl -fsSL https://github.com/hightman/scws/archive/refs/tags/1.2.3.tar.gz \
        -o scws.tar.gz \
    && mkdir scws-src \
    && tar -xzf scws.tar.gz --strip-components=1 -C scws-src \
    && cd scws-src \
    && autoreconf -fi \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install \
    && cd /usr/src \
    && curl -fsSL https://github.com/amutu/zhparser/archive/refs/heads/master.tar.gz \
        -o zhparser.tar.gz \
    && mkdir zhparser-src \
    && tar -xzf zhparser.tar.gz --strip-components=1 -C zhparser-src \
    && cd zhparser-src \
    && SCWS_HOME=/usr/local make -j"$(nproc)" \
    && SCWS_HOME=/usr/local make install \
    && rm -rf /usr/src/scws-src /usr/src/zhparser-src /usr/src/*.tar.gz

FROM ${POSTGRES_BASE_IMAGE}

COPY --from=builder /usr/local/lib/libscws.so* /usr/local/lib/
COPY --from=builder /usr/local/etc/ /usr/local/etc/
COPY --from=builder /usr/lib/postgresql/18/lib/zhparser.so /usr/lib/postgresql/18/lib/
COPY --from=builder /usr/share/postgresql/18/extension/zhparser* /usr/share/postgresql/18/extension/
COPY --from=builder /usr/share/postgresql/18/tsearch_data/dict.utf8.xdb /usr/share/postgresql/18/tsearch_data/
COPY --from=builder /usr/share/postgresql/18/tsearch_data/rules.utf8.ini /usr/share/postgresql/18/tsearch_data/

RUN ldconfig
