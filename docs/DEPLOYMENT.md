# Project SGR – Deployment Guide

## Environments

### Development
```bash
docker compose -f docker/docker-compose.yml up -d
# Runs: API (hot reload), Frontend, Postgres, Redis, Prometheus, Grafana
```

**Access:**
- API: http://localhost:8000
- Frontend: http://localhost:3000
- Docs: http://localhost:8000/docs
- Grafana: http://localhost:3001 (admin/sgr_grafana_dev)
- Prometheus: http://localhost:9090

### Staging
```bash
# Build images
docker build -f docker/Dockerfile -t sgr-api:staging .
docker build -f frontend/Dockerfile -t sgr-frontend:staging ./frontend

# Tag & push
docker tag sgr-api:staging ghcr.io/yourorg/sgr-api:staging
docker push ghcr.io/yourorg/sgr-api:staging
```

### Production (Kubernetes)
```bash
# 1. Set up cluster & prerequisites
kubectl cluster-info
helm repo add jetstack https://charts.jetstack.io
helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace

# 2. Deploy SGR
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml

# 3. Update secrets with real values
kubectl edit secret sgr-secrets -n sgr-prod

# 4. Deploy infrastructure
kubectl apply -f k8s/postgres.yaml k8s/redis.yaml
kubectl wait --for=condition=ready pod -l app=postgres -n sgr-prod --timeout=300s

# 5. Run migrations
kubectl run -it --rm migrate \
  --image=ghcr.io/yourorg/sgr-api:main \
  --restart=Never \
  -n sgr-prod \
  -- alembic upgrade head

# 6. Deploy applications
kubectl apply -f k8s/api-deployment.yaml k8s/frontend-deployment.yaml k8s/ingress.yaml

# 7. Verify
kubectl get pods -n sgr-prod
kubectl get ingress -n sgr-prod
```

## Pre-Production Checklist

### 1. Configuration
- [ ] `.env` file with all real values
- [ ] Database password changed from default
- [ ] API secret key is 32+ random characters
- [ ] Exchange API keys (if live trading) securely stored
- [ ] Sentry DSN configured (error tracking)
- [ ] Telegram bot token for alerts

### 2. Database
- [ ] PostgreSQL replication configured (if HA)
- [ ] Backup strategy implemented (daily dumps)
- [ ] Archival script for audit logs >90 days old
- [ ] Run migrations: `alembic upgrade head`

### 3. Monitoring
- [ ] Prometheus scrape targets all responding
- [ ] Grafana dashboards imported
- [ ] AlertManager configured (Slack/email integration)
- [ ] Sentry project linked & events flowing

### 4. Security
- [ ] SSL/TLS certificates installed (Let's Encrypt OK for prod)
- [ ] Rate limiting enabled on sensitive endpoints
- [ ] CORS origins restricted
- [ ] API keys rotated (if existing system)
- [ ] Network policies enforced (Kubernetes)

### 5. Testing
- [ ] Full backtesting passed (Sharpe >0.8)
- [ ] Paper trading for 2+ weeks at scale
- [ ] Failover tested (pod deletion)
- [ ] Graceful shutdown tested
- [ ] Recovery after crash tested

### 6. Go-Live Gates
- [ ] Risk limits reviewed by compliance
- [ ] Max position size limits appropriate
- [ ] Daily loss limit set
- [ ] Portfolio heat limit enforced
- [ ] Kill switch tested (manual override)

## Troubleshooting

### API won't start
```bash
# Check logs
docker compose logs api

# Check database connection
psql -h localhost -U sgr -d sgr -c "SELECT 1"

# Check Redis
redis-cli -h localhost ping
```

### High error rates
```bash
# View recent errors
curl http://localhost:8000/api/v1/system/health

# Check circuit breaker status
# (would need custom endpoint)

# Check exchange connectivity
curl http://localhost:8000/docs  # Try exchange endpoints
```

### Memory leak
```bash
# Monitor container memory
docker stats sgr-api

# Inside container
pip list --outdated  # Check deps
```

### Database migrations fail
```bash
# Check migration status
alembic current

# View available migrations
alembic history

# Downgrade last migration (if needed)
alembic downgrade -1

# Re-run
alembic upgrade head
```

## Scaling

### Horizontal (More Replicas)
```bash
# Kubernetes
kubectl scale deployment sgr-api -n sgr-prod --replicas=5

# Docker Compose (manual – not designed for this)
docker compose -f docker/docker-compose.yml up -d --scale api=3
```

### Vertical (Bigger Machines)
- Increase resource requests/limits in Deployment manifest
- Increase PostgreSQL `shared_buffers`, `work_mem`
- Increase Redis `maxmemory`

## Rollback

### Kubernetes
```bash
# View rollout history
kubectl rollout history deployment/sgr-api -n sgr-prod

# Rollback to previous version
kubectl rollout undo deployment/sgr-api -n sgr-prod

# Rollback to specific revision
kubectl rollout undo deployment/sgr-api -n sgr-prod --to-revision=3
```

### Docker Compose
```bash
# Pull previous image tag
docker pull ghcr.io/yourorg/sgr-api:v1.2.3

# Update compose file and restart
docker compose -f docker/docker-compose.yml up -d
```

## Backup & Recovery

### Database Backup
```bash
# Manual backup
pg_dump -h postgres -U sgr sgr > backup_$(date +%Y%m%d).sql

# Automated (daily via cron)
0 2 * * * pg_dump -h postgres -U sgr sgr > /backups/sgr_$(date +\%Y\%m\%d).sql

# Restore from backup
psql -h postgres -U sgr sgr < backup_20240820.sql
```

### Disaster Recovery
1. Restore PostgreSQL from backup
2. Restore Redis snapshot (if critical)
3. Restart API container (reads from DB)
4. Run recovery manager: `sgr.core.resilience.RecoveryManager.recover_after_crash()`

## Monitoring Dashboard

### Key Metrics to Watch
- **Portfolio Value:** Should grow (or be stable in paper trading)
- **Sharpe Ratio:** Live should ≈ backtest (if degrading, investigate)
- **Error Rate:** Should stay <1%
- **API Latency:** P95 <500ms
- **Position Count:** Monitor for accumulation
- **Circuit Breaker Status:** Should stay CLOSED

### Alerting Rules
- API down >2 min → Critical alert
- Error rate >5% → Warning
- Portfolio heat >70% → Warning
- API latency P95 >1s → Warning

## Maintenance

### Weekly
- [ ] Review audit logs for anomalies
- [ ] Check backups completed successfully
- [ ] Monitor disk usage (logs, database)

### Monthly
- [ ] Rotate API keys (if active)
- [ ] Review security patches (dependencies)
- [ ] Analyze performance trends

### Quarterly
- [ ] Full disaster recovery drill
- [ ] Upgrade dependencies
- [ ] Audit user/role permissions

## Support

### Getting Help
- **Logs:** `docker compose logs -f <service>`
- **Metrics:** Check Prometheus http://localhost:9090
- **Errors:** Check Sentry dashboard
- **Database:** `psql -h postgres -U sgr sgr`
