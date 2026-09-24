# Kubernetes deployment

The base manifests deploy the stateless application services, the 16:20
`report-worker` CronJob, Flink job/task managers, the realtime feature job,
PDB/HPA policies, and a shared report volume. Kafka, PostgreSQL, ClickHouse,
Redis, and MinIO are expected to be managed services reachable at the DNS
names in `base/platform.yaml`.

Before applying:

1. Build and publish `Dockerfile` as `banxia-strategy`.
2. Build and publish `deploy/flink/Dockerfile` as `banxia-flink`.
3. Replace the image names/tags in `base/kustomization.yaml`.
4. Create `banxia-secrets` from a secret manager. The example file contains
   placeholders only and must not be applied unchanged.
5. Adjust the `ReadWriteMany` storage class and managed-service DNS names.

Apply the deployment:

```bash
kubectl apply -f deploy/kubernetes/banxia-secrets.yaml
kubectl apply -k deploy/kubernetes/base
```

Run the report job immediately after the first deployment:

```bash
kubectl -n banxia create job \
  --from=cronjob/report-worker report-worker-bootstrap
```

The collector runs two replicas but only the holder of the PostgreSQL
advisory lease contacts mootdx. A standby takes over after the leader
connection closes. The local WAL uses pod-local storage; production clusters
should replace the `emptyDir` volume with a small encrypted persistent volume
when zero-loss recovery across node eviction is required.
