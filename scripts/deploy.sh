#!/usr/bin/env bash
# One-command Cloud Run deployment (replay mode). Idempotent: safe to re-run.
#
#   gcloud auth login && gcloud config set project <PROJECT_ID>
#   export GEMINI_API_KEY=...            # https://aistudio.google.com/apikey
#   ./scripts/deploy.sh
#
# Creates ONLY: enabled APIs, one Artifact Registry repo, 4 Secret Manager
# secrets, one runtime service account, one Cloud Run service (min 0 / max 2
# instances, 1 vCPU / 1 GiB) and one Vertex AI Agent Engine used purely as the
# session store (no idle compute). No GKE, no Cloud SQL.
set -euo pipefail

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-asia-south1}"
VERTEX_LOCATION="${VERTEX_LOCATION:-us-central1}"   # Agent Engine sessions region
SERVICE="${SERVICE:-k8s-triage-agents}"
REPO="${AR_REPO:-triage}"
SA_NAME="triage-runtime"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"
SECRETS_FILE="${SECRETS_FILE:-$HOME/k8s-triage-agents-secrets.txt}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:$(git rev-parse --short=12 HEAD)"

[[ -n "$PROJECT" ]] || { echo "No GCP project: run 'gcloud config set project <id>'"; exit 1; }
[[ -n "${GEMINI_API_KEY:-}" ]] || { echo "Set GEMINI_API_KEY"; exit 1; }
gcloud auth print-access-token >/dev/null || { echo "Run 'gcloud auth login'"; exit 1; }
echo ">> project=$PROJECT region=$REGION image=$IMAGE"

echo ">> enabling APIs"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudtrace.googleapis.com cloudbuild.googleapis.com \
  monitoring.googleapis.com logging.googleapis.com aiplatform.googleapis.com --project "$PROJECT"

echo ">> artifact registry"
gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --project "$PROJECT" --description "k8s-triage-agents images"

echo ">> runtime service account (least privilege)"
gcloud iam service-accounts describe "$SA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --project "$PROJECT" --display-name "k8s-triage-agents runtime"
for role in roles/cloudtrace.agent roles/monitoring.metricWriter roles/logging.logWriter \
            roles/secretmanager.secretAccessor roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition None --quiet >/dev/null
done

echo ">> session store: Vertex AI Agent Engine (sessions only)"
ENGINE_ID="${AGENT_ENGINE_ID:-}"
if [[ -z "$ENGINE_ID" ]]; then
  TOKEN=$(gcloud auth print-access-token)
  BASE="https://${VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT}/locations/${VERTEX_LOCATION}/reasoningEngines"
  ENGINE_ID=$(curl -fsS -H "Authorization: Bearer $TOKEN" "$BASE" |
    python3 -c "import json,sys; d=json.load(sys.stdin); print(next((e['name'].split('/')[-1] for e in d.get('reasoningEngines',[]) if e.get('displayName')=='k8s-triage-sessions'),''))")
  if [[ -z "$ENGINE_ID" ]]; then
    OP=$(curl -fsS -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      "$BASE" -d '{"displayName":"k8s-triage-sessions","description":"ADK session store for k8s-triage-agents"}')
    ENGINE_ID=$(echo "$OP" | python3 -c "import json,sys; print(json.load(sys.stdin)['name'].split('/')[5])")
  fi
fi
SESSION_DB_URL="vertexai://${PROJECT}/${VERTEX_LOCATION}/${ENGINE_ID}"
echo "   $SESSION_DB_URL"

echo ">> secrets"
umask 077
if [[ ! -f "$SECRETS_FILE" ]]; then
  {
    echo "# k8s-triage-agents secrets - generated $(date -u +%FT%TZ). Keep private."
    echo "TRIAGE_API_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
    echo "APPROVER_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
  } > "$SECRETS_FILE"
fi
# shellcheck disable=SC1090
source <(grep -E '^(TRIAGE_API_KEY|APPROVER_TOKEN)=' "$SECRETS_FILE")
put_secret() {  # name value
  if gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    printf '%s' "$2" | gcloud secrets versions add "$1" --data-file=- --project "$PROJECT" >/dev/null
  else
    printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy automatic --project "$PROJECT" >/dev/null
  fi
}
put_secret triage-gemini-api-key "$GEMINI_API_KEY"
put_secret triage-api-key "$TRIAGE_API_KEY"
put_secret triage-approver-token "$APPROVER_TOKEN"
put_secret triage-session-db-url "$SESSION_DB_URL"

echo ">> RAG index with Gemini embeddings (built locally; key never enters the image)"
GEMINI_API_KEY="$GEMINI_API_KEY" uv run python -m scripts.build_index

echo ">> build (Cloud Build) -> $IMAGE"
gcloud builds submit --project "$PROJECT" --region "$REGION" --tag "$IMAGE" --ignore-file .dockerignore .

echo ">> deploy Cloud Run"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --service-account "$SA" --cpu 1 --memory 1Gi --min-instances 0 --max-instances 2 \
  --concurrency 20 --timeout 600 --allow-unauthenticated \
  --set-env-vars "CLUSTER_MODE=replay,TRACE_EXPORTER=gcp,ENABLE_CLOUD_MONITORING=true,GCP_PROJECT=${PROJECT},GOOGLE_CLOUD_PROJECT=${PROJECT},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION}" \
  --set-secrets "GEMINI_API_KEY=triage-gemini-api-key:latest,TRIAGE_API_KEY=triage-api-key:latest,APPROVER_TOKEN=triage-approver-token:latest,SESSION_DB_URL=triage-session-db-url:latest"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
{ echo "SERVICE_URL=$URL"; echo "SESSION_DB_URL=$SESSION_DB_URL"; } >> "$SECRETS_FILE"
echo ">> deployed: $URL   (secrets: $SECRETS_FILE)"
echo ">> smoke test"
API_URL="$URL" TRIAGE_API_KEY="$TRIAGE_API_KEY" APPROVER_TOKEN="$APPROVER_TOKEN" \
  GCP_PROJECT="$PROJECT" ./scripts/smoke_test.sh
