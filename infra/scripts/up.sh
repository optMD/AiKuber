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
