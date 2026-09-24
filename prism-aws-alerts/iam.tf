data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition

  watched_repository_arns = [
    for name in var.watched_repositories :
    "arn:${local.partition}:codecommit:${var.aws_region}:${local.account_id}:${name}"
  ]

  watched_pipeline_arns = [
    for name in keys(var.pipeline_branches) :
    "arn:${local.partition}:codepipeline:${var.aws_region}:${local.account_id}:${name}"
  ]

  # Union of every branch any chat target cares about - drives the CodeCommit
  # EventBridge rule so we only wake the Lambda for branches someone watches.
  watched_branches = distinct(flatten([for t in var.chat_targets : t.branches]))
}

data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- pr_merge_notifier role -------------------------------------------------

resource "aws_iam_role" "pr_merge_notifier" {
  name               = "${var.name_prefix}-pr-merge-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "pr_merge_notifier_logs" {
  role       = aws_iam_role.pr_merge_notifier.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "pr_merge_notifier" {
  statement {
    sid       = "ReadPullRequests"
    actions   = ["codecommit:GetPullRequest"]
    resources = local.watched_repository_arns
  }

  statement {
    sid       = "WriteThreadMapping"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.pr_deploy_threads.arn]
  }
}

resource "aws_iam_role_policy" "pr_merge_notifier" {
  name   = "${var.name_prefix}-pr-merge-notifier"
  role   = aws_iam_role.pr_merge_notifier.id
  policy = data.aws_iam_policy_document.pr_merge_notifier.json
}

# --- deployment_notifier role ------------------------------------------------

resource "aws_iam_role" "deployment_notifier" {
  name               = "${var.name_prefix}-deployment-notifier"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "deployment_notifier_logs" {
  role       = aws_iam_role.deployment_notifier.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "deployment_notifier" {
  statement {
    sid       = "ReadPipelineExecutions"
    actions   = ["codepipeline:GetPipelineExecution"]
    resources = local.watched_pipeline_arns
  }

  statement {
    sid       = "ReadThreadMapping"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.pr_deploy_threads.arn]
  }
}

resource "aws_iam_role_policy" "deployment_notifier" {
  name   = "${var.name_prefix}-deployment-notifier"
  role   = aws_iam_role.deployment_notifier.id
  policy = data.aws_iam_policy_document.deployment_notifier.json
}
