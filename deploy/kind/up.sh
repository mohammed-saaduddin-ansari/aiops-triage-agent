#!/usr/bin/env bash
# Create a kind cluster with Prometheus/Alertmanager, Loki, the triage agent and
# a breakable demo app. Needs: kind, kubectl, helm, docker.
#
# Set ANTHROPIC_API_KEY before running to use Claude; otherwise the FakeLLM runs.
set -euo pipefail

CLUSTER=aiops-triage
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
KPS_VERSION=93.1.1
LOKI_VERSION=7.5.0

for bin in kind kubectl helm docker; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

if ! kind get clusters | grep -qx "$CLUSTER"; then
  kind create cluster --config "$ROOT/deploy/kind/cluster.yaml"
fi
kubectl config use-context "kind-$CLUSTER"

echo ">> kube-prometheus-stack"
helm upgrade --install kps kube-prometheus-stack \
  --repo https://prometheus-community.github.io/helm-charts --version "$KPS_VERSION" \
  --namespace monitoring --create-namespace \
  -f "$ROOT/deploy/kind/values/kube-prometheus-stack.yaml" --wait --timeout 10m

echo ">> loki"
helm upgrade --install loki loki \
  --repo https://grafana.github.io/helm-charts --version "$LOKI_VERSION" \
  --namespace monitoring -f "$ROOT/deploy/kind/values/loki.yaml" --wait --timeout 10m

echo ">> triage agent"
docker build -t aiops-triage-agent:dev "$ROOT"
kind load docker-image aiops-triage-agent:dev --name "$CLUSTER"
kubectl apply -k "$ROOT/deploy/kubernetes"
APPROVAL_TOKEN="${TRIAGE_APPROVAL_TOKEN:-$(openssl rand -hex 16)}"
secret_args=(--from-literal=TRIAGE_APPROVAL_TOKEN="$APPROVAL_TOKEN")
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  secret_args+=(--from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY")
  kubectl -n triage patch configmap triage-agent --type merge -p '{"data":{"TRIAGE_LLM_PROVIDER":"anthropic"}}'
fi
kubectl -n triage create secret generic triage-agent "${secret_args[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n triage rollout restart deployment/triage-agent
kubectl -n triage rollout status deployment/triage-agent --timeout 3m

echo ">> demo app"
kubectl apply -f "$ROOT/deploy/kind/demo-app.yaml"
kubectl -n demo create configmap demo-app-code --from-file="$ROOT/deploy/demo-app/app.py" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n demo rollout status deployment/demo-app --timeout 3m

cat <<MSG

Ready. In another terminal:
  kubectl -n triage port-forward svc/triage-agent 8080:8080

Break something (each creates a new rollout revision, as a bad deploy would):
  make kind-break-crash    # missing config -> CrashLoopBackOff
  make kind-break-leak     # memory leak -> OOMKilled
  make kind-break-errors   # downstream refused -> 5xx
Then, after a couple of minutes:
  curl -s localhost:8080/triages | python -m json.tool

Approval token (also in secret triage/triage-agent): $APPROVAL_TOKEN
MSG
