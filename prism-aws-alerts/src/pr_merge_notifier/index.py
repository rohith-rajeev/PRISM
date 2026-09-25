import json
import os
import time
import urllib.request

import boto3

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
CHAT_TARGETS = json.loads(os.environ["CHAT_TARGETS_JSON"])
THREAD_TTL_DAYS = int(os.environ.get("THREAD_TTL_DAYS", "30"))

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
codecommit = boto3.client("codecommit")


def _targets_for_branch(branch):
    return [t for t in CHAT_TARGETS if branch in t.get("branches", [])]


def _post_to_chat(webhook_url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _find_target(pr, repo_name, destination_reference):
    for target in pr.get("pullRequestTargets", []):
        if (
            target.get("repositoryName") == repo_name
            and target.get("destinationReference") == destination_reference
        ):
            return target
    targets = pr.get("pullRequestTargets", [])
    return targets[0] if targets else {}


def _build_card(repo_name, branch, pr_id, title, author):
    return {
        "cardsV2": [
            {
                "cardId": f"pr-merge-{repo_name}-{pr_id}",
                "card": {
                    "header": {
                        "title": f"Merged PR #{pr_id} → {branch}",
                        "subtitle": repo_name,
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"decoratedText": {"topLabel": "PR", "text": title}},
                                {"decoratedText": {"topLabel": "Author", "text": author}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def handler(event, context):
    detail = event.get("detail", {})

    if str(detail.get("isMerged", "")).lower() != "true":
        print("Not a merge closure event, ignoring:", json.dumps(detail))
        return

    repo_names = detail.get("repositoryNames") or []
    repo_name = repo_names[0] if repo_names else None
    pr_id = detail.get("pullRequestId")
    destination_reference = detail.get("destinationReference", "")
    branch = destination_reference.replace("refs/heads/", "")

    if not repo_name or not pr_id:
        print("Missing repository or pull request id, ignoring event")
        return

    targets = _targets_for_branch(branch)
    if not targets:
        print(f"No chat_targets configured for branch '{branch}', ignoring")
        return

    pr = codecommit.get_pull_request(pullRequestId=str(pr_id))["pullRequest"]
    target_meta = _find_target(pr, repo_name, destination_reference)
    merge_commit_id = (target_meta.get("mergeMetadata") or {}).get("mergeCommitId")

    if not merge_commit_id:
        # Some merges land via a direct push to the destination branch rather than
        # CodeCommit's Merge Pull Request action (CodeCommit then auto-closes the
        # PR via UpdatePullRequestStatus), which never populates
        # mergeMetadata.mergeCommitId. Fall back to the branch's current tip - the
        # only value guaranteed to match what CodePipeline later reports as its
        # source revision for this deployment.
        try:
            merge_commit_id = codecommit.get_branch(
                repositoryName=repo_name, branchName=branch
            )["branch"]["commitId"]
        except Exception as exc:
            print(f"WARNING: could not resolve branch tip for {repo_name}@{branch}: {exc}")

    title = pr.get("title") or "(no title)"
    author_arn = pr.get("authorArn", "")
    author = author_arn.split("/")[-1] if author_arn else "unknown"

    card_payload = _build_card(repo_name, branch, pr_id, title, author)

    chat_threads = []
    for chat in targets:
        try:
            response = _post_to_chat(chat["webhook_url"], card_payload)
        except Exception as exc:
            print(f"ERROR posting to chat '{chat['name']}': {exc}")
            continue

        thread_name = (response.get("thread") or {}).get("name")
        if not thread_name:
            print(f"WARNING: no thread name in response from chat '{chat['name']}':", json.dumps(response))
            continue

        chat_threads.append({"chat_name": chat["name"], "thread_name": thread_name})

    if not chat_threads:
        print("No chat posts succeeded, nothing to store")
        return

    if not merge_commit_id:
        print(f"WARNING: no mergeCommitId found for PR {pr_id}, cannot link a future deployment reply")
        return

    ttl = int(time.time()) + THREAD_TTL_DAYS * 86400
    table.put_item(
        Item={
            "commit_sha": merge_commit_id,
            "repository": repo_name,
            "branch": branch,
            "pull_request_id": str(pr_id),
            "pr_title": title,
            "chat_threads": chat_threads,
            "created_at": int(time.time()),
            "ttl": ttl,
        }
    )
    print(f"Stored thread mapping for {merge_commit_id}: {chat_threads}")
