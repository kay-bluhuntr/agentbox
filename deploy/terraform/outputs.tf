output "cluster_name" {
  value = module.eks.cluster_name
}

output "configure_kubectl" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "db_secret_arn" {
  description = "Secrets Manager ARN holding the master DB credentials"
  value       = module.db.db_instance_master_user_secret_arn
}
