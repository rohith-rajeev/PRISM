# PRISM ⇄ code-generation tool integration

Some teams generate changes with an automated code-generation tool that takes
requirements through design, implementation and testing and ends with a pull
request. This document calls any such tool **codegen**. PRISM takes over from the
pull request. The integration lets the two hand a PR back and forth
**automatically** until PRISM approves and merges it:

```
codegen builds ─▶ PR exists ─▶ codegen hands it to PRISM ─▶ PRISM reviews
       ▲                                                        │
       │                                         ┌──────────────┴─────────────┐
       │                                changes requested               approved
       │                                         │                            │
       └─ codegen fixes and pushes ◀─ findings go back (round + 1)     PRISM merges
          to the same PR branch                                        and tells codegen
```

It is **optional on both sides**. PRISM works exactly as before with it off (the
default), codegen works exactly as before with its half off, and either can be
running without the other.

PRISM knows nothing about any particular tool. **Anything that speaks the
protocol below can be the other half**: it finds PRISM through a discovery file,
submits a review request over loopback HTTP, and receives the verdict by
callback or by polling. This document is PRISM's half and the shared wire
contract (protocol version 1); the other half is whatever small adapter the
code-generation tool ships.

## Turning it on

**Help → Code-generation integration → "Accept reviews from a code-generation tool."** That is the whole
setup. It is saved in `~/.prism/config.json` under a `codegen` key:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Listen on loopback for reviews from codegen. Only a literal `true` enables it. |
| `auto_merge` | `true` | May PRISM merge an approved PR that codegen handed over? Off = review only; codegen is told "approved" and a person merges. The checkbox **Merge approved pull requests from it** sets this. |
| `max_iterations` | `5` | Review rounds one PR may take before the loop stops for a person (1–50). |
| `port` | `0` | `0` lets the OS pick a free port. It is published in the discovery file, so nothing needs to know it. |

The status line under the checkbox shows `✓ Listening on 127.0.0.1:<port>`, or
why it could not start. A failure to start never affects the rest of PRISM.

## What PRISM does with a request

A request becomes an **ordinary job** — it appears in the Jobs list marked
`via codegen` and runs through the same review → describe → merge pipeline as one
you started by hand. Nothing about the pipeline is relaxed:

- the reviewer is read-only and PRISM performs every write;
- the **high-impact human confirmation** still applies (impact ≥ 7 pauses the
  job in PRISM waiting for you, and codegen is told `needs-human` if you decline);
- the **"source branch moved since the review"** refusal still applies;
- merge is fast-forward only, and only for an OPEN PR with an approving verdict;
- one active job per PR, and a sync conflict is still put to you hunk by hunk.

The reviewer is told the PR came from codegen, what codegen was asked to build (so it
checks the PR delivers it), and — from round 2 — to confirm each earlier finding
was really fixed. Its verdict rules are unchanged.

## The loop and its stops

| PRISM's outcome | `decision` sent to codegen | What codegen does |
|---|---|---|
| Approved, merged | `merged` | Done. |
| Request changes | `changes-requested` | Fix the findings, push to the same branch, resubmit (round + 1). |
| Block | `blocked` | Same as above. |
| PR changed while being reviewed | `stale` | Resubmit; nothing to fix. |
| Approved, merge not allowed (`auto_merge` off) | `approved` | Stop; a person merges. |
| Impact gate declined, not fast-forwardable, PR closed, verdict unreadable | `needs-human` | Stop; a person acts in PRISM. |
| PRISM crashed on the review | `failed` | Stop; a person looks. |
| You pressed Stop on the job | `stopped` | Stop. |

Anything PRISM does not recognise is reported as `needs-human`, never as
something codegen should loop on. The loop is bounded three ways:

1. `max_iterations` — a request beyond it is refused with `iteration-limit`.
   PRISM keeps its own count per PR as well, so a codegen that forgets to
   increment the round cannot get around it.
2. **No new commits** — a resubmission whose PR source commit is the one PRISM
   already reviewed is refused with `no-new-commits`, so an agent that did not
   push can't burn a review.
3. A person pressing Stop on the job, or quitting PRISM.

## Wire contract (version 1)

All traffic is JSON over HTTP on `127.0.0.1`.

### Discovery

When enabled, PRISM writes `~/.prism/codegen-bridge.json` (mode `0600` on Linux and macOS; on Windows, which has no such mode, it inherits the permissions of your user profile folder, which is private to your account by default) and removes
it on exit:

```json
{"app": "prism", "protocol": 1, "version": "3.5", "url": "http://127.0.0.1:41873",
 "token": "<random, per launch>", "pid": 12345, "started_at": 1790000000}
```

No file means PRISM is not running (or the integration is off). Every request
needs `Authorization: Bearer <token>`.

### `POST /v1/reviews` — ask for a review

```json
{
  "protocol": 1,
  "request_id": "codegen-acme-be_214-i1-9f2c1a7e",
  "repo_name": "acme-be",
  "pr_id": 214,
  "region": "us-east-1",
  "local_repo": "/home/me/work/acme-be",
  "iteration": 1,
  "run_ref": "run-20260925-01",
  "callback": {"url": "http://127.0.0.1:50211/prism/callback", "token": "<codegen's own>"},
  "context": {"requirements": "Add a --version flag to the CLI"},
  "options": {"auto_merge": false}
}
```

