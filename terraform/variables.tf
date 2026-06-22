variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment (production | staging | development)"
  type        = string
  default     = "production"
  validation {
    condition     = contains(["production", "staging", "development"], var.environment)
    error_message = "environment must be production, staging, or development"
  }
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "miso_elt"
}

variable "db_username" {
  description = "RDS master username"
  type        = string
  default     = "miso_app"
}

variable "rds_instance_class" {
  description = "RDS instance type"
  type        = string
  default     = "db.t4g.micro"   # free-tier eligible
}

variable "ecr_image_uri" {
  description = "Full ECR image URI including tag (set by CI pipeline)"
  type        = string
  # e.g. 123456789012.dkr.ecr.us-east-1.amazonaws.com/miso-elt:abc1234
}

variable "alert_email" {
  description = "Email address to receive CloudWatch alarm notifications"
  type        = string
}
