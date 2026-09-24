#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$PROJECT_ROOT/deploy/compose/.env"
COMPOSE_FILE="$PROJECT_ROOT/deploy/compose/docker-compose.yml"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker is not installed" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "error: copy deploy/compose/.env.example to deploy/compose/.env first" >&2
  exit 1
fi

compose=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

"${compose[@]}" ps

postgres_tables="$(
  "${compose[@]}" exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = '\''banxia'\'';"'
)"
if (( postgres_tables < 14 )); then
  echo "error: expected at least 14 PostgreSQL tables, got $postgres_tables" >&2
  exit 1
fi

clickhouse_tables="$(
  "${compose[@]}" exec -T clickhouse sh -c \
    'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --query "SELECT count() FROM system.tables WHERE database = '\''banxia'\''"'
)"
if (( clickhouse_tables < 4 )); then
  echo "error: expected at least 4 ClickHouse tables/views, got $clickhouse_tables" >&2
  exit 1
fi

"${compose[@]}" exec -T redis sh -c \
  'test "$(redis-cli -a "$REDIS_PASSWORD" --no-auth-warning ping)" = "PONG"'

"${compose[@]}" run --rm minio-init

kafka_topics=""
required_topics=(
  market.quote.snapshot.v1
  market.bar.1m.v1
  market.feature.realtime.v1
  strategy.plan.created.v1
  strategy.decision.v1
  strategy.audit.v1
  report.generated.v1
)
for _attempt in {1..30}; do
  kafka_topics="$(
    "${compose[@]}" exec -T kafka \
      /opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
  )"
  missing_topic=""
  for topic in "${required_topics[@]}"; do
    if ! grep -qx "$topic" <<<"$kafka_topics"; then
      missing_topic="$topic"
      break
    fi
  done
  [[ -z "$missing_topic" ]] && break
  sleep 2
done
for topic in "${required_topics[@]}"; do
  if ! grep -qx "$topic" <<<"$kafka_topics"; then
    echo "error: missing Kafka topic $topic" >&2
    exit 1
  fi
done

echo "Infrastructure smoke check passed."
