FROM amazon/aws-cli:2.31.13

COPY entrypoint.sh /usr/local/bin/athena-minio-init-entrypoint
RUN chmod +x /usr/local/bin/athena-minio-init-entrypoint
ENTRYPOINT ["/usr/local/bin/athena-minio-init-entrypoint"]
