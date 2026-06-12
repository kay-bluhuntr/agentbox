# AgentBox infrastructure: VPC + EKS + RDS Postgres.
#
# Deliberately built on the community terraform-aws-modules rather than
# hand-rolled resources: they're battle-tested, and the interesting decisions
# here are the knobs (private subnets, IRSA, single-AZ dev RDS), not VPC
# boilerplate. State backend config lives in backend.tf per environment.

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

provider "aws" {
  region = var.region
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.8"

  name = "${var.name}-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["${var.region}a", "${var.region}b", "${var.region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true # dev: one NAT GW; production would be one per AZ

  tags = local.tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.8"

  cluster_name    = var.name
  cluster_version = "1.29"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_endpoint_public_access = true
  enable_irsa                    = true

  eks_managed_node_groups = {
    default = {
      instance_types = ["t3.large"]
      min_size       = 2
      max_size       = 5
      desired_size   = 2
    }
  }

  tags = local.tags
}

module "db" {
  source  = "terraform-aws-modules/rds/aws"
  version = "~> 6.5"

  identifier     = "${var.name}-pg"
  engine         = "postgres"
  engine_version = "16"
  family         = "postgres16"
  instance_class = "db.t4g.micro" # dev sizing; bump + multi_az for production

  allocated_storage = 20
  db_name           = "agentbox"
  username          = "agentbox"
  port              = 5432

  manage_master_user_password = true # credentials live in Secrets Manager, not state

  multi_az               = false
  db_subnet_group_name   = module.vpc.database_subnet_group_name
  subnet_ids             = module.vpc.private_subnets
  vpc_security_group_ids = [module.db_sg.security_group_id]

  tags = local.tags
}

module "db_sg" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "~> 5.1"

  name   = "${var.name}-pg"
  vpc_id = module.vpc.vpc_id

  ingress_with_source_security_group_id = [{
    rule                     = "postgresql-tcp"
    source_security_group_id = module.eks.node_security_group_id
  }]

  tags = local.tags
}

locals {
  tags = {
    Project   = "agentbox"
    ManagedBy = "terraform"
  }
}
