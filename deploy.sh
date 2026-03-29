#!/bin/bash
# CrisisFlow — Cloud Run deployment
# Usage: ./deploy.sh <GCP_PROJECT_ID>
# Example: ./deploy.sh my-project-123

set -e

PROJECT=${1:?Usage: ./deploy.sh <GCP_PROJECT_ID>}
REGION="us-central1"
IMAGE="gcr.io/$PROJECT/crisisflow"

echo "=== CrisisFlow Cloud Run Deployment ==="
echo "Project: $PROJECT | Region: $REGION"

# ── 1. Authenticate & set project ─────────────────────────────────────────
gcloud config set project "$PROJECT"
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  containerregistry.googleapis.com

# ── 2. Build image in the cloud (no local Docker needed) ──────────────────
echo ""
echo "Building image with Cloud Build..."
gcloud builds submit --tag "$IMAGE" .

# ── 3. Deploy logistics specialist first (needed by main service) ──────────
echo ""
echo "Deploying logistics specialist..."
gcloud run deploy crisisflow-logistics \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 300 \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY:?Set GOOGLE_API_KEY env var}" \
  --set-env-vars "SNOWFLAKE_ACCOUNT=${SNOWFLAKE_ACCOUNT:-}" \
  --set-env-vars "SNOWFLAKE_USER=${SNOWFLAKE_USER:-}" \
  --set-env-vars "SNOWFLAKE_PASSWORD=${SNOWFLAKE_PASSWORD:-}" \
  --set-env-vars "SNOWFLAKE_DATABASE=${SNOWFLAKE_DATABASE:-}" \
  --set-env-vars "SNOWFLAKE_SCHEMA=${SNOWFLAKE_SCHEMA:-}" \
  --set-env-vars "SNOWFLAKE_WAREHOUSE=${SNOWFLAKE_WAREHOUSE:-}"

LOGISTICS_URL=$(gcloud run services describe crisisflow-logistics \
  --region "$REGION" --format "value(status.url)")
echo "Logistics URL: $LOGISTICS_URL"

# ── 4. Deploy main crisisflow service ──────────────────────────────────────
echo ""
echo "Deploying main CrisisFlow service..."
gcloud run deploy crisisflow-main \
  --image "$IMAGE" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --set-env-vars "GOOGLE_API_KEY=${GOOGLE_API_KEY}" \
  --set-env-vars "LOGISTICS_A2A_URL=${LOGISTICS_URL}" \
  --set-env-vars "SNOWFLAKE_ACCOUNT=${SNOWFLAKE_ACCOUNT:-}" \
  --set-env-vars "SNOWFLAKE_USER=${SNOWFLAKE_USER:-}" \
  --set-env-vars "SNOWFLAKE_PASSWORD=${SNOWFLAKE_PASSWORD:-}" \
  --set-env-vars "SNOWFLAKE_DATABASE=${SNOWFLAKE_DATABASE:-}" \
  --set-env-vars "SNOWFLAKE_SCHEMA=${SNOWFLAKE_SCHEMA:-}" \
  --set-env-vars "SNOWFLAKE_WAREHOUSE=${SNOWFLAKE_WAREHOUSE:-}"

MAIN_URL=$(gcloud run services describe crisisflow-main \
  --region "$REGION" --format "value(status.url)")

echo ""
echo "=== DEPLOYMENT COMPLETE ==="
echo "Main CrisisFlow:        $MAIN_URL"
echo "Logistics Specialist:   $LOGISTICS_URL"
echo ""
echo "Agent card:   $MAIN_URL/.well-known/agent.json"
echo "List apps:    $MAIN_URL/list-apps"
echo ""
echo "Test it:"
echo "  curl $MAIN_URL/list-apps"
