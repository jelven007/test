#!/usr/bin/env bash
set -euo pipefail

bootstrap="${KAFKA_BOOTSTRAP_SERVERS:-kafka:9092}"
topics=(
  "market.quote.snapshot.v1:12:604800000"
  "market.bar.1m.v1:12:1209600000"
  "market.feature.realtime.v1:12:604800000"
  "strategy.plan.created.v1:3:2592000000"
  "strategy.decision.v1:12:2592000000"
  "strategy.audit.v1:12:7776000000"
  "report.generated.v1:3:2592000000"
)

for spec in "${topics[@]}"; do
  IFS=: read -r topic partitions retention_ms <<<"$spec"
  /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server "$bootstrap" \
    --create \
    --if-not-exists \
    --topic "$topic" \
    --partitions "$partitions" \
    --replication-factor 1 \
    --config "retention.ms=$retention_ms"

  /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server "$bootstrap" \
    --create \
    --if-not-exists \
    --topic "${topic}.dlq" \
    --partitions "$partitions" \
    --replication-factor 1 \
    --config "retention.ms=7776000000"
done
