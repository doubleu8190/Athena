#!/bin/sh
set -eu

auth_file=/run/secrets/neo4j_auth

if [ ! -r "$auth_file" ]; then
  echo "Neo4j auth secret is missing or unreadable: $auth_file" >&2
  exit 1
fi

auth=$(cat "$auth_file")
if [ -z "$auth" ]; then
  echo "Neo4j auth secret is empty: $auth_file" >&2
  exit 1
fi

# Accept either Neo4j's username/password format or a password-only file.
case "$auth" in
  */*) export NEO4J_AUTH="$auth" ;;
  *) export NEO4J_AUTH="neo4j/$auth" ;;
esac

exec /startup/docker-entrypoint.sh "$@"
