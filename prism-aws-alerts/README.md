# prism-aws-alerts

Standalone Terraform project. Posts a card to one or more Google Chat spaces
when a pull request is merged into a watched branch (e.g. `qa`, `staging`,
`main`), then posts a second message **as a threaded reply** on that same
message, in every one of those spaces, once the resulting commit has been
deployed (or fails to deploy) by the matching CodePipeline pipeline.

Routing is per chat, per branch: a chat can watch multiple branches, and a
branch can be watched by multiple chats. For example:

```hcl
chat_targets = [
  { name = "chat-a", webhook_url = "...", branches = ["qa", "staging", "main"] },
  { name = "chat-b", webhook_url = "...", branches = ["staging"] },
]
```

`chat-a` gets both the merge card and the deploy reply for `qa`, `staging`
*and* `main` (production); `chat-b` only gets them for `staging`. Each chat's
message thread is tracked independently, so the deploy reply lands in the
right thread in every chat that received the original merge card.

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
   Change` events where `state = SUCCEEDED` or `FAILED`, for every pipeline
   listed in `pipeline_branches`.
   - Calls `codepipeline:GetPipelineExecution` to read the commit SHA the
     pipeline actually built.
   - Looks that commit up in DynamoDB. If found, posts a card in the
     same style as the merge card — titled `{environment} deployment
     {succeeded|failed}`, with a colour-coded **SUCCEEDED** (green) or
     **FAILED** (red) status row, the PR, commit and author — **threaded
     onto** the original message in each recorded chat. The author is the PR
     author recorded by the merge alert, so both messages name the same
     person; the git commit author is only a fallback.
   - If the commit isn't found (e.g. a manual re-run on an old revision), it
     falls back to `pipeline_branches` to work out which branch was deployed
     and posts a standalone message to every chat watching that branch
     instead — unless `post_unlinked_deployments = false`, in which case it's
     dropped.

No SNS topics, CodeCommit notification rules, or existing EventBridge buses
are touched — both rules run on the account's default event bus, which
already receives these events natively.

## The develop group

Merges and deployments to `develop` go to their own Google Chat group, using
the same mechanism as the release branches - there is no second alerting
system. It is simply one more `chat_targets` entry that watches **only**
`develop`, plus the develop pipelines in the pipeline maps:

```hcl
chat_targets = [
  { name = "chat-a",       webhook_url = "<release group webhook>", branches = ["qa", "staging", "main"] },
  { name = "chat-b",       webhook_url = "<staging group webhook>", branches = ["staging"] },
  { name = "chat-develop", webhook_url = "<develop group webhook>", branches = ["develop"] },
]

pipeline_branches = {
  # ...the qa / staging / main pipelines...
  "your-backend-pipeline-dev"  = "develop"
  "your-frontend-pipeline-dev" = "develop"
}

pipeline_repositories = {
  # ...the qa / staging / main pipelines...
  "your-backend-pipeline-dev"  = "your-backend-repo"
  "your-frontend-pipeline-dev" = "your-frontend-repo"
}
```

List one develop pipeline per repository that deploys `develop`. The values
above are placeholders, like every other entry in `terraform.tfvars.example`;
put the real develop webhook only in the gitignored `terraform.tfvars` (or
`TF_VAR_chat_targets`).

With that in place:

- A PR merged into `develop` posts the "Pull Request Merged" card to the
  develop group only. Merges into `qa`, `staging` or `main` never reach it,
  and develop merges never reach the release groups (no target lists both).
- When a develop pipeline succeeds or fails, the reply is threaded onto that
  merge card in the develop group, in the same card style as the other
  environments, titled `Develop deployment succeeded|failed` (the environment
  label comes from the pipeline name's last part: `-dev` / `-develop` read as
  "Develop").
- A develop deployment that can't be matched to a merge card follows
  `post_unlinked_deployments`: when on, the standalone card goes to the develop
  group only, because it is the only target watching `develop`.
- Thread tracking is unchanged: develop merges are stored in the same
  DynamoDB table with the same `thread_ttl_days` (30-day) TTL, and chat target
  names must still be unique.
- CodeCommit and CodePipeline only. Alerts for GitLab repositories or
  pipelines are out of scope.

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
