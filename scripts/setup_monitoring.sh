#!/usr/bin/env bash
# KPIs as code: log-based metrics from LedgerFlow's structured logs + a Cloud Monitoring dashboard.
# Durable and summed across instances (unlike the in-app /dashboard, which is per instance).
#
#   scripts/setup_monitoring.sh        # create or update metrics, create or replace the dashboard
#
# Cost: user-defined log-based metrics count toward Cloud Monitoring's chargeable metrics; at
# this volume they stay inside the monthly free allotment.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${PROJECT:-project-b2f8fd7f-3661-4447-8b6}"
ACCOUNT="${ACCOUNT:-tellyourjourney04@gmail.com}"
A="--account=$ACCOUNT"

gcloud services enable logging.googleapis.com monitoring.googleapis.com --project="$PROJECT" $A >/dev/null

for f in monitoring/metrics/*.json; do
  name=$(basename "$f" .json)
  if gcloud logging metrics describe "$name" --project="$PROJECT" $A >/dev/null 2>&1; then
    gcloud logging metrics update "$name" --config-from-file="$f" --project="$PROJECT" $A >/dev/null
    echo "updated metric $name"
  else
    gcloud logging metrics create "$name" --config-from-file="$f" --project="$PROJECT" $A >/dev/null
    echo "created metric $name"
  fi
done

# Replace an existing dashboard of the same name, so re-runs do not pile up copies.
EXISTING=$(gcloud monitoring dashboards list --project="$PROJECT" $A \
  --filter='displayName="LedgerFlow AI — KPIs"' --format='value(name)' 2>/dev/null || true)
for d in $EXISTING; do gcloud monitoring dashboards delete "$d" --project="$PROJECT" $A --quiet >/dev/null; done
gcloud monitoring dashboards create --config-from-file=monitoring/dashboard.json --project="$PROJECT" $A >/dev/null
echo "dashboard: https://console.cloud.google.com/monitoring/dashboards?project=$PROJECT"
