## SGR Production Deployment Guide

**Last Updated:** September 2024  
**Status:** Production-Ready (Docker)

---

### Table of Contents

1. [Quick Start](#quick-start)
2. [Architecture](#architecture)
3. [Environment Configuration](#environment-configuration)
4. [Local Development](#local-development)
5. [Production Deployment](#production-deployment)
6. [Kubernetes Deployment](#kubernetes-deployment)
7. [Health Checks & Monitoring](#health-checks--monitoring)
8. [Backup & Restore](#backup--restore)
9. [Troubleshooting](#troubleshooting)
10. [Crash Recovery](#crash-recovery)
11. [Security](#security)

---

### Quick Start

#### Docker Compose (Local/Dev)

```bash
# Development with hot reload
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d

# Production
docker compose -f docker/docker-compose.prod.yml up -d
```

#### Access Services

```
API:        http://localhost:8000
Swagger:    http://localhost:8000/docs
Grafana:    http://localhost:3001  (admin / sgr_grafana_dev)
Prometheus: http://localhost:9090
```

---

### Architecture

#### Container Layout

```
┌─────────────────────────────────────────┐
│           Load Balancer / Ingress        │
│          (Reverse Proxy, optional)       │
└────────┬──────────────┬──────────────────┘
         │              │
    ┌────▼───┐      ┌───▼────┐
    │   API   │      │ Grafana│
    │ :8000   │      │ :3001  │
    └────┬───┘      └───┬────┘
         │              │
    ┌────▼──────────────▼────┐
    │   SGR Internal Network  │
    │   (172.28.0.0/16)       │
    │                         │
    │ ┌──────┐  ┌────────┐   │
    │ │Worker│  │Prometh.│   │
    │ └──┬───┘  └────────┘   │
    │    │                    │
    │ ┌──▼────────────────┐   │
    │ │  PostgreSQL       │   │
    │ │  Redis            │   │
    │ └───────────────────┘   │
    └────────────────────────┘
```

#### Separation of Concerns

- **API Container**: REST + WebSocket (stateless, horizontally scalable)
- **Worker Container**: Trading Engine + Orchestrator (state per instance, careful restart)
- **PostgreSQL**: Single source of truth (shared, high availability in production)
- **Redis**: Event Bus + Cache (shared, failover via sentine l optional)

---

### Environment Configuration

#### Files

```
.env.example         - Template (commited to repo)
.env                 - Local development (gitignored)
.env.prod.example    - Production template (commited)
.env.prod            - Production config (gitignored, secret)
```

#### Key Variables

```bash
# Database
DB_HOST=postgres
DB_USER=sgr
DB_PASSWORD=STRONG_PASSWORD_HERE
DB_NAME=sgr

# Redis
REDIS_HOST=redis
REDIS_PORT=6379

# Trading Mode (CRITICAL)
TRADING_MODE=paper        # Default (always)
# TRADING_MODE=live       # ONLY after full validation

# Exchange Credentials (NEVER in default .env)
PIONEX_LIVE_API_KEY=      # Keep empty
PIONEX_LIVE_API_SECRET=   # Keep empty

# Risk Limits
RISK_MAX_PORTFOLIO_DRAWDOWN=0.15
RISK_DAILY_LOSS_LIMIT=0.05
RISK_MAX_SINGLE_POSITION_PCT=0.05
```

#### Secret Management

**Development**: Use `.env` file (gitignored)

**Production**: 

- **Kubernetes**: Use Kubernetes Secrets
  ```bash
  kubectl create secret generic sgr-secrets \
    --from-literal=DB_PASSWORD=xxx \
    --from-literal=REDIS_PASSWORD=yyy
  ```

- **Docker Swarm**: Use Docker Secrets
  ```bash
  echo "password" | docker secret create db_password -
  ```

- **AWS**: Use Secrets Manager / Parameter Store
  ```bash
  aws secretsmanager create-secret \
    --name sgr/prod/db-password \
    --secret-string "xxx"
  ```

---

### Local Development

#### Setup

```bash
# 1. Clone repo
git clone https://github.com/gordon365-tech/sgr.git
cd sgr

# 2. Copy .env
cp .env.example .env

# 3. Start stack
docker compose -f docker/docker-compose.yml -f docker/docker-compose.dev.yml up -d

# 4. Verify
docker compose ps
curl http://localhost:8000/health
```

#### Hot Reload

Source code changes are automatically reflected:

```bash
docker compose logs -f api    # Watch API logs
# Edit sgr/api/main.py → saved → Uvicorn reloads automatically
```

#### Database Migrations

```bash
# Apply migrations inside container
docker compose exec api alembic upgrade head

# Create new migration
docker compose exec api alembic revision --autogenerate -m "describe change"
```

---

### Production Deployment

#### Pre-Flight Checklist

- [ ] `.env.prod` configured with real passwords/keys
- [ ] TRADING_MODE=paper (confirmed!)
- [ ] Database backups enabled
- [ ] Monitoring alerts configured
- [ ] Kill switch tested
- [ ] Network security validated
- [ ] No test credentials in secrets

#### Deployment Steps

```bash
# 1. Build images
docker compose -f docker/docker-compose.prod.yml build

# 2. Test locally first
docker compose -f docker/docker-compose.prod.yml up -d

# 3. Run health checks
curl -f http://localhost:8000/health/ready || exit 1
curl -f http://localhost:8000/health/trading || exit 1

# 4. Production deployment (via CI/CD or manual)
docker compose -f docker/docker-compose.prod.yml down
docker compose -f docker/docker-compose.prod.yml up -d

# 5. Verify
docker compose logs -f api
```

#### Rolling Updates

```bash
# Update API without interrupting worker
docker compose -f docker/docker-compose.prod.yml up -d api

# Update Worker (careful!)
# Workers hold state - graceful shutdown is critical
docker compose -f docker/docker-compose.prod.yml up -d worker
```

---

### Kubernetes Deployment

#### Prerequisites

```bash
# 1. EKS Cluster
aws eks create-cluster --name sgr-prod ...

# 2. ECR Registry
aws ecr create-repository --repository-name sgr-api
aws ecr create-repository --repository-name sgr-worker
```

#### Deploy

```bash
# 1. Create namespace
kubectl create namespace sgr-prod

# 2. Create secrets
kubectl create secret generic sgr-secrets \
  --from-literal=DB_PASSWORD=xxx \
  --from-literal=REDIS_PASSWORD=yyy \
  -n sgr-prod

# 3. Apply manifests
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/postgres-statefulset.yaml
kubectl apply -f k8s/redis-statefulset.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/ingress.yaml

# 4. Verify
kubectl get pods -n sgr-prod
kubectl logs -f deployment/sgr-api -n sgr-prod
```

#### Helm (Alternative)

```bash
# Using Helm Charts
helm install sgr ./helm/sgr \
  --namespace sgr-prod \
  --create-namespace \
  -f helm/sgr/values-prod.yaml \
  --set-string postgres.password=$(openssl rand -base64 32)
```

---

### Health Checks & Monitoring

#### Health Endpoints

**Liveness** (Is process alive?)
```bash
GET /health/live
→ 200 OK {status: "alive"}
```

**Readiness** (Can accept traffic?)
```bash
GET /health/ready
→ 200 OK if db_connected && redis_connected
→ 503 if not ready (remove from load balancer)
```

**Trading** (Is trading safe?)
```bash
GET /health/trading
→ 200 OK if kill_switch_inactive && recovery_done && exchange_ok
→ 503 if trading_disabled
```

#### Prometheus Metrics

Key metrics to monitor:

```prometheus
# Order metrics
sgr_orders_submitted_total
sgr_orders_filled_total
sgr_orders_rejected_total
sgr_orders_duplicate_blocked_total

# Risk metrics
sgr_kill_switch_active
sgr_portfolio_drawdown
sgr_risk_rejected_total

# Reconciliation
sgr_reconciliation_runs_total
sgr_reconciliation_discrepancies_found

# Performance
sgr_execution_latency_seconds
sgr_order_latency_seconds
```

#### Grafana Dashboards

Pre-configured dashboards:

- **Portfolio Overview**: Value, PnL, positions
- **Risk Dashboard**: Drawdown, heat, limits
- **Trading Activity**: Orders, fills, rejections
- **Infrastructure**: CPU, memory, network

Access: http://localhost:3001

---

### Backup & Restore

#### PostgreSQL Backup

```bash
# Manual backup
docker compose exec postgres pg_dump -U sgr sgr > backup.sql

# Automated (via cronjob in k8s/postgres-backup-cronjob.yaml)
kubectl apply -f k8s/postgres-backup-cronjob.yaml

# Upload to S3
aws s3 cp backup.sql s3://sgr-backups/$(date +%Y%m%d).sql
```

#### Restore

```bash
# Restore from backup
docker compose exec -T postgres psql -U sgr sgr < backup.sql

# Verify
docker compose exec postgres psql -U sgr -d sgr -c "\dt"
```

---

### Troubleshooting

#### API Won't Start

```bash
# Check logs
docker compose logs api

# Common issues:
# 1. Database not ready → wait for PostgreSQL healthcheck
# 2. Redis not connected → check Redis logs
# 3. Port already in use → change port or stop other service
```

#### High Memory

```bash
docker stats sgr-api
# If > 2GB: memory leak or large dataset
# → Check market data cache size
# → Review feature store retention policy
```

#### Orders Not Executing

```bash
# Check trading health
curl http://localhost:8000/health/trading

# Check logs
docker compose logs worker

# Possible issues:
# - Kill switch is active → reset via API
# - Risk limits breached → adjust config
# - Exchange offline → check exchange status
```

#### Reconciliation Issues

```bash
# Manual reconciliation
curl -X POST http://localhost:8000/api/v1/reconciliation/reconcile

# Check discrepancies
curl http://localhost:8000/api/v1/reconciliation/discrepancies
```

---

### Crash Recovery

#### Automatic Recovery (Built-in)

On Container Restart:

1. Database connection
2. Position restoration from DB
3. Open order recovery
4. Strategy re-activation
5. Risk state recalculation
6. Ready for trading (if all checks pass)

#### Manual Recovery

```bash
# If automatic recovery fails:
# 1. Check database state
docker compose exec postgres psql -U sgr -d sgr -c "SELECT * FROM positions;"

# 2. Check orders
docker compose exec postgres psql -U sgr -d sgr -c "SELECT * FROM orders WHERE status='pending';"

# 3. Run reconciliation
curl -X POST http://localhost:8000/api/v1/reconciliation/reconcile

# 4. Reset kill switch if needed (CAREFUL!)
curl -X POST http://localhost:8000/api/v1/system/kill-switch/reset
```

---

### Security

#### Network

- PostgreSQL: NOT exposed (internal only)
- Redis: NOT exposed (internal only)
- API: Behind reverse proxy (TLS termination)
- Metrics: Restricted access (firewall rule)

#### Credentials

- NO secrets in Docker images
- NO secrets in git commits
- Environment variables or Secret Store
- Rotate credentials regularly

#### Access Control

- API authentication: JWT tokens
- Rate limiting: Per-user, per-endpoint
- Audit logging: All sensitive operations
- Network policies: Deny-all, explicit allow

#### Compliance

- TLS/HTTPS: Required in production
- Data encryption: At-rest (if sensitive) and in-transit
- Log retention: 30 days minimum
- Backup retention: 90 days minimum

---

### Monitoring & Alerting

#### Key Alerts

```yaml
# High drawdown
- alert: HighPortfolioDrawdown
  expr: sgr_portfolio_drawdown > 0.10
  for: 5m
  
# Kill switch activated
- alert: KillSwitchActive
  expr: sgr_kill_switch_active == 1
  for: 1m

# Reconciliation failures
- alert: ReconciliationFailure
  expr: rate(sgr_reconciliation_failures_total[5m]) > 0
  
# Duplicate orders detected
- alert: DuplicateOrderDetected
  expr: rate(sgr_orders_duplicate_blocked_total[5m]) > 0
```

#### Notifications

- Slack: Critical alerts
- Email: Daily summary
- Telegram: Real-time trading alerts (optional)

---

### Performance Tuning

#### Database

```sql
-- Connection pooling
max_connections = 200

-- Memory tuning
shared_buffers = 25% of RAM
effective_cache_size = 75% of RAM
work_mem = (total_ram / max_connections) / 2
```

#### API (Uvicorn)

```bash
# Workers = 2-4 per CPU core
--workers 4

# Disable access logs in production
--no-access-log
```

#### Redis

```bash
# Memory limit
--maxmemory 512mb
--maxmemory-policy allkeys-lru
```

---

### Support & Debugging

See [Troubleshooting](#troubleshooting) section above.

For architecture decisions, see `docs/ADR.md`.

For API reference, see `docs/API.md`.
