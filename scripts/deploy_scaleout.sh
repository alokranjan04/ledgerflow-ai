#!/usr/bin/env bash
# Scale-out topology on Cloud Run (ADR 0005). NOT run by default: it creates billable
# resources (Memorystore Basic 1 GB, about US$35/month, plus extra Cloud Run instances).
#
#   Customers ──HTTPS──▶ ledgerflow-ai (gateway, 1..N instances, session affinity)
#                           │  Direct VPC egress ──▶ Memorystore Redis (sessions, limits, lockouts)
#                           └─ HTTPS + Google ID token + internal token ──▶ ledgerflow-systems (private)
#
#   scripts/deploy_scaleout.sh            # provision + deploy both services
#   MAX_INSTANCES=10 scripts/deploy_scaleout.sh
#
# Secrets (Gemini key, admin token, internal token) come from Secret Manager, as in deploy.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-project-b2f8fd7f-3661-4447-8b6}"
ACCOUNT="${ACCOUNT:-tellyourjourney04@gmail.com}"
REGION="${REGION:-asia-south1}"
GATEWAY="${GATEWAY:-ledgerflow-ai}"
SYSTEMS="${SYSTEMS:-ledgerflow-systems}"
REDIS_INSTANCE="${REDIS_INSTANCE:-ledgerflow-state}"
NETWORK="${NETWORK:-default}"
SUBNET="${SUBNET:-default}"
MAX_INSTANCES="${MAX_INSTANCES:-5}"
A="--account=$ACCOUNT"
GW_SA="$GATEWAY-runtime@$PROJECT.iam.gserviceaccount.com"
SYS_SA="$SYSTEMS-runtime@$PROJECT.iam.gserviceaccount.com"
PY="${PY:-.venv/Scripts/python}"; [ -x "$PY" ] || PY=python3

echo "This creates billable resources (Memorystore + extra instances) in $PROJECT/$REGION."
read -r -p "Type 'yes' to continue: " OK; [ "$OK" = "yes" ] || exit 1

gcloud services enable redis.googleapis.com run.googleapis.com secretmanager.googleapis.com \
  compute.googleapis.com --project="$PROJECT" $A

echo "== shared internal token (Secret Manager)"
TOKEN_SECRET="$GATEWAY-internal-token"
if ! gcloud secrets describe "$TOKEN_SECRET" --project="$PROJECT" $A >/dev/null 2>&1; then
  gcloud secrets create "$TOKEN_SECRET" --replication-policy=automatic --project="$PROJECT" $A
  "$PY" -c 'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32))' | \
    gcloud secrets versions add "$TOKEN_SECRET" --data-file=- --project="$PROJECT" $A
fi
for SA_NAME in "$GATEWAY-runtime" "$SYSTEMS-runtime"; do
  gcloud iam service-accounts describe "$SA_NAME@$PROJECT.iam.gserviceaccount.com" --project="$PROJECT" $A >/dev/null 2>&1 || \
    gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" $A
  gcloud secrets add-iam-policy-binding "$TOKEN_SECRET" --member="serviceAccount:$SA_NAME@$PROJECT.iam.gserviceaccount.com" \
    --role=roles/secretmanager.secretAccessor --project="$PROJECT" $A >/dev/null
done

echo "== Memorystore Redis (private IP on $NETWORK)"
gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" --project="$PROJECT" $A >/dev/null 2>&1 || \
  gcloud redis instances create "$REDIS_INSTANCE" --size=1 --tier=basic --redis-version=redis_7_2 \
    --region="$REGION" --network="$NETWORK" --project="$PROJECT" $A
REDIS_HOST=$(gcloud redis instances describe "$REDIS_INSTANCE" --region="$REGION" --project="$PROJECT" $A --format='value(host)')

echo "== systems service (private: IAM invoker only)"
gcloud run deploy "$SYSTEMS" --source=. --region="$REGION" --project="$PROJECT" $A \
  --service-account="$SYS_SA" --no-allow-unauthenticated --port=8080 \
  --min-instances=1 --max-instances=1 --memory=1Gi --cpu=1 \
  --set-env-vars="SERVICE_ROLE=systems,LOG_JSON=1,GCP_PROJECT=$PROJECT" \
  --set-secrets="INTERNAL_TOKEN=$TOKEN_SECRET:latest" --quiet
SYS_URL=$(gcloud run services describe "$SYSTEMS" --region="$REGION" --project="$PROJECT" $A --format='value(status.url)')
gcloud run services add-iam-policy-binding "$SYSTEMS" --region="$REGION" --project="$PROJECT" $A \
  --member="serviceAccount:$GW_SA" --role=roles/run.invoker >/dev/null

echo "== gateway (autoscaled, Direct VPC egress to Redis)"
gcloud run deploy "$GATEWAY" --source=. --region="$REGION" --project="$PROJECT" $A \
  --service-account="$GW_SA" --allow-unauthenticated --port=8080 \
  --timeout=3600 --concurrency=250 --min-instances=1 --max-instances="$MAX_INSTANCES" \
  --memory=1Gi --cpu=1 --session-affinity --cpu-boost \
  --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only \
  --update-env-vars="SERVICE_ROLE=gateway,REDIS_URL=redis://$REDIS_HOST:6379/0,MOCK_BASE_URL=$SYS_URL/mock,SYSTEMS_AUDIENCE=$SYS_URL" \
  --update-secrets="INTERNAL_TOKEN=$TOKEN_SECRET:latest" --quiet

URL=$(gcloud run services describe "$GATEWAY" --region="$REGION" --project="$PROJECT" $A --format='value(status.url)')
echo "== verify"
curl -fsS "$URL/readyz"; echo
curl -s -o /dev/null -w "systems service from the internet: HTTP %{http_code} (expect 403)\n" "$SYS_URL/mock/crm/customers/C1001"
echo "Gateway: $URL   Systems (private): $SYS_URL   Redis: $REDIS_HOST"
