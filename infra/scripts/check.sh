#!/usr/bin/env bash
set -euo pipefail

NS=niaspo
BASE=http://localhost:8080

wait_for() {
  for _ in $(seq 1 20); do
    if curl -fsS -o /dev/null "$1"; then return 0; fi
    sleep 3
  done
  echo "недоступно: $1" >&2
  return 1
}

ask() {
  curl -fsS -X POST "$BASE/api/ask" -H 'Content-Type: application/json' -d "{\"prompt\":\"проверка $RUN_ID\"}"
}

RUN_ID="$(date +%s)-$$"

kubectl -n "$NS" get pods

echo "--- frontend"
wait_for "$BASE/"
curl -fsS -o /dev/null -w "%{http_code}\n" "$BASE/"

echo "--- api health"
wait_for "$BASE/api/health"
curl -fsS "$BASE/api/health"
echo

echo "--- api ask (первый запрос, ожидается from_cache: false)"
first=$(ask)
echo "$first"
jq -e '.from_cache == false' <<<"$first" >/dev/null

echo "--- api ask (повтор, ожидается from_cache: true)"
second=$(ask)
echo "$second"
jq -e '.from_cache == true' <<<"$second" >/dev/null

echo "--- metrics ai-inference"
metrics=$(kubectl get --raw "/api/v1/namespaces/$NS/services/ai-inference:8000/proxy/metrics")
grep -q '^ai_requests_total' <<<"$metrics"
grep -q '^ai_cache_requests_total' <<<"$metrics"
grep -q '^ai_upstream_requests_total' <<<"$metrics"
grep -q '^ai_upstream_latency_seconds' <<<"$metrics"

echo "--- prometheus target ai-inference (up)"
for _ in $(seq 1 40); do
  targets=$(kubectl get --raw "/api/v1/namespaces/$NS/services/prometheus-server:9090/proxy/api/v1/targets")
  if jq -e '.data.activeTargets[] | select(.labels.job == "ai-inference" and .health == "up")' <<<"$targets" >/dev/null; then
    break
  fi
  sleep 5
done
jq -e '.data.activeTargets[] | select(.labels.job == "ai-inference" and .health == "up")' <<<"$targets" >/dev/null

echo "--- grafana"
curl -fsS -o /dev/null -w "%{http_code}\n" "$BASE/grafana/api/health"
kubectl -n "$NS" get configmap grafana-dashboards -o jsonpath='{.data.ai-inference\\.json}' | grep -q '"uid":"ai-inference"'
