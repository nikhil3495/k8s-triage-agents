#!/usr/bin/env bash
# One-time: let GitHub Actions in nikhil3495/k8s-triage-agents deploy via
# Workload Identity Federation (no service-account JSON keys).
set -euo pipefail
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project)}"
GH_REPO="${GH_REPO:-nikhil3495/k8s-triage-agents}"
POOL=github; PROVIDER=github-oidc; DEPLOYER=gh-deployer
NUM=$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')
DSA="${DEPLOYER}@${PROJECT}.iam.gserviceaccount.com"

gcloud iam workload-identity-pools describe "$POOL" --location global --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools create "$POOL" --location global --project "$PROJECT" --display-name "GitHub"
gcloud iam workload-identity-pools providers describe "$PROVIDER" --workload-identity-pool "$POOL" \
  --location global --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project "$PROJECT" \
    --location global --workload-identity-pool "$POOL" --issuer-uri https://token.actions.githubusercontent.com \
    --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository" \
    --attribute-condition "assertion.repository=='${GH_REPO}'"
gcloud iam service-accounts describe "$DSA" --project "$PROJECT" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$DEPLOYER" --project "$PROJECT" --display-name "GitHub deployer"
for role in roles/run.admin roles/cloudbuild.builds.editor roles/artifactregistry.writer \
            roles/storage.admin roles/iam.serviceAccountUser roles/serviceusage.serviceUsageConsumer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$DSA" --role "$role" --condition None --quiet >/dev/null
done
gcloud iam service-accounts add-iam-policy-binding "$DSA" --project "$PROJECT" \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/${NUM}/locations/global/workloadIdentityPools/${POOL}/attribute.repository/${GH_REPO}" >/dev/null
WIF="projects/${NUM}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}"
gh variable set GCP_PROJECT --body "$PROJECT" --repo "$GH_REPO"
gh variable set GCP_WIF_PROVIDER --body "$WIF" --repo "$GH_REPO"
gh variable set GCP_DEPLOY_SA --body "$DSA" --repo "$GH_REPO"
echo "WIF ready: $WIF"
