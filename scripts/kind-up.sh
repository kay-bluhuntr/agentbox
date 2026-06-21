#!/usr/bin/env bash
# Spin up a local kind cluster running the full stack: Postgres (in-cluster,
# dev only), the agentbox control plane, and the sandboxed exec namespace.
set -euo pipefail

CLUSTER=agentbox

kind get clusters | grep -q "^${CLUSTER}$" || kind create cluster --name "${CLUSTER}"

docker build -t agentbox:dev .
kind load docker-image agentbox:dev --name "${CLUSTER}"

kubectl create namespace agentbox --dry-run=client -o yaml | kubectl apply -f -

# Generate a random Postgres password, kept out of Git. Reuse the existing one
# on re-runs so the database (and its data) survives a repeat `make kind-up`.
PGPASS=$(kubectl -n agentbox get secret agentbox-db -o jsonpath='{.data.postgres-password}' 2>/dev/null | base64 -d || true)
[ -n "${PGPASS}" ] || PGPASS=$(openssl rand -hex 16)

# Dev-only Postgres
kubectl -n agentbox apply -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata: {name: postgres}
spec:
  replicas: 1
  selector: {matchLabels: {app: postgres}}
  template:
    metadata: {labels: {app: postgres}}
    spec:
      containers:
        - name: postgres
          image: postgres:16-alpine
          env:
            - {name: POSTGRES_USER, value: agentbox}
            - {name: POSTGRES_PASSWORD, value: "${PGPASS}"}
            - {name: POSTGRES_DB, value: agentbox}
          ports: [{containerPort: 5432}]
---
apiVersion: v1
kind: Service
metadata: {name: postgres}
spec:
  selector: {app: postgres}
  ports: [{port: 5432}]
YAML

kubectl -n agentbox create secret generic agentbox-db \
  --from-literal=database-url="postgresql+psycopg://agentbox:${PGPASS}@postgres.agentbox.svc:5432/agentbox" \
  --from-literal=postgres-password="${PGPASS}" \
  --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install agentbox deploy/helm/agentbox \
  --namespace agentbox \
  --set image.repository=agentbox \
  --set image.tag=dev \
  --set image.pullPolicy=IfNotPresent \
  --set replicaCount=1

kubectl -n agentbox rollout status deploy/agentbox --timeout=120s
echo
echo "AgentBox is up. Try:"
echo "  kubectl -n agentbox port-forward svc/agentbox 8080:80 &"
echo "  ./scripts/smoke.sh"
