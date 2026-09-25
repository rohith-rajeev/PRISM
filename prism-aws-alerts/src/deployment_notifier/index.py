import json
import os
import urllib.error
import urllib.request

import boto3

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
CHAT_TARGETS = json.loads(os.environ["CHAT_TARGETS_JSON"])
CHAT_TARGETS_BY_NAME = {t["name"]: t for t in CHAT_TARGETS}
PIPELINE_BRANCHES = json.loads(os.environ.get("PIPELINE_BRANCHES_JSON", "{}"))
PIPELINE_REPOSITORIES = json.loads(os.environ.get("PIPELINE_REPOSITORIES_JSON", "{}"))
POST_UNLINKED = os.environ.get("POST_UNLINKED_DEPLOYMENTS", "true").lower() == "true"
STATUS_BY_STATE = {"SUCCEEDED": "succeeded", "FAILED": "failed"}
ENVIRONMENT_LABELS = {"qa": "QA", "staging": "Staging", "main": "Production"}

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
codepipeline = boto3.client("codepipeline")
codecommit = boto3.client("codecommit")


def _targets_for_branch(branch):
    return [t for t in CHAT_TARGETS if branch in t.get("branches", [])]


def _post_to_chat(webhook_url, payload, thread_name):
    url = webhook_url
    if thread_name:
        sep = "&" if "?" in url else "?"
        # Valid values per the Chat API's messageReplyOption enum are
        # REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD and REPLY_MESSAGE_OR_FAIL - there is
        # no REPLY_MESSAGE_OR_NEW_THREAD_FALLBACK; sending that made-up value got a
        # flat 400 from every threaded reply attempt.
        url = f"{url}{sep}messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{exc}: {body}") from exc


def _get_commit_author(repo_name, commit_sha):
    """Look up who authored the deployed commit, straight from CodeCommit.

    Resolved independently of the PR-merge DynamoDB thread so the author still
    shows up even when that thread was never recorded (e.g. the merge alert
    itself failed to post) - which is the scenario this is for.
    """
    try:
        resp = codecommit.get_commit(repositoryName=repo_name, commitId=commit_sha)
        author = resp.get("commit", {}).get("author", {})
        return author.get("name") or author.get("email")
    except Exception as exc:
        print(f"ERROR fetching commit author for {repo_name}@{commit_sha}: {exc}")
        return None


def _build_message(environment, component, status, pr_id, commit_sha, thread_name, author=None):
    header = f"{environment} deployment {status}"
    if component:
        header += f" ({component})"
    lines = [header]
    if pr_id:
        lines.append(f"- PR #{pr_id}")
    if commit_sha:
        lines.append(f"- Commit: {commit_sha[:12]}")
    if author:
        lines.append(f"- Author: {author}")

    payload = {"text": "\n".join(lines)}
    if thread_name:
        payload["thread"] = {"name": thread_name}
    return payload


def handler(event, context):
    detail = event.get("detail", {})
    pipeline_name = detail.get("pipeline")
    execution_id = detail.get("execution-id")
    state = detail.get("state")
    status = STATUS_BY_STATE.get(state)

    if not status or not pipeline_name or not execution_id:
        print("Ignoring event:", json.dumps(detail))
        return

    pipeline_parts = pipeline_name.split("-")
    env_key = pipeline_parts[-1]
    environment = ENVIRONMENT_LABELS.get(env_key, env_key.title())
    component = pipeline_parts[1].title() if len(pipeline_parts) > 2 else ""

    commit_sha = None
    try:
        resp = codepipeline.get_pipeline_execution(
            pipelineName=pipeline_name, pipelineExecutionId=execution_id
        )
        revisions = resp.get("pipelineExecution", {}).get("artifactRevisions", [])
        if revisions:
            commit_sha = revisions[0].get("revisionId")
    except Exception as exc:
        print(f"ERROR fetching pipeline execution details: {exc}")

    item = None
    if commit_sha:
        item = table.get_item(Key={"commit_sha": commit_sha}).get("Item")

    repo_name = (item or {}).get("repository") or PIPELINE_REPOSITORIES.get(pipeline_name)
    author = _get_commit_author(repo_name, commit_sha) if repo_name and commit_sha else None

    if item:
        pr_id = item.get("pull_request_id")
        for entry in item.get("chat_threads", []):
            chat = CHAT_TARGETS_BY_NAME.get(entry.get("chat_name"))
            if not chat:
                print(f"WARNING: stored chat '{entry.get('chat_name')}' is no longer configured, skipping")
                continue
            payload = _build_message(
                environment, component, status, pr_id, commit_sha, entry.get("thread_name"), author
            )
            try:
                _post_to_chat(chat["webhook_url"], payload, entry.get("thread_name"))
                print(f"Posted threaded deployment reply to chat '{chat['name']}'")
            except Exception as exc:
                print(f"ERROR posting to chat '{chat['name']}': {exc}")
        return

    if not POST_UNLINKED:
        print(f"No PR thread found for commit {commit_sha}, skipping (POST_UNLINKED_DEPLOYMENTS=false)")
        return

    branch = PIPELINE_BRANCHES.get(pipeline_name)
    targets = _targets_for_branch(branch) if branch else []
    if not targets:
        print(f"No chat_targets watch branch '{branch}' (pipeline {pipeline_name}), nothing to post")
        return

    for chat in targets:
        payload = _build_message(environment, component, status, None, commit_sha, None, author)
        try:
            _post_to_chat(chat["webhook_url"], payload, None)
            print(f"Posted standalone deployment message to chat '{chat['name']}' (no matching PR thread)")
        except Exception as exc:
            print(f"ERROR posting to chat '{chat['name']}': {exc}")
