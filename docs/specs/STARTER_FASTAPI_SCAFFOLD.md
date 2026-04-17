# Starter FastAPI Scaffold

The production-ready starter implementation is available at:

- `server/fund/app.py`
- `server/fund/routers/`
- `server/fund/services/`
- `server/fund/repositories/`
- `server/fund/models.py`
- `server/fund/database.py`
- `server/fund_api.py` (compatibility wrapper)

## Run

```bash
python -m coherence_engine serve-fund --host 0.0.0.0 --port 8010
```

Or directly:

```bash
python -m coherence_engine.server.fund_api
```

Swagger UI:

- `http://localhost:8010/docs`

## Migrations (Alembic)

Run DB migrations before starting services in production:

```bash
alembic upgrade head
```

Create new migration revision:

```bash
alembic revision -m "describe change"
```

## Outbox Dispatcher Worker

Dispatch outbox events from DB to transport:

Kafka once:

```bash
python -m coherence_engine dispatch-outbox \
  --backend kafka \
  --kafka-bootstrap-servers localhost:9092 \
  --run-mode once
```

Redis loop:

```bash
python -m coherence_engine dispatch-outbox \
  --backend redis \
  --redis-url redis://localhost:6379/0 \
  --run-mode loop \
  --poll-seconds 2
```

SQS once:

```bash
python -m coherence_engine dispatch-outbox \
  --backend sqs \
  --sqs-queue-url https://sqs.us-east-1.amazonaws.com/123456789012/coherence-fund-events \
  --sqs-region us-east-1
```

Process scoring queue asynchronously:

```bash
python -m coherence_engine process-scoring-jobs \
  --run-mode loop \
  --max-jobs 100 \
  --poll-seconds 2 \
  --lease-seconds 120 \
  --retry-base-seconds 5
```

Replay dead-letter outbox rows:

```bash
python -m coherence_engine replay-outbox --all-failed --limit 100 --reset-attempts
```

Replay dead-letter scoring jobs:

```bash
python -m coherence_engine replay-scoring-jobs --all-failed --limit 100 --reset-attempts
```

## Implemented Endpoints

- `GET /health`
- `POST /applications`
- `POST /applications/{application_id}/interview-sessions`
- `POST /applications/{application_id}/score`
- `GET /applications/{application_id}/decision`
- `POST /applications/{application_id}/escalation-packet`

## Database Configuration

Set Postgres URL:

```bash
export COHERENCE_FUND_DATABASE_URL="postgresql+psycopg://postgres:postgres@localhost:5432/coherence_fund"
export COHERENCE_FUND_AUTO_CREATE_TABLES="false"
```

If not set, it defaults to a persistent local SQLite file:

`sqlite:///./coherence_fund.db`

## Notes

- Persistence is implemented via SQLAlchemy models and DB-backed repositories.
- Idempotency is persisted in `fund_idempotency_records`.
- Events are written to `fund_event_outbox`, validated against schema files in `docs/specs/schemas/events`, then dispatched by the worker.
- Policy evaluation follows `docs/specs/decision_policy_spec.md` defaults.
- Auth is DB-backed API-key by default (`fund_api_keys` table), with secret-manager bootstrap admin fallback.
- Bootstrap admin access can come from AWS/GCP/Vault secret manager via:
  - `COHERENCE_FUND_SECRET_MANAGER_PROVIDER`
  - `COHERENCE_FUND_BOOTSTRAP_ADMIN_SECRET_REF`
  - `COHERENCE_FUND_SECRET_MANAGER_STRICT_POLICY`
  - `COHERENCE_FUND_SECRET_MANAGER_STARTUP_ENFORCE`
- Secret-manager probe endpoint:
  - `GET /api/v1/secret-manager/ready`
- Admin key lifecycle routes:
  - `POST /api/v1/admin/api-keys`
  - `GET /api/v1/admin/api-keys`
  - `POST /api/v1/admin/api-keys/{key_id}/revoke`
  - `POST /api/v1/admin/api-keys/{key_id}/rotate`

