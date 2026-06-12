variable "name" {
  description = "Base name for all resources"
  type        = string
  default     = "agentbox"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "eu-west-2" # London
}
