# prism-aws-alerts

Standalone Terraform project. Posts a card to one or more Google Chat spaces
when a pull request is merged into a watched branch (e.g. `qa`, `staging`),
then posts a second card **as a threaded reply** on that same message, in
every one of those spaces, once the resulting commit has been deployed by the
matching CodePipeline pipeline.

Routing is per chat, per branch: a chat can watch multiple branches, and a
branch can be watched by multiple chats. For example:

```hcl
chat_targets = [
  { name = "chat-a", webhook_url = "...", branches = ["qa", "staging"] },
  { name = "chat-b", webhook_url = "...", branches = ["staging"] },
]
```

`chat-a` gets both the merge card and the deploy reply for `qa` *and*
`staging`; `chat-b` only gets them for `staging`. Each chat's message thread
is tracked independently, so the deploy reply lands in the right thread in
every chat that received the original merge card.

All resources are new (`prism-aws-alerts-*` by default) and independent of any
other IaC-managed stacks in the same AWS account (e.g. a SAM or CDK
application) — nothing here is imported from or attached to them.

## How it works

1. **`pr-merge-notifier` Lambda** — triggered by an EventBridge rule matching
   `aws.codecommit` / `CodeCommit Pull Request State Change` events where
   `isMerged = True` and the destination branch is one watched by any
   `chat_targets` entry.
   - Calls `codecommit:GetPullRequest` to get the PR title/author and, more
     importantly, `pullRequestTargets[].mergeMetadata.mergeCommitId` — the
     exact commit that landed on the branch (works regardless of merge
     strategy).
   - Posts a "Pull Request Merged" card to **every** chat target whose
     `branches` list includes the destination branch.
   - Stores `commit_sha -> [{chat_name, thread_name}, ...]` in DynamoDB
     (30-day TTL by default) so the deployment step below knows which threads,
     in which chats, to reply to.

2. **`deployment-notifier` Lambda** — triggered by an EventBridge rule
   matching `aws.codepipeline` / `CodePipeline Pipeline Execution State
   Change` events where `state = SUCCEEDED`, for every pipeline listed in
   `pipeline_branches`.
   - Calls `codepipeline:GetPipelineExecution` to read the commit SHA the
     pipeline actually built.
   - Looks that commit up in DynamoDB. If found, posts a "Deployment
     Succeeded" card **threaded onto** the original message in each recorded
     chat.
   - If the commit isn't found (e.g. a manual re-run on an old revision), it
     falls back to `pipeline_branches` to work out which branch was deployed
     and posts a standalone card to every chat watching that branch instead
     — unless `post_unlinked_deployments = false`, in which case it's dropped.

No SNS topics, CodeCommit notification rules, or existing EventBridge buses
are touched — both rules run on the account's default event bus, which
already receives these events natively.

## Setup

1. Create an incoming webhook for each target Google Chat space (space menu →
   *Apps & integrations* → *Webhooks*). Copy each full URL.
2. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in
   `chat_targets` (or export it instead, so it never touches disk):
   ```bash
   export TF_VAR_chat_targets='[{"name":"chat-a","webhook_url":"https://chat.googleapis.com/...A","branches":["qa","staging"]},{"name":"chat-b","webhook_url":"https://chat.googleapis.com/...B","branches":["staging"]}]'
   ```
3. Adjust `watched_repositories` and `pipeline_branches` in `terraform.tfvars`
   if they ever change.
4. ```bash
   terraform init
   terraform plan
   terraform apply
   ```

Uses whatever AWS credentials are already active in your shell/CLI profile —
no keys are hardcoded anywhere in this project.

## Environment variables (set on the Lambdas via Terraform)

| Variable | Lambda(s) | Purpose |
|---|---|---|
| `CHAT_TARGETS_JSON` | both | JSON-encoded `chat_targets` list (name/webhook_url/branches per chat) |
| `DYNAMODB_TABLE_NAME` | both | Thread-mapping table name |
| `THREAD_TTL_DAYS` | pr-merge-notifier | TTL for stored mappings (default 30) |
| `PIPELINE_BRANCHES_JSON` | deployment-notifier | JSON-encoded `pipeline_branches` map, used for fallback routing |
| `POST_UNLINKED_DEPLOYMENTS` | deployment-notifier | Post a standalone card when no originating PR thread is found (default `true`) |

## Notes / assumptions

- "Merged" is detected via CodeCommit's native PR close event with
  `isMerged: True` — no CodeCommit notification rule or SNS topic is needed,
  EventBridge already receives this natively.
- Correlation relies on each CodePipeline execution being triggered by, and
  building, a single push/merge to its branch (matches a typical
  `Source(CodeCommit) -> Build(CodeBuild)` pipeline shape, one pipeline per
  repo/branch). If a pipeline is ever re-run manually on an old revision, or
  multiple merges land before a build picks them up, the reply is matched by
  exact commit SHA, so it will simply fail to find a thread rather than
  mis-attribute one.
- `chat_targets` names must be unique (enforced by a variable validation
  block) since they're used as the join key between the merge and deploy
  Lambdas.
