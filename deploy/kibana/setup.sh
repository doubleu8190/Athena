#!/bin/sh
# Kibana setup: import dashboards and create index pattern.
# This container runs once and exits.

set -e

KIBANA_URL="${KIBANA_URL:-http://kibana:5601}"
ES_URL="${ELASTICSEARCH_HOSTS:-http://elasticsearch:9200}"

echo "Waiting for Kibana to be ready..."
until curl -s "$KIBANA_URL/api/status" | grep -q '"level":"available"'; do
    sleep 5
done

echo "Kibana is ready. Creating index pattern..."

# Create athena-* index pattern as default
curl -s -X POST "$KIBANA_URL/api/saved_objects/_import" \
    -H "kbn-xsrf: true" \
    --form file=@/dashboards/athena-index-pattern.ndjson

# Import dashboards if available
for f in /dashboards/*.ndjson; do
    if [ -f "$f" ]; then
        echo "Importing dashboard: $f"
        curl -s -X POST "$KIBANA_URL/api/saved_objects/_import" \
            -H "kbn-xsrf: true" \
            --form file=@"$f"
    fi
done

echo "Kibana setup complete."
