# Helm Installation & Usage Guide

## Quick Start

### Install
```bash
# Add Helm repo (when published)
helm repo add sgr https://charts.sgr.example.com
helm repo update

# Or: local chart
helm install my-sgr ./helm/sgr \
  --namespace sgr-prod \
  --create-namespace \
  --values helm/sgr/values-prod.yaml
```

### Upgrade
```bash
helm upgrade my-sgr ./helm/sgr \
  --namespace sgr-prod \
  --values helm/sgr/values-prod.yaml
```

### Uninstall
```bash
helm uninstall my-sgr --namespace sgr-prod
```

## Values Customization

### Production Overrides (`values-prod.yaml`)
```yaml
api:
  replicaCount: 3
  autoscaling:
    minReplicas: 3
    maxReplicas: 10
    targetCPUUtilizationPercentage: 60

secrets:
  dbPassword: "your-real-db-password"
  apiSecretKey: "your-real-secret-key-min-32-chars"
  encryptionKey: "your-real-32-byte-key"
  sentryDsn: "https://xxx@sentry.io/xxx"
```

### Installation with Overrides
```bash
helm install my-sgr ./helm/sgr \
  --namespace sgr-prod \
  --create-namespace \
  -f helm/sgr/values.yaml \
  -f helm/sgr/values-prod.yaml \
  --set secrets.dbPassword=mypassword \
  --set api.image.tag=v1.2.3
```

## Helm Templates Reference

### Built-in Values
- `.Release.Name`: Release name (e.g., "my-sgr")
- `.Release.Namespace`: Kubernetes namespace
- `.Chart.Name`: Chart name ("sgr")
- `.Chart.Version`: Chart version ("0.1.0")
- `.Values`: All values from values.yaml

### Helper Functions
- `include "sgr.labels"`: Common labels (managed by Helm)
- `include "sgr.selectorLabels"`: Pod selector labels
- `include "sgr.fullname"`: Full resource name

### Conditional Rendering
```yaml
{{- if .Values.prometheus.enabled }}
# Prometheus configuration
{{- end }}
```

## Validation

```bash
# Dry-run (validate without installing)
helm install my-sgr ./helm/sgr --dry-run --debug

# Lint (check for errors)
helm lint ./helm/sgr

# Template (render templates)
helm template my-sgr ./helm/sgr
```

## Debugging

```bash
# Get all resources
kubectl get all -n sgr-prod

# View manifest
helm get manifest my-sgr -n sgr-prod

# View values used
helm get values my-sgr -n sgr-prod

# Check release history
helm history my-sgr -n sgr-prod

# Rollback to previous release
helm rollback my-sgr 1 -n sgr-prod
```

## Publishing to Helm Repository

```bash
# Package chart
helm package ./helm/sgr

# Push to repository (e.g., ChartMuseum, GitHub Pages, Artifactory)
# Example: GitHub Pages
git add helm/sgr-0.1.0.tgz
git commit -m "Release sgr chart v0.1.0"
git push
```

Then users can:
```bash
helm repo add sgr https://raw.githubusercontent.com/yourorg/sgr/main/helm/
helm install my-sgr sgr/sgr
```
