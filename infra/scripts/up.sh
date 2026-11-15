#!/usr/bin/env bash
set -euo pipefail

INFRA="$(cd "$(dirname "$0")/.." && pwd)"
NS=niaspo
KUBE_CONTEXT=kind-niaspo
APP_DEPLOYMENTS=(ai-inference frontend gateway)
ALL_DEPLOYMENTS=(postgres redis "${APP_DEPLOYMENTS[@]}" grafana prometheus-server)

if ! kind get clusters | grep -qx niaspo; then
  kind create cluster --config "$INFRA/kind-config.yaml"
fi

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

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

for deploy in "${APP_DEPLOYMENTS[@]}"; do
  kubectl -n "$NS" rollout restart "deployment/$deploy"
done

for deploy in "${ALL_DEPLOYMENTS[@]}"; do
  kubectl -n "$NS" rollout status "deployment/$deploy" --timeout=240s
done

"$INFRA/scripts/check.sh"
