variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix applied to every resource this project creates, to keep it isolated from any other IaC-managed stacks in the same account (e.g. a SAM or CDK application)."
  type        = string
  default     = "prism-aws-alerts"
}

variable "chat_targets" {
  description = <<-EOT
    Google Chat spaces to notify, each with its own webhook and its own set of
    branches. A branch can be watched by more than one chat, and a chat can
    watch more than one branch - e.g. "chat-a" watching ["qa", "staging"] and
    "chat-b" watching only ["staging"]. Set via a terraform.tfvars file or
    TF_VAR_chat_targets (as a JSON string) - never commit real webhook URLs.
  EOT
  type = list(object({
    name        = string
    webhook_url = string
    branches    = list(string)
  }))
  sensitive = true

  validation {
    condition     = length(var.chat_targets) == length(distinct([for t in var.chat_targets : t.name]))
    error_message = "Each chat_targets entry must have a unique name."
  }
}

variable "watched_repositories" {
  description = "CodeCommit repository names to watch for merged pull requests. Placeholder values below - override for your account."
  type        = list(string)
  default = [
    "your-backend-repo",
    "your-frontend-repo",
  ]
}

variable "pipeline_branches" {
  description = "Map of CodePipeline pipeline name -> the branch it deploys. Used to (a) watch these pipelines for successful executions and (b) know which chats to notify as a fallback when a deployment can't be matched to a recorded PR-merge thread. Placeholder values below - override for your account."
  type        = map(string)
  default = {
    "your-backend-pipeline-qa"   = "qa"
    "your-backend-pipeline-stg"  = "staging"
    "your-frontend-pipeline-qa"  = "qa"
    "your-frontend-pipeline-stg" = "staging"
  }
}

variable "thread_ttl_days" {
  description = "How many days a stored PR-merge -> Google Chat thread mapping is kept in DynamoDB before it expires (TTL)."
  type        = number
  default     = 30
}

variable "post_unlinked_deployments" {
  description = "If true, a successful deployment whose source commit cannot be matched to a recorded PR-merge thread is still posted as a new (non-threaded) card instead of being dropped."
  type        = bool
  default     = true
}

variable "lambda_log_retention_days" {
  description = "CloudWatch Logs retention for both Lambda functions."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to all taggable resources."
  type        = map(string)
  default     = {}
}
