# MISO ELT Service

An ELT pipeline that ingests MISO real-time fuel mix data into a PostgreSQL database and exposes it via a secure read-only API.

---

## Architecture Overview

```
EventBridge Scheduler (1/min)
        │
        ▼
  ECS Fargate Task ──── [Worker Container] ──► MISO API
        │                  python -m src.ingestion.worker --once
        │
        ▼
  RDS PostgreSQL (private subnet)
     schema: miso
     ├── dim_fuel_category
     ├── fact_fuel_mix
     └── ingestion_run
        │
        ▼
  ECS Fargate Service ── [API Container]
        │                  uvicorn src.api.app:app
        ▼
  Application Load Balancer (HTTPS)
        │
        ▼
  External Consumer (Bearer token auth)

  CloudWatch ◄──────── Both containers emit structured JSON logs
  SNS Alerts ◄──────── Worker publishes custom metrics on success/failure
```

### Component Summary

| Component | Purpose |
|-----------|---------|
| **Worker** | Fetches MISO API, upserts to Postgres, writes ingestion audit row, publishes CW metrics |
| **API** | FastAPI read-only service, connects as `miso_readonly` role, Bearer token auth |
| **RDS** | PostgreSQL 16, private subnet, encrypted, no public access |
| **ALB** | Terminates HTTPS, health-checks `/health`, forwards to ECS |
| **EventBridge Scheduler** | Triggers worker ECS task every minute |
| **Secrets Manager** | Stores DB password (RDS-managed rotation) and API key |
| **CloudWatch** | Logs, custom metrics, alarms, ops dashboard |
| **SNS** | Email alerts on ingestion failure or stale data |

---

## Data Model

Star schema in the `miso` PostgreSQL schema:

```sql
-- Dimension: fuel types (Coal, Wind, Nuclear, ...)
miso.dim_fuel_category
  id             SERIAL PRIMARY KEY
  category_name  VARCHAR(100) UNIQUE NOT NULL
  is_renewable   BOOLEAN NOT NULL DEFAULT false
  created_at     TIMESTAMPTZ

-- Fact: one row per (interval, fuel_type)
miso.fact_fuel_mix
  id               BIGSERIAL PRIMARY KEY
  interval_est_utc TIMESTAMPTZ NOT NULL   -- UTC-normalised from MISO EST
  fuel_category_id INTEGER → dim_fuel_category(id)
  act_mw           NUMERIC(12,2)          -- MW generated (can be negative)
  total_mw         NUMERIC(12,2)          -- Grid total from API envelope
  ingested_at      TIMESTAMPTZ            -- Write timestamp
  raw_ref_id       TEXT                   -- "21-Jun-2026 - Interval 02:10 EST"

  UNIQUE (interval_est_utc, fuel_category_id)   -- natural key for idempotency

-- Audit: one row per ingestion execution
miso.ingestion_run
  id               BIGSERIAL PRIMARY KEY
  started_at       TIMESTAMPTZ
  finished_at      TIMESTAMPTZ
  status           VARCHAR(20)  -- 'success' | 'failure' | 'skipped'
  rows_upserted    INTEGER
  error_message    TEXT
  interval_est_utc TIMESTAMPTZ
  raw_ref_id       TEXT
```

### Why star schema?
The fuel-type dimension is small (8 rows) and slowly-changing. A star schema avoids repeating string category names in every fact row, keeps fact-table rows narrow (better scan performance), and gives a clean home for dimension attributes like `is_renewable` that operators may want to update without touching fact data.

### Idempotency
Every load uses `INSERT … ON CONFLICT (interval_est_utc, fuel_category_id) DO UPDATE`. Running the same API snapshot 10 times produces exactly the same database state as running it once.

### Timezone handling
MISO reports all timestamps as EST (UTC-5, fixed offset — not EDT). We always convert to UTC before persisting, avoiding DST ambiguity entirely.

---

## Security Model

