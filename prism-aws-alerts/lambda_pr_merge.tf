data "archive_file" "pr_merge_notifier" {
  type        = "zip"
  source_dir  = "${path.module}/src/pr_merge_notifier"
  output_path = "${path.module}/build/pr_merge_notifier.zip"
}

resource "aws_cloudwatch_log_group" "pr_merge_notifier" {
  name              = "/aws/lambda/${var.name_prefix}-pr-merge-notifier"
  retention_in_days = var.lambda_log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "pr_merge_notifier" {
  function_name    = "${var.name_prefix}-pr-merge-notifier"
  role             = aws_iam_role.pr_merge_notifier.arn
  handler          = "index.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.pr_merge_notifier.output_path
  source_code_hash = data.archive_file.pr_merge_notifier.output_base64sha256

  environment {
    variables = {
      CHAT_TARGETS_JSON   = jsonencode(var.chat_targets)
      DYNAMODB_TABLE_NAME = aws_dynamodb_table.pr_deploy_threads.name
      THREAD_TTL_DAYS     = tostring(var.thread_ttl_days)
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.pr_merge_notifier,
    aws_iam_role_policy.pr_merge_notifier,
    aws_iam_role_policy_attachment.pr_merge_notifier_logs,
  ]

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "pr_merged" {
  name        = "${var.name_prefix}-pr-merged"
  description = "Fires when a pull request is merged into a watched branch of a watched CodeCommit repository."

  event_pattern = jsonencode({
    source      = ["aws.codecommit"]
    detail-type = ["CodeCommit Pull Request State Change"]
    detail = {
      event                = ["pullRequestStatusChanged"]
      isMerged             = ["True"]
      repositoryNames      = var.watched_repositories
      destinationReference = [for b in local.watched_branches : "refs/heads/${b}"]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "pr_merged" {
  rule = aws_cloudwatch_event_rule.pr_merged.name
  arn  = aws_lambda_function.pr_merge_notifier.arn
}

resource "aws_lambda_permission" "pr_merged_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pr_merge_notifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.pr_merged.arn
}
