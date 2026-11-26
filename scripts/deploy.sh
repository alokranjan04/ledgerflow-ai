#!/usr/bin/env bash
# Deploy LedgerFlow AI to Cloud Run (keyless: the service runs AS a dedicated
# service account; no key files). Env vars come from .env ONLY.
#
#   scripts/deploy.sh               # build + deploy
#   scripts/deploy.sh --env-only    # push .env changes without a rebuild (~30 s)
#
# Override with env vars: PROJECT, ACCOUNT, REGION, SERVICE.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-project-b2f8fd7f-3661-4447-8b6}"
ACCOUNT="${ACCOUNT:-tellyourjourney04@gmail.com}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-ledgerflow-ai}"
A="--account=$ACCOUNT"
SA="$SERVICE-runtime@$PROJECT.iam.gserviceaccount.com"
PY="${PY:-.venv/Scripts/python}"; [ -x "$PY" ] || PY=python3
TMPD="$(mktemp -d)"; trap 'rm -rf "$TMPD"' EXIT

[ -f .env ] || { echo "No .env — copy .env.example and fill GEMINI_API_KEY"; exit 1; }

# Env YAML from .env only (PORT is reserved by Cloud Run; PUBLIC_URL is set after deploy).
"$PY" - "$TMPD/env.yaml" <<'EOF'
import sys, json
from dotenv import dotenv_values
v = {k: x.strip() for k, x in dotenv_values(".env").items() if x and x.strip()}
for k in ("PORT", "PUBLIC_URL", "DATA_DIR", "MOCK_BASE_URL"):
    v.pop(k, None)
open(sys.argv[1], "w").write("".join(f"{k}: {json.dumps(x)}\n" for k, x in v.items()) or "LEDGERFLOW_ENV: \"cloudrun\"\n")
print("env vars:", ", ".join(sorted(v)) or "(none)")
EOF

if [ "${1:-}" = "--env-only" ]; then
  gcloud run services update "$SERVICE" --region="$REGION" --project="$PROJECT" $A \
    --env-vars-file="$TMPD/env.yaml" --quiet
  URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" $A --format='value(status.url)')
  gcloud run services update "$SERVICE" --region="$REGION" --project="$PROJECT" $A --update-env-vars="PUBLIC_URL=$URL" --quiet
  echo "updated: $URL"; exit 0
fi

echo "== APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project="$PROJECT" $A

echo "== runtime service account"
gcloud iam service-accounts describe "$SA" --project="$PROJECT" $A >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SERVICE-runtime" --display-name="$SERVICE runtime" --project="$PROJECT" $A

echo "== deploy (source build)"
# Single instance on purpose: synthetic systems (SQLite) and sessions live in-process.
# The production path (Cloud SQL + Memorystore, N instances) is in docs/ARCHITECTURE.md.
gcloud run deploy "$SERVICE" --source=. --region="$REGION" --project="$PROJECT" $A \
  --service-account="$SA" --allow-unauthenticated --port=8080 \
  --timeout=3600 --concurrency=250 --min-instances=1 --max-instances=1 \
  --memory=1Gi --cpu=1 --session-affinity --cpu-boost \
  --env-vars-file="$TMPD/env.yaml" --quiet

URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" $A --format='value(status.url)')
gcloud run services update "$SERVICE" --region="$REGION" --project="$PROJECT" $A --update-env-vars="PUBLIC_URL=$URL" --quiet

echo "== verify"
curl -fsS "$URL/health"; echo
curl -fsS -X POST "$URL/answer" -d From=919800000001 | head -c 300; echo
echo "Deployed: $URL"
echo "Phone: point the Vobiz/Twilio answer URL (and hangup/fallback URLs) at $URL/answer (POST)."
