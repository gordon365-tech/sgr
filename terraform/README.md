# Terraform AWS Infrastructure Guide

## Quick Start

### Prerequisites
```bash
# Install Terraform
brew install terraform  # macOS
# or download from https://www.terraform.io/downloads.html

# Configure AWS credentials
aws configure
# or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY

# Install kubectl
brew install kubectl
```

### Initialize Terraform
```bash
cd terraform

# Initialize (downloads providers & plugins)
terraform init

# Create S3 bucket & DynamoDB table for state (one-time)
# First update backend in provider.tf to local:
#   backend "local" {}
terraform apply
# Then update backend to s3 and run:
terraform init -migrate-state
```

### Plan & Apply
```bash
# Review what will be created
terraform plan -out=tfplan

# Apply changes
terraform apply tfplan

# View outputs (endpoints, cluster name, etc.)
terraform output
```

### Access the Cluster
```bash
# Update kubeconfig
aws eks update-kubeconfig \
  --name $(terraform output -raw eks_cluster_name) \
  --region us-east-1

# Verify
kubectl get nodes
kubectl get pods -A
```

## Architecture

### Compute (EKS)
- **Cluster:** 1.28 Kubernetes
- **Nodes:** t3.large instances (min 2, max 5, auto-scaling)
- **Subnets:** 2 public + 2 private across 2 AZs
- **Security:** Network policies, security groups

### Database (RDS PostgreSQL)
- **Engine:** PostgreSQL 16.1
- **Instance:** db.t3.large (100GB gp3)
- **Multi-AZ:** Yes (production)
- **Backups:** 30-day retention
- **Encryption:** At-rest & in-transit

### Cache (ElastiCache Redis)
- **Engine:** Redis 7.0
- **Nodes:** 2 (production, 1 dev)
- **Encryption:** At-rest & in-transit
- **Failover:** Automatic (production)

### Registry (ECR)
- **Repositories:** sgr-api, sgr-frontend
- **Scanning:** Trivy on push
- **Lifecycle:** Keep last 10 images

### State Management
- **S3:** Encrypted, versioned state
- **DynamoDB:** State locking

## Configuration

### Production vs. Development
```bash
# Development (default)
terraform apply

# Production (multi-AZ RDS, more replicas)
terraform apply -var environment=prod -var max_node_count=10
```

### Variables
- `aws_region`: AWS region (default: us-east-1)
- `environment`: dev/staging/prod
- `cluster_name`: K8s cluster name
- `cluster_version`: Kubernetes version
- `instance_type`: Node instance type
- `min_node_count`: Min nodes
- `max_node_count`: Max nodes
- `database_instance_class`: RDS instance type
- `redis_node_type`: ElastiCache node type

### tfvars File (Production)
```hcl
# terraform/prod.tfvars
aws_region                    = "us-west-2"
environment                   = "prod"
cluster_name                  = "sgr-prod"
instance_type                 = "t3.xlarge"
min_node_count                = 3
max_node_count                = 10
database_instance_class       = "db.r6i.xlarge"
database_allocated_storage    = 200
redis_node_type               = "cache.r6g.large"
```

### Apply with tfvars
```bash
terraform apply -var-file=prod.tfvars
```

## Deploying Applications

### Get Database Credentials
```bash
# RDS password (stored in Secrets Manager)
aws secretsmanager get-secret-value \
  --secret-id sgr-prod/postgres/password \
  --query SecretString \
  --output text

# RDS endpoint
terraform output postgres_endpoint
```

### Deploy via Helm
```bash
# Get cluster name
CLUSTER_NAME=$(terraform output -raw eks_cluster_name)

# Update kubeconfig
aws eks update-kubeconfig --name $CLUSTER_NAME

# Install Helm chart
helm install sgr ../helm/sgr \
  --namespace sgr-prod \
  --create-namespace \
  -f ../helm/sgr/values-prod.yaml \
  --set postgresql.auth.password=<from-secrets-manager> \
  --set api.image.tag=latest
```

## Monitoring

### CloudWatch Logs
```bash
# View EKS logs
aws logs tail /aws/eks/sgr-prod --follow
```

### Terraform State
```bash
# List resources
terraform state list

# Show resource details
terraform state show aws_eks_cluster.main

# Refresh state
terraform refresh
```

## Troubleshooting

### Cluster not accessible
```bash
# Check cluster status
aws eks describe-cluster --name sgr-prod --query 'cluster.status'

# Update kubeconfig
aws eks update-kubeconfig --name sgr-prod --region us-east-1

# Test connection
kubectl cluster-info
```

### RDS connection failed
```bash
# Check security group
aws ec2 describe-security-groups --filters Name=group-name,Values=sgr-prod-postgres-sg

# Test from pod
kubectl run -it --rm debug --image=postgres:16 --restart=Never -- \
  psql -h <rds-endpoint> -U sgruser -d sgr -c "SELECT 1"
```

### State lock (if stuck)
```bash
# View locks
aws dynamodb scan --table-name terraform-locks

# Force unlock (DANGER – use only if stuck)
terraform force-unlock <LOCK_ID>
```

## Cleanup

```bash
# Destroy all resources
terraform destroy

# Destroy specific resource
terraform destroy -target aws_eks_cluster.main

# Remove local state
rm -rf .terraform terraform.tfstate*
```

## Security Best Practices

- [ ] Use IAM roles for pod authentication (IRSA)
- [ ] Enable network policies in K8s
- [ ] Rotate database password regularly
- [ ] Enable CloudTrail for audit logs
- [ ] Use AWS Secrets Manager for sensitive data
- [ ] Enable VPC Flow Logs
- [ ] Regular RDS backups tested for restore
- [ ] Encrypt all data in transit (TLS)

## Cost Optimization

- Use spot instances for non-critical workloads
- Enable RDS Reserved Instances (1-3 year commitments)
- Use NAT Gateway autoscaling
- Archive old CloudWatch logs to S3
- Review and delete unused resources

## Scaling

### Horizontal (More Nodes)
```bash
terraform apply -var max_node_count=20
```

### Vertical (Bigger Instances)
```bash
terraform apply -var instance_type=t3.2xlarge
```

### Database
```bash
terraform apply -var database_allocated_storage=500
```