| Layer | Control |
|-------|---------|
| Network | RDS in private subnets; no public IP. Only ECS tasks can reach port 5432. |
| DB credentials | Stored in Secrets Manager. Injected as env vars at ECS task start. Never in code or Terraform state. |
| DB roles | Ingestion worker connects as `miso_app` (INSERT/UPDATE). API connects as `miso_readonly` (SELECT only, enforced at the Postgres level). |
| API auth | Bearer token (48-char random, stored in Secrets Manager). Compared with `secrets.compare_digest` to prevent timing attacks. |
| Rate limiting | 60 req/min per IP on all endpoints; 30 req/min on `/history` (via slowapi). Returns 429 on breach. |
| Query abuse | History and summary endpoints reject date ranges exceeding 31 days — prevents full-table scans. |
| Security headers | Every response includes `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `X-XSS-Protection`, `Referrer-Policy`, and `Cache-Control: no-store`. |
| Error responses | FastAPI's default 500 handler returns `{"detail": "Internal Server Error"}` — no stack traces or DB error strings are exposed. |
| Docs | `/docs` and `/openapi.json` are disabled in `production` environment. |
| Dependency scanning | CI runs `pip-audit` on every push to catch known CVEs in dependencies. |

---

## Monitoring & Alerting

### CloudWatch Alarms

| Alarm | Trigger | Action |
|-------|---------|--------|
| `IngestionFailure` | Any failure metric in 5 min | SNS email |
| `StaleData` | No success in 10 min (treat_missing=breaching) | SNS email |
| `API5xx` | >10 ALB 5xx errors in 3 min | SNS email |
| `RDSHighCPU` | CPU >80% for 15 min | SNS email |
| `RDSLowStorage` | Free storage <2 GB | SNS email |
| `ECSNoRunningTasks` | API service has 0 running tasks for 3 min | SNS email |
| `WorkerLogErrors` | Worker logs ≥3 ERROR lines in 5 min | SNS email |

### Custom Metrics (namespace: `MISO/ELT`)
- `IngestionSuccess` — Count (1 per successful run)
- `IngestionFailure` — Count (1 per failed run)
- `RowsUpserted` — Count of fact rows written
- `MISOAPILatencyMs` — p95 latency to MISO API
- `LastSuccessfulIngestionAgeSeconds` — staleness gauge

### CloudWatch Dashboard
`miso-elt-production-ops` — ingestion success/failure, rows upserted, API latency, RDS CPU, all active alarms.

---

## Local Development

### Prerequisites
- Docker + Docker Compose
- Python 3.12 (for running tests without Docker)

### Start everything locally

```bash
# Bring up Postgres, run migrations, run one ingestion, start API
docker-compose up

# One-shot ingestion only
docker-compose run --rm worker

# API only (after postgres + migrate have run)
docker-compose up api
```

API will be available at `http://localhost:8000`.

> **Note:** The examples below use `python -m json.tool` for pretty-printing, which requires no extra installation. If you have `jq` installed you can substitute `| jq .` instead.

```bash
# Health check (no auth)
curl http://localhost:8000/health

# Latest fuel mix
curl -s -H "Authorization: Bearer dev-api-key-change-in-production" \
  http://localhost:8000/api/v1/fuel-mix/latest | python -m json.tool

# History (last 20 intervals)
curl -s -H "Authorization: Bearer dev-api-key-change-in-production" \
  "http://localhost:8000/api/v1/fuel-mix/history?page_size=20" | python -m json.tool

# Summary stats
curl -s -H "Authorization: Bearer dev-api-key-change-in-production" \
  http://localhost:8000/api/v1/fuel-mix/summary | python -m json.tool

# Ingestion audit log
curl -s -H "Authorization: Bearer dev-api-key-change-in-production" \
  http://localhost:8000/api/v1/ingestion/status | python -m json.tool
```

### docker-compose setup notes

A couple of things to be aware of on a fresh clone:

**Readonly user name:** The login user created by `scripts/init_db.sql` is `miso_readonly_user`, not `miso_readonly` (which is the role). Make sure your `docker-compose.yml` has:
```yaml
DB_READONLY_USER: miso_readonly_user
```
for both the `api` and `worker` services.

**Suppress boto3 metadata noise:** Locally there are no AWS credentials, so boto3 will print connection-refused tracebacks while searching for them. This is harmless — the worker handles it gracefully and continues — but you can silence it by adding to both `api` and `worker` environments:
```yaml
AWS_EC2_METADATA_DISABLED: "true"
```

### Run tests

```bash
# Unit tests (no Postgres required)
pip install -r requirements.txt
DB_HOST=localhost DB_PASSWORD=x DB_READONLY_PASSWORD=x \
API_KEY=test-api-key ENVIRONMENT=development SNS_ALERT_TOPIC_ARN="" \
python -m pytest tests/unit/ -v

# Integration tests (requires live Postgres — use docker-compose postgres service)
docker-compose up -d postgres
python -m pytest tests/integration/ -v
```

### Run ingestion manually (CLI)

```bash
# One-shot (exits after one fetch)
python -m src.ingestion.worker --once

# Daemon mode (polls every 60 s)
python -m src.ingestion.worker
```

---

## AWS Deployment

### Prerequisites
- AWS account with sufficient IAM permissions
- Terraform ≥ 1.7
- Docker
- AWS CLI configured

### First-time deploy

