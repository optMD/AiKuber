#!/usr/bin/env bash
set -euo pipefail

NS=niaspo

kubectl get namespace "$NS" >/dev/null 2>&1 || kubectl create namespace "$NS"

create_once() {
  local name=$1
  shift
  kubectl -n "$NS" get secret "$name" >/dev/null 2>&1 || kubectl -n "$NS" create secret generic "$name" "$@"
}

create_once postgres-credentials --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 16)"
create_once grafana-admin --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -hex 12)"
