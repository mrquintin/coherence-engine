# Secret Manager Rotation Runbook

This runbook covers secure API-key bootstrap and rotation for the fund backend using managed secrets (AWS/GCP/Vault).

## Goals

- Never store bootstrap/admin API keys in static env JSON.
- Ensure every rotation updates both DB key records and secret-manager secret versions.
- Keep rollback path explicit and auditable.

## Prerequisites

- DB migrations applied (`alembic upgrade head`).
- `COHERENCE_FUND_AUTH_MODE=db`.
- Secret manager provider configured:
  - `COHERENCE_FUND_SECRET_MANAGER_PROVIDER=aws|gcp|vault`
  - `COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY=true`
  - `COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE=true`
- Bootstrap admin secret reference configured:
  - `COHERENCE_FUND_BOOTSTRAP_ADMIN_ENABLED=true`
  - `COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF=<provider-ref>`

## Provider Credential Policy

- Strict mode disallows static cloud credentials by default.
- AWS: avoid `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in runtime env; use IAM role/workload identity.
- GCP: avoid `GOOGLE_APPLICATION_CREDENTIALS`; use workload identity.
- Vault: use HTTPS endpoint; supply token via file (`COHERENCE_FUND_VAULT_TOKEN_FILE`) where possible.

## Initial Bootstrap

1. Write bootstrap admin token to secret manager under the configured secret ref.
2. Start API and verify startup probe:
   - `GET /api/v1/secret-manager/ready` returns `status=ready` or `status=configured`.
3. Use bootstrap token only to mint DB-managed admin keys:
   - `python -m coherence_engine create-fund-api-key --label "ops-admin" --role admin --expires-in-days 30 --secret-ref "<admin-secret-ref>"`
4. Transition operators to DB-managed keys and minimize bootstrap token use.

## Scheduled Rotation Procedure

1. Pick key to rotate (`key_id`) and target secret ref.
2. Rotate and sync in one operation:
   - `python -m coherence_engine rotate-fund-api-key --key-id <key_id> --expires-in-days 30 --secret-ref "<secret-ref>"`
3. Confirm new key exists and old key is revoked:
   - `GET /api/v1/admin/api-keys`
4. Validate consumers can authenticate with new key.
5. Confirm audit events include:
   - `api_key_rotate`
   - `api_key_secret_synced` or `api_key_secret_synced_cli`

## Emergency Revocation

1. Revoke compromised key:
   - `python -m coherence_engine revoke-fund-api-key --key-id <key_id>`
2. Issue replacement key and sync to secret manager immediately.
3. Validate all services use replacement token.
4. Archive incident details with key IDs and timestamps.

## Startup and Runtime Health

- `GET /api/v1/secret-manager/ready`:
  - `ready`: provider reachable and active probe succeeded.
  - `configured`: provider configured but no active probe secret ref available.
  - `disabled`: provider disabled.
  - `failed`: policy or connectivity check failed (503).
- `GET /api/v1/ready` remains DB readiness only.

## Deployment Preflight

Run before deploy rollout:

```bash
python deploy/scripts/secret_manager_preflight.py --require-reachable
```

Useful non-prod override flags:

- `--allow-disabled`
- `--allow-non-strict`

