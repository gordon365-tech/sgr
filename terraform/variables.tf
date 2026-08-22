variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
  
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "sgr-prod"
}

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.28"
}

variable "instance_type" {
  description = "EC2 instance type for node group"
  type        = string
  default     = "t3.large"
}

variable "min_node_count" {
  description = "Minimum number of nodes"
  type        = number
  default     = 2
}

variable "max_node_count" {
  description = "Maximum number of nodes"
  type        = number
  default     = 5
}

variable "database_allocated_storage" {
  description = "RDS database allocated storage in GB"
  type        = number
  default     = 100
}

variable "database_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.large"
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t3.medium"
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
