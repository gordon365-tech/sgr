# RDS PostgreSQL
resource "aws_db_instance" "postgres" {
  identifier     = "${var.cluster_name}-postgres"
  engine         = "postgres"
  engine_version = "16.1"
  
  instance_class       = var.database_instance_class
  allocated_storage    = var.database_allocated_storage
  storage_type         = "gp3"
  storage_encrypted    = true
  
  db_name  = "sgr"
  username = "sgruser"
  password = random_password.db_password.result
  
  db_subnet_group_name   = aws_db_subnet_group.postgres.name
  vpc_security_group_ids = [aws_security_group.postgres.id]
  
  backup_retention_period = 30
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:00-sun:05:00"
  
  skip_final_snapshot = false
  final_snapshot_identifier = "${var.cluster_name}-postgres-final-snapshot-${formatdate("YYYY-MM-DD-hhmm", timestamp())}"
  
  multi_az = var.environment == "prod" ? true : false
  
  tags = {
    Name = "${var.cluster_name}-postgres"
  }
}

# DB Subnet Group
resource "aws_db_subnet_group" "postgres" {
  name       = "${var.cluster_name}-postgres-subnet"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.cluster_name}-postgres-subnet"
  }
}

# Security Group for RDS
resource "aws_security_group" "postgres" {
  name        = "${var.cluster_name}-postgres-sg"
  description = "Security group for RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.eks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.cluster_name}-postgres-sg"
  }
}

# Random password for RDS
resource "random_password" "db_password" {
  length  = 32
  special = true
}

# Store password in Secrets Manager
resource "aws_secretsmanager_secret" "db_password" {
  name = "${var.cluster_name}/postgres/password"
}

resource "aws_secretsmanager_secret_version" "db_password" {
  secret_id      = aws_secretsmanager_secret.db_password.id
  secret_string  = random_password.db_password.result
}

# ElastiCache Redis
resource "aws_elasticache_cluster" "redis" {
  cluster_id           = "${var.cluster_name}-redis"
  engine               = "redis"
  engine_version       = "7.0"
  node_type            = var.redis_node_type
  num_cache_nodes      = var.environment == "prod" ? 2 : 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  
  security_group_ids      = [aws_security_group.redis.id]
  subnet_group_name       = aws_elasticache_subnet_group.redis.name
  
  automatic_failover_enabled = var.environment == "prod" ? true : false
  
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  
  tags = {
    Name = "${var.cluster_name}-redis"
  }
}

# ElastiCache Subnet Group
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.cluster_name}-redis-subnet"
  subnet_ids = aws_subnet.private[*].id
}

# Security Group for Redis
resource "aws_security_group" "redis" {
  name        = "${var.cluster_name}-redis-sg"
  description = "Security group for ElastiCache Redis"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.eks.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.cluster_name}-redis-sg"
  }
}

# Outputs
output "postgres_endpoint" {
  value       = aws_db_instance.postgres.endpoint
  description = "RDS PostgreSQL endpoint"
}

output "redis_endpoint" {
  value       = aws_elasticache_cluster.redis.cache_nodes[0].address
  description = "ElastiCache Redis endpoint"
}

output "db_password_secret" {
  value       = aws_secretsmanager_secret.db_password.arn
  description = "ARN of the Secrets Manager secret containing the DB password"
  sensitive   = true
}
