import json
import os
import urllib.request

import boto3

TABLE_NAME = os.environ["DYNAMODB_TABLE_NAME"]
CHAT_TARGETS = json.loads(os.environ["CHAT_TARGETS_JSON"])
CHAT_TARGETS_BY_NAME = {t["name"]: t for t in CHAT_TARGETS}
PIPELINE_BRANCHES = json.loads(os.environ.get("PIPELINE_BRANCHES_JSON", "{}"))
POST_UNLINKED = os.environ.get("POST_UNLINKED_DEPLOYMENTS", "true").lower() == "true"
STATUS_BY_STATE = {"SUCCEEDED": "succeeded", "FAILED": "failed"}

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
codepipeline = boto3.client("codepipeline")


def _targets_for_branch(branch):
    return [t for t in CHAT_TARGETS if branch in t.get("branches", [])]


def _post_to_chat(webhook_url, payload, thread_name):
    url = webhook_url
    if thread_name:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}messageReplyOption=REPLY_MESSAGE_OR_NEW_THREAD_FALLBACK"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=UTF-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_message(environment, status, pr_id, commit_sha, thread_name):
    lines = [f"{environment} deployment {status}:"]
    if pr_id:
        lines.append(f"- PR #{pr_id}")
    if commit_sha:
        lines.append(f"- Commit #{commit_sha[:12]}")

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

    environment = pipeline_name.split("-")[-1].title()

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

    if item:
        pr_id = item.get("pull_request_id")
        for entry in item.get("chat_threads", []):
            chat = CHAT_TARGETS_BY_NAME.get(entry.get("chat_name"))
            if not chat:
                print(f"WARNING: stored chat '{entry.get('chat_name')}' is no longer configured, skipping")
                continue
            payload = _build_message(
                environment, status, pr_id, commit_sha, entry.get("thread_name")
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
        payload = _build_message(environment, status, None, commit_sha, None)
        try:
            _post_to_chat(chat["webhook_url"], payload, None)
            print(f"Posted standalone deployment message to chat '{chat['name']}' (no matching PR thread)")
        except Exception as exc:
            print(f"ERROR posting to chat '{chat['name']}': {exc}")
