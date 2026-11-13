#!/usr/bin/env bash
set -euo pipefail

INFRA="$(cd "$(dirname "$0")/.." && pwd)"
NS=niaspo

if ! kind get clusters | grep -qx niaspo; then
  kind create cluster --config "$INFRA/kind-config.yaml"
fi

for svc in gateway frontend ai-inference; do
  docker build -t "niaspo-$svc:dev" "$INFRA/services/$svc"
  kind load docker-image "niaspo-$svc:dev" --name niaspo
done

EXTERNAL_IMAGES=(
  postgres:16-alpine
  redis:7-alpine
  grafana/grafana:12.3.1
  quay.io/prometheus/prometheus:v3.16.0
  registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.20.0
)

platform="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"

for img in "${EXTERNAL_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull "$img"
  workdir="$(mktemp -d)"
  docker save --platform "$platform" "$img" -o "$workdir/image.tar"
  kind load image-archive "$workdir/image.tar" --name niaspo
  rm -rf "$workdir"
done

"$INFRA/scripts/create-secrets.sh"

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
helm upgrade --install prometheus prometheus-community/prometheus -n "$NS" -f "$INFRA/helm/prometheus-values.yaml"
helm upgrade --install grafana grafana/grafana -n "$NS" -f "$INFRA/helm/grafana-values.yaml"

kubectl apply -k "$INFRA/k8s"

for deploy in postgres redis ai-inference frontend gateway grafana prometheus-server; do
  kubectl -n "$NS" rollout status "deployment/$deploy" --timeout=240s
done

"$INFRA/scripts/check.sh"