```bash
# 1. Create ECR repo and push initial image
cd terraform
terraform init
terraform apply -target=module.ecs.aws_ecr_repository.app -auto-approve

ECR_URL=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_URL
docker build -t $ECR_URL:latest .
docker push $ECR_URL:latest

# 2. Full apply
terraform apply \
  -var="ecr_image_uri=$ECR_URL:latest" \
  -var="alert_email=your@email.com"

# 3. Run DB migrations (one-time, or after schema changes)
# The CI pipeline does this automatically on every deploy.
# Manually:
CLUSTER=$(terraform output -raw ...)
aws ecs run-task --cluster $CLUSTER \
  --task-definition miso-elt-production-worker \
  --launch-type FARGATE \
  --network-configuration "..." \
  --overrides '{"containerOverrides":[{"name":"worker","command":["python","-m","alembic","upgrade","head"]}]}'
```

### Subsequent deploys (idempotent)
```bash
# CI does this automatically on push to main.
# Manually:
docker build -t $ECR_URL:$(git rev-parse --short HEAD) .
docker push $ECR_URL:$(git rev-parse --short HEAD)
terraform apply -var="ecr_image_uri=$ECR_URL:$(git rev-parse --short HEAD)"
```

### Retrieve the API key
```bash
aws secretsmanager get-secret-value \
  --secret-id $(terraform output -raw api_key_secret_arn) \
  --query SecretString --output text
```

### API base URL
```bash
echo "https://$(terraform output -raw alb_dns_name)"
```

---

## CI/CD Pipeline

GitHub Actions workflow (`.github/workflows/ci.yml`):

1. **On every push/PR**: lint (ruff), type-check (mypy), unit tests
2. **On PR to main**: Terraform plan posted as PR comment
3. **On merge to main**:
   - Build + push Docker image to ECR (tagged with commit SHA)
   - `terraform apply` with new image URI
   - Run Alembic migrations via ECS task
   - Force ECS rolling deployment
   - Wait for service stability

### Required GitHub Secrets
| Secret | Description |
|--------|-------------|
| `AWS_DEPLOY_ROLE_ARN` | IAM role ARN for OIDC auth (no long-lived keys) |
| `ECR_REPOSITORY_NAME` | ECR repo name (output from terraform) |
| `ALERT_EMAIL` | Email for SNS alarm subscriptions |
| `ECS_CLUSTER_NAME` | ECS cluster name |
| `API_SERVICE_NAME` | ECS API service name |
| `PRIVATE_SUBNET_ID` | Subnet for migration task |
| `APP_SG_ID` | Security group for migration task |

---

## Known Limitations

- **AWS not live-tested:** The Terraform and CI/CD pipeline are complete and correct, but the AWS deployment has not been executed against a live account (no AWS account was provisioned for this assessment). The local stack — Postgres, migrations, worker, and API — is fully functional and verified. See the AWS Deployment section for step-by-step instructions.

- **CloudWatch metrics and SNS alerts are no-ops locally:** Without AWS credentials the worker logs a `cloudwatch_put_metric_failed` warning and continues normally. No data is lost and ingestion succeeds. These work as intended when deployed to ECS with the appropriate IAM task role.

---

## Design Decisions & Trade-offs

### Why EventBridge Scheduler + ECS run-to-completion instead of a daemon?
- **Cost**: Fargate tasks only run for ~5 seconds per minute vs paying for an always-on container.
- **Reliability**: If the task crashes, EventBridge retries. A daemon that panics is just gone.
- **Simplicity**: No internal scheduler state to manage.
- **Trade-off**: ~5–10 second cold start per invocation. Acceptable since MISO data refreshes every 5 minutes in practice.

### Why not Lambda?
Lambda would work, but Fargate is easier to reason about for tasks that need a persistent DB connection, full Python environment, and predictable execution semantics. Lambda cold starts and the 15-minute timeout are non-issues here, but Fargate keeps the same container image for both the worker and API, reducing operational surface.

### Why `INSERT … ON CONFLICT DO UPDATE` instead of upsert-or-skip?
MISO occasionally revises values within the same 5-minute interval. Using `DO UPDATE` means we always have the latest ACT value, not the first one we saw.

### Why not partition `fact_fuel_mix` by month?
At one row per fuel type (8) per minute, we accumulate ~11,520 rows/day. A single table with the composite index `(fuel_category_id, interval_est_utc)` will handle years of data without needing partitioning. This can be revisited if query patterns change.

### Why ALB instead of API Gateway?
ALB + ECS is simpler operationally (one fewer AWS service, no stage management, no per-request pricing). API Gateway would add value if we needed request throttling per consumer, usage plans, or a managed auth layer — none of which are required here.

### Why not expose `/docs` in production?
Reducing the exposed surface area. The OpenAPI schema reveals endpoint structure, parameter names, and response shapes — information an attacker could use. For internal consumers, docs are available in development mode or can be generated from the schema file.