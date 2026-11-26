# SPEC-PLT-001: Configuration, secrets and deployment
Status: implemented     Owner: platform     Version: 1.0

## Context
Run locally with one command, and deploy to Cloud Run without leaking secrets.

## Requirements
- **REQ-PLT-001.1** [MUST] Secrets SHALL come only from environment variables (`.env` locally) and SHALL be excluded from the image and the repository.
- **REQ-PLT-001.2** [SHOULD] The service SHALL run on Cloud Run as a dedicated service account with no key file, with env built from `.env` only. (Evidence: `scripts/deploy.sh`)
- **REQ-PLT-001.3** [SHOULD] `scripts/check_env.py` SHALL verify the Gemini key with a real text call and a real Live session. (Evidence: `scripts/check_env.py`)