Required: `request_id`, `repo_name`, `pr_id`, `local_repo` (absolute path of a git
clone). Everything else is optional. `request_id` is the idempotency key: a
retried POST returns the original review (`200`, `"duplicate": true`) instead of
queueing another. `options.auto_merge` can only turn merging **off**; it can
never override PRISM's own setting. `callback.url` must be a loopback address —
anything else is refused (`bad-callback`), so a request cannot make PRISM call
out to the network.

| Status | Body | Meaning |
|---|---|---|
| `202` | `{review_id, job_id, state: "queued", iteration}` | Accepted. |
| `200` | same, `duplicate: true` | This `request_id` was already accepted. |
| `400` | `{error, message}` | Invalid input (`bad-request`, `bad-callback`, `unsupported-protocol`). |
| `401` / `403` | | Bad token / bad `Host` header. |
| `409` | `already-reviewing` | That PR already has an active job. |
| `409` | `iteration-limit` | Round ceiling reached. |
| `409` | `no-new-commits` | Nothing pushed since the last review. |
| `503` | `busy` | The PRISM window did not answer in time. |

### Outcome (callback body and `GET /v1/reviews/{review_id}`)

```json
{
  "protocol": 1, "review_id": "rv_3c7ba89be2cd8fb4", "request_id": "…", "run_ref": "…",
  "repo_name": "acme-be", "pr_id": "214", "iteration": 1, "revision": 1,
  "state": "finished", "decision": "changes-requested", "merged": false,
  "verdict": "request-changes", "verdict_raw": "🔴 Request changes",
  "impact_score": "4", "impact_reason": "…",
  "findings": ["- **[High]** calc.py:9 — off by one"],
  "feedback": "PRISM reviewed this pull request … Fix every finding below …",
  "reason": "", "finished_at": 1790000123
}
```

`feedback` is non-empty only for `changes-requested` / `blocked`. `revision`
increases if a person retries the job in PRISM, so the receiver can tell a new
result from a duplicate delivery. While a review is still running the GET
returns `{state: "queued" | "running" | "needs-input" | …}` with no `decision`.

PRISM POSTs the outcome to `callback.url` (bearer = `callback.token`), retrying a
few times with backoff; a `4xx` is final. **Delivery is best-effort — the
authoritative copy is always available to poll**, and codegen polls as a safety
net. The callback waits until the job reads as finished in PRISM first, so a
resubmission sent the instant codegen hears can never be refused as a duplicate.

### `POST /v1/reviews/{review_id}/cancel`

Stops the review's job. `202 {"cancelled": true}`, or `false` if it had already
finished.

## Security

- **Opt-in, loopback-only.** Off by default; when on, the server binds
  `127.0.0.1` and checks the `Host` header (DNS-rebinding defence).
- **Bearer token** generated per launch, constant-time compared, readable only by
  the local user (`0600` discovery file).
- **Inputs are validated once, at the door**: repo name, PR id, region and
  request id against strict patterns; paths must be absolute, existing git
  clones; bodies are capped at 256 KB; free text is length-bounded.
- **Callbacks are loopback-only**, so PRISM can't be pointed at the LAN or a
  cloud metadata address.
- **codegen can ask for less, never more.** It can turn merging off for a request;
  it cannot turn it on, skip the review, change the model, or bypass any gate.
- **The merge decision is still PRISM's**, made by the same code and gates as a
  manual job. codegen's text reaches the reviewer only as clearly marked context.
- Anyone who can read the discovery file can already act as the local user, so
  the token defends against *other processes' and web pages'* requests, not
  against the machine's owner.

## State and restarts

PRISM stays stateless: finished reviews are kept **in memory only** (the last
200) so they can be polled. If PRISM restarts mid-review, codegen's poll gets
`404`, and codegen resubmits under the same `request_id` — PRISM treats it as a
fresh review (and its own record of the last reviewed commit, in
`~/.prism/`, still makes the follow-up review incremental). Quitting PRISM asks
for confirmation as before; jobs from codegen count as ordinary jobs there.

If two PRISM windows are open, the one started last owns the discovery file.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Status says "Could not start: …" | A fixed `port` is taken, or `~/.prism` is not writable. Set `port` back to `0`. |
| Nothing is ever submitted | PRISM's integration is off, or PRISM was not running — codegen retries for a while, then tells its session. |
| `no-new-commits` | codegen's agent finished a round without pushing to the PR branch. |
| `iteration-limit` | The PR has used its rounds; review it by hand or raise `max_iterations`. |
| Job sits at "Needs input" | The impact gate or the reviewer is waiting for **you** in PRISM. |
| codegen shows `callback: failed` | codegen was unreachable; it recovers on its next poll. |

## Code map

- `codegen_bridge.py` — server, validation, outcome shaping, callback delivery
  (stdlib only, no Tkinter, unit-tested).
- `jobs.py` — `JobSpec.origin` / `origin_ref` (display only; absent from
  `pipeline_kwargs()`) and the optional `on_outcome` hook. With no hook the
  scheduler behaves exactly as before.
- `config.py` — `get_codegen()` / `set_codegen()`.
- `app.py` — the Help card, the bridge's lifecycle, and `_drain_codegen`, which
  admits requests **on the UI thread** (the only thread allowed to mutate jobs).
- `tests/test_codegen_bridge.py` — validation, the HTTP surface end to end, the
  loop's guards, and that a job with no bridge is untouched.
