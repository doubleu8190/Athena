# MinIO 精简镜像不带 HTTP 客户端；只从固定 BusyBox 镜像复制静态探针工具。
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS healthcheck
FROM coollabsio/minio:RELEASE.2025-10-15T17-29-55Z@sha256:69b55a1c1c5dc285ce04db96689f5b2102317fc77a50680a1874ca6efd1c87f9

COPY --from=healthcheck /bin/busybox /usr/local/bin/busybox
COPY entrypoint.sh /usr/local/bin/athena-minio-entrypoint
RUN chmod +x /usr/local/bin/athena-minio-entrypoint
ENTRYPOINT ["/usr/local/bin/athena-minio-entrypoint"]
