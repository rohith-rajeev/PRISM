resource "aws_dynamodb_table" "pr_deploy_threads" {
  name         = "${var.name_prefix}-pr-deploy-threads"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "commit_sha"

  attribute {
    name = "commit_sha"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  tags = var.tags
}
