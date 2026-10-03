# Consistent reviews of the same change

## The problem

The same change often gets reviewed more than once: a retry, another pass on the
same PR after edits, or another PR carrying it into a different branch. How that
happens depends on the project's branching strategy — it might be a chain of
environment branches, a release branch, a hotfix line or nothing like either —
and PRISM assumes none of it. Reviewing each pass from scratch gave different
findings each time. A defect missed on the first pass could surface on a later
review, after the code had already been tested on the strength of the first, so
the fix for it was never covered by that testing.

## Why it happened

1. **No memory between reviews.** The only continuity PRISM had was the
   incremental review, which applies to the *same PR id* when the earlier
   reviewed commit is an ancestor of the current one. A different PR, a retry on
   an unchanged commit, or the same change on another branch was reviewed from
   scratch, as if new, by a model that explores freely and so notices different
   things on different runs.
2. **Open-ended coverage.** The reviewer was told six dimensions to consider but
   not *how* — no procedure for failure paths, for who depends on a changed
   contract, or for whether a bug is one instance of a repeated pattern. What it
   found first was partly luck, and luck differs run to run.
3. **No signal for "this should have been caught earlier".** A defect found late
   in already-reviewed code looked like any other finding, so nobody could tell
   that its fix would be untested downstream.

## What changed

Context from earlier passes alone would not fix this: it makes later reviews
consistent but cannot make the first one exhaustive. So there are two halves.

### Half 1 — a review memory that recognises the same change

**PRISM only reviews. Nothing here writes to any repository, branch or clone** —
not a file, not a commit, not a ref. The memory is one small file in a per-user
directory under the OS temp folder (see "Where it is kept").

After each review PRISM remembers its findings together with a **fingerprint of
the change it reviewed**: the diff is taken with zero context lines and split
into hunks, and each hunk is reduced to a short hash of its file path plus its
added and removed lines (whitespace normalised; line numbers, blob ids and
rename detection play no part). When a PR is reviewed, PRISM fingerprints it the
same way and finds earlier reviews whose hunks recur in it. Matching by content
rather than commit id is what makes it survive the things that change shas:

