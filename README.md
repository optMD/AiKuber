# AiKuber

AiKuber is a local Kubernetes platform for operating an AI-backed service. The stack includes an nginx gateway, a static frontend, the `ai-inference` API, Redis, PostgreSQL, Prometheus and Grafana.

## Requirements

- Docker Desktop
- kind
- kubectl
- Helm
- jq
- an environment variable named `CLAUDEHUB_API_KEY`

## Start the platform

Run the deployment script from the repository root:

```bash
export CLAUDEHUB_API_KEY="your-api-key"
./infra/scripts/up.sh
```

The script creates or reuses the `kind-niaspo` cluster, builds the three local images, installs Prometheus and Grafana, applies the Kubernetes manifests and runs the smoke checks.

## Access the services

- Frontend and API gateway: http://localhost:8080/
- API health: http://localhost:8080/api/health
- Grafana: http://localhost:8080/grafana/

The Grafana dashboard is named `AI Inference · Operations`.

## Check the deployment

```bash
./infra/scripts/check.sh
kubectl --context kind-niaspo -n niaspo get pods
```

## Stop the local cluster

```bash
kind delete cluster --name niaspo
```
