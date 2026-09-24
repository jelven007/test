#!/bin/sh
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

for bucket in market-raw strategy-reports strategy-audit flink-state; do
  mc mb --ignore-existing "local/$bucket"
  mc version enable "local/$bucket"
  mc anonymous set none "local/$bucket"
done