| Situation | Matched? |
|---|---|
| The same commits moved to another branch (fast-forward) | yes — by hunks, and by sha as a shortcut |
| Rebase onto a moved base | yes — every sha changes, the hunks don't |
| Squash | yes |
| Cherry-pick onto another branch | yes |
| A PR that bundles several changes (A reviewed alone, this PR holds A+B+C) | yes — A's hunks are all present |
| Follow-up fix on top of reviewed code | yes — the unchanged hunks match, the edited ones don't |
| Heavily reworked code, or a conflict resolution that rewrites the lines | partly, or not at all — correctly, it is no longer the same change |
| Unrelated PR, whichever branch it targets | no (and a single shared boilerplate hunk is not enough: at least 25% of the earlier review's hunks must recur) |

The reviewer receives the matched findings as a clearly marked `REVIEW HISTORY`
block. Each finding is tagged `[same code in this PR]` or `[code has changed
since]` where PRISM can tell (a hint only). The reviewer is told to:

- review the whole diff independently anyway — history is **never** an approval
  and never narrows scope or lowers the bar (config, migrations, IaC and flags
  differ per environment, so they get a full look even when the code is identical);
- account for every earlier finding: fixed, partly fixed, or still present, with
  evidence (line numbers in old findings may have shifted; it re-reads the code);
- stay consistent — not reverse a conclusion about unchanged code without saying
  what the earlier review got wrong;
- mark anything the earlier reviews **missed in code they had covered** as a
  **late find**: `(late find — missed in PR #n)`.

### Half 2 — a systematic first pass (the part that reduces late finds)

The reviewer's instructions (`agents/pr-reviewer.md`) now require, on every
review, including the very first:

- **2a Failure handling and edge cases**, per changed function: unusual inputs;
  every call that can fail (timeout, error status, partial or malformed result,
  retry safety/idempotency); exception handling itself (swallowed or over-broad
  catches, missing cleanup, half-written state, un-awaited async, leaked
  internals); concurrency and state; and the unhappy path end to end.
- **2b Impact of the change**: that the PR is consistent with itself (callers it
  also changes are updated, a schema change has its migration, new config exists
  for the environments it targets), and — only for a contract the PR changes whose
  callers the diff doesn't show — one targeted look at its direct usages, reported
  only if the change breaks them.
- **2c The same mistake more than once in this PR**: when a finding is an instance
  of a pattern, the reviewer checks whether the rest of *this PR's diff* repeats
  it and says so on the same line (`Same pattern also at: …`), so the fixer
  repairs every occurrence in the PR at once.
- a `Coverage:` line stating what was checked and what could not be verified, so
  a gap is stated rather than silent.

### When PRISM finds something late

PRISM counts findings tagged as late finds. When there are any it logs
`⚠ N late finding(s) …` in the job and adds the same warning to the PR
description's PRISM block.

A late fix is not covered by the testing that followed the earlier review, so the
reviewer weighs it: **Critical or High** (data loss, security, a crash or wrong
result on a common path) still means **Request changes**; a **Medium, Low or Nit**
late find is reported but on its own leaves the verdict at **Approve with
comments**, to be fixed in the next normal cycle rather than as an untested
last-minute change. This is a judgment rule in the reviewer's instructions
(Step 0.6) — see "Tuning" if you would rather it block.

**The process answer is still yours:** a defect found late should be fixed where
the code is first tested and carried forward from there, not patched in at the
last point. PRISM can flag a late find; it cannot make that happen.

## Where it is kept

`<OS temp folder>/prism-<your user id>/review_history.json` — for example
`/tmp/prism-1000/review_history.json` on Linux. It is deliberately short-lived
and machine-local:

- the OS may clear the temp folder (on many Linux systems at every reboot); PRISM
  also drops anything older than 30 days and keeps at most 150 reviews;
- when it is gone PRISM simply has no memory and reviews as it did before;
- `PRISM_HISTORY_DIR` can point it somewhere else if you want it to last longer.

Because the temp folder is shared between users, PRISM trusts that directory only
if it is a real directory (not a symlink), owned by you, and not writable by
anyone else; otherwise it neither reads nor writes it. The file is mode 0600 on Linux and macOS; Windows has no such mode, and its per-user temporary folder is already private to your account.

## What this does not do

- **It does not make the reviewer deterministic.** It narrows variance and makes
  the remaining differences visible; a model can still miss something.
- **It is machine-local.** A review done on a different machine, or after the
  temp folder was cleared, has no record of the earlier ones. That is the accepted
  trade for PRISM never writing to a repository.
- **It cannot match code that has really been rewritten** between reviews, nor
  hunks whose lines a conflict resolution changed. A very large diff is
  fingerprinted only up to 2,000 hunks.
- **Late-find tagging relies on the reviewer following its instructions.** An
  untagged late find still appears as an ordinary finding; it just isn't flagged.
- **It stays inside the PR.** The reviewer looks at the PR's diff and what that
  diff newly breaks, nothing else, and is told to keep the review fast: no
  repository-wide searches, no auditing of code the PR doesn't touch.

## Safety — why this cannot destabilise PRISM

- **No repository is touched.** Fingerprinting runs `git diff` and `git rev-list`
  (read-only) after a read-only fetch, the same fetch the reviewer itself does. A
  test snapshots the working tree, local refs, HEAD and the remote's refs before
  and after and asserts they are identical.
- Every new step is best-effort and falls back to **exactly the previous
  behaviour**: no memory, no git, a failed fetch, an untrustworthy directory, a
  corrupt file or no matching review all yield an empty history block, i.e. the
  prompt PRISM sent before. Recording a review is wrapped twice and can never fail
  a job.
- History is **context only**. It feeds no merge decision, no gate and no verdict
  parser; the merge gates (verdict, OPEN, fast-forward, source unchanged,
  high-impact confirmation) are untouched, and the verdict still comes only from
  the reviewer's own report.
- Nothing a PR author writes can forge it: it is not in any repository or PR
  description. Its content originates from reviewing untrusted diffs, so it reaches
  the reviewer fenced and labelled as data and length-bounded (3 reviews, 12
  findings each, 6000 characters), like every other PR-derived text.
- It is **bounded** (150 reviews; 200 commits, 2,000 hunk hashes and 25 findings
  each) and written temp-then-rename.
- The report format PRISM parses is unchanged. The only parser change is raising
  the per-finding ceiling from 800 to 1500 characters, so a finding can carry its
  "same pattern also at" note on its one line.

## Tuning

- To make late Medium/Low findings block as well, delete the "Weigh late finds by
  what a late fix costs" bullet in `agents/pr-reviewer.md` Step 0.6.
- To forget everything: delete the `review_history.json` file above.
- To turn the whole feature off: remove Step 0.6 and the `_review_history_context`
  call in `run_opencode_review`; nothing else depends on it.

## Code map

- `orchestrator.py` — `_history_dir` (location and trust checks), `_diff_hunks`
  (fingerprints), `_record_review_history`, `related_reviews` (matching),
  `history_block`, `_review_history_context`, `count_late_findings`;
  `run_opencode_review` adds the block to the prompt; `_run_pipeline` records the
  review and surfaces late finds; `update_description_direct(late=…)`.
- `agents/pr-reviewer.md` — Step 0.6, Step 2 (2a/2b/2c), the report format.
- `tests/test_review_history.py` — the memory, content matching on real git
  repositories (rebase, squash, cherry-pick, a PR bundling other changes, follow-up fix),
  prompt wiring, late finds, the "writes nothing to the repo" proof, and pins on
  the reviewer's instructions.
