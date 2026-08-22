# Project SGR Kubernetes Deployment

## Quick Start

```bash
# 1. Create namespace
kubectl apply -f k8s/namespace.yaml

# 2. Create configs & secrets
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml

# 3. Deploy infrastructure (PostgreSQL, Redis)
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis.yaml

# 4. Wait for services to be ready
kubectl wait --for=condition=ready pod -l app=postgres -n sgr-prod --timeout=300s
kubectl wait --for=condition=ready pod -l app=redis -n sgr-prod --timeout=300s

# 5. Deploy API & Frontend
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/frontend-deployment.yaml

# 6. Set up networking & monitoring
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/monitoring.yaml

# 7. Verify deployment
kubectl get pods -n sgr-prod
kubectl get svc -n sgr-prod
```

## Architecture

### Components
- **API**: 2-5 replicas, auto-scaling on CPU/Memory
- **Frontend**: 2-5 replicas, static files served from Next.js
- **PostgreSQL**: 1 replica (StatefulSet), 20Gi PVC
- **Redis**: 1 replica (StatefulSet), 10Gi PVC
- **Prometheus**: Metrics aggregation & alerting
- **Ingress**: TLS termination, rate limiting

### Security
- Pod Security Standards: restricted
- Network Policies: deny-all by default, explicit allow
- RBAC: Prometheus service account for cluster access
- Non-root containers: UID 1000
- TLS via cert-manager (Let's Encrypt)

### High Availability
- Multiple replicas with PodDisruptionBudgets
- HorizontalPodAutoscalers for dynamic scaling
- Readiness/Liveness probes for health checks
- Graceful termination (30s grace period)

## Configuration

### Secrets (k8s/secret.yaml)
⚠️ **IMPORTANT**: Replace all `changeme-*` values:
- `DB_PASSWORD`: Strong database password
- `API_SECRET_KEY`: 32+ char random string
- `ENCRYPTION_MASTER_KEY`: 32-byte key for encryption at rest
- Exchange API keys (leave empty for paper trading)
- Sentry DSN & Telegram tokens (optional)

### ConfigMap (k8s/configmap.yaml)
- `ENVIRONMENT`: production
- `TRADING_MODE`: paper | live
- `API_CORS_ORIGINS`: Dashboard URL(s)

### Ingress (k8s/ingress.yaml)
Update hostnames:
- `api.example.com` → your API domain
- `dashboard.example.com` → your frontend domain

Requires:
- NGINX Ingress Controller
- cert-manager with Let's Encrypt issuer

## Monitoring

### Prometheus
Scrapes metrics from:
- `/metrics` endpoint on API (9000)
- Kubernetes nodes & pods

Port: `9090` (accessible via Ingress or port-forward)

### Alerts
Pre-configured in `k8s/monitoring.yaml`:
- API down (critical)
- High error rate >5% (warning)
- High memory usage >90% (warning)

Integrate with AlertManager for notifications (Slack, email, etc.)

## Scaling

### Horizontal Scaling
API & Frontend auto-scale 2-5 replicas based on CPU/Memory:
```bash
kubectl get hpa -n sgr-prod
```

### Manual Scaling
```bash
kubectl scale deployment sgr-api -n sgr-prod --replicas=4
```

## Database Migrations

Run migrations before deployment:
```bash
kubectl run -it --rm sgr-migrate \
  --image=ghcr.io/gordongraff81-tech/sgr-api:latest \
  --restart=Never \
  -n sgr-prod \
  -- alembic upgrade head
```

## Logs & Debugging

```bash
# View logs
kubectl logs -f deployment/sgr-api -n sgr-prod
kubectl logs -f deployment/sgr-frontend -n sgr-prod

# Exec into pod
kubectl exec -it <pod-name> -n sgr-prod -- /bin/sh

# Port-forward services
kubectl port-forward svc/sgr-api 8000:8000 -n sgr-prod
kubectl port-forward svc/prometheus 9090:9090 -n sgr-prod

# Describe resources
kubectl describe pod <pod-name> -n sgr-prod
kubectl describe pvc <pvc-name> -n sgr-prod
```

## Troubleshooting

### Pod not starting
```bash
kubectl describe pod <name> -n sgr-prod
kubectl logs <name> -n sgr-prod --previous  # check init errors
```

### Disk space (PVC) issues
```bash
kubectl get pvc -n sgr-prod
kubectl describe pvc <name> -n sgr-prod
# Expand PVC if needed
```

### Ingress not routing
```bash
kubectl get ingress -n sgr-prod -o wide
kubectl describe ingress sgr-ingress -n sgr-prod
# Check cert status
kubectl get certificate -n sgr-prod
```

## Cleanup

```bash
# Delete all resources
kubectl delete namespace sgr-prod

# Keep PVCs (data preservation)
kubectl delete all -n sgr-prod --ignore-not-found=true
kubectl delete pvc -n sgr-prod -l app
```

## Production Checklist

- [ ] Replace all secrets with real values
- [ ] Set correct ingress hostnames & TLS certificate
- [ ] Configure persistent volumes with appropriate storage class
- [ ] Set resource requests/limits based on expected load
- [ ] Implement AlertManager integration for notifications
- [ ] Enable network policies & RBAC
- [ ] Set up monitoring dashboards (Grafana)
- [ ] Test failover & pod eviction
- [ ] Enable PodSecurityPolicy for stricter controls
- [ ] Configure backup strategy for PostgreSQL & Redis
