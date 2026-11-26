# PRISM v7.0 — Functional requirements

**Status:** ready to build  
**Baseline:** PRISM 6.5.1, as shipped (desktop app, command line, and `prism-aws-alerts`)  
**Audience:** the person implementing v7.0, and the person accepting it  

This document specifies only what v7.0 adds or changes. Behavior that is not named here stays as it is in 6.5.1. Where a rule below conflicts with 6.5.1, v7.0 follows this document.

Each requirement has an id (`FR-…`). A requirement is done when its acceptance scenarios pass and the edge cases in the same section are handled.

---

## 1. What v7.0 is for

v7.0 lets PRISM review GitLab merge requests the same way it reviews CodeCommit pull requests, makes document reviews usable when the file is not already on a branch, and makes the outcome of a document review unambiguous to the person who has to act on it.

Eight changes:

| Id | Change | Why it is in this release |
|---|---|---|
| FR-GL | Review and merge GitLab merge requests | High priority. 6.5.1 only speaks CodeCommit. |
| FR-DOC-IN | Document review from a dropped or uploaded file | Reviews today require a path already in a clone. |
| FR-OWN | Name the owner of a document review request | Chat messages do not say who should act. |
| FR-CARD | A shorter, grouped document-review chat card | The current card is one long stack and fills the screen. |
| FR-VER | Document decision is Approve or Request changes | “Approve with comments” reads as permission to proceed while major findings are still open. |
| FR-SCALE | Reviewers do not approve a very large or expensive change | A high impact score still allows Approve, and merge then waits for a person to confirm. |
| FR-CHAT | Live chat inside the job details of both a pull-request review and a document review | Available in any review state. Answers come from the open job’s details. Generic questions are declined. |
| FR-ALERT | A separate chat group for `develop` merges and deployments | Release chats (qa, staging, main) should not also carry develop traffic. |

---

## 2. Baseline the new work sits on

These facts are true in 6.5.1 and stay true unless a requirement below says otherwise.

- A pull-request job reviews one CodeCommit pull request and can update its description, sync the source branch with the destination, and fast-forward merge. It never force-pushes and never merges a request-changes or block verdict.
- A document-review job reads a spec or requirement-decomposition document on a branch, through one persona (Business Analyst, Architect, or QA). PRISM writes a review-comments section into the document, and can commit and push that section from a temporary worktree. The user’s own checkout is left alone.
- Document comments keep their severities: Blocker, Major, Minor, Suggestion. Security, stability, and scalability are always checked.
- The person who runs PRISM is signed onto the review (“Reviewed by”). That name is the app username, asked for once.
- When a document review finishes, a Google Chat card is posted to the document-review webhook, if one is configured. The card lists the persona, reviewer, verdict, impact, comment counts, then one branch row and one document block per repository.
- A pull-request verdict is one of Approve, Approve with comments, Request changes, or Block, plus an impact score from 1 to 10. Impact of 7 or higher pauses an otherwise mergeable approval and asks a person to proceed or abort.
- After a job has finished, the user can ask the reviewer a read-only follow-up about that job. While a job is running, a separate box sends a steering note into the review (capped at four rounds).
- `prism-aws-alerts` posts a Google Chat card when a CodeCommit pull request merges into a watched branch, then a threaded reply when the matching CodePipeline deployment succeeds or fails. Which spaces hear which branches is a list of chat targets. The example targets watch `qa`, `staging`, and `main`.

---

## 3. Document review — files that are not already on a branch

**FR-DOC-IN.** A document review can start from a file the user drops on the form or chooses with an upload control, as well as from a path already in a repository.

### 3.1 What the user can do

**FR-DOC-IN-1.** On the document-review form, each repository’s document list accepts files in three ways, and all three may be used in the same job:

- a path typed from the repository root (unchanged);
- Browse, which may select several files (unchanged for files inside the repository);
- dragging files from the desktop onto the document list, or choosing them with an Upload control.

**FR-DOC-IN-2.** A dropped or uploaded file is listed by its file name before the job starts. The user can remove it. Starting the job reviews that file even when it is not committed on the selected branch.

**FR-DOC-IN-3.** One drop or one upload may contain several files. They join the list; they do not replace files already listed.

**FR-DOC-IN-4.** Accepted types are the types a document review can already read: plain text, Markdown, Word (`.docx`), PDF, and Excel (`.xlsx`). Any other type is named in the form and is not added. A file that cannot be read (password-protected, scanned PDF with no text) is reported on the job and is not treated as reviewed.

**FR-DOC-IN-5.** The command line accepts the same files. A path that is not inside the repository is an uploaded document. A path inside the repository stays a repository document.

### 3.2 What the review is judged against

**FR-DOC-IN-6.** When the job has a repository and a branch, the reviewer still reads that branch’s codebase as the context for the uploaded file, the same way it does for a file already on the branch. Requirements, when a requirements folder is given, apply to both kinds of document.

**FR-DOC-IN-7.** When the job has no repository, the review still runs. It judges the uploaded document against itself and against the requirements folder if one was given. The review states that no codebase was available. It does not invent a repository.

### 3.3 Where the comments go

**FR-DOC-IN-8.** A document that already lives on the selected branch keeps today’s write path: comments go into that file in a temporary worktree, and Commit and Push behave as they do now.

**FR-DOC-IN-9.** An uploaded file that is not on the branch is still reviewed. PRISM writes the review-comments section into a copy of that file when the format can hold it (text, Markdown, Word, Excel). A PDF receives a sidecar Markdown file next to the uploaded file, named so it is obvious which PDF it belongs to, containing the same review section. The original PDF is not modified.

**FR-DOC-IN-10.** An uploaded file is not committed or pushed unless the user has chosen a repository, a branch, and a destination path inside that repository, and Commit is on. The destination path is empty until the user sets it. PRISM does not guess a path and does not add the file to the branch on its own.

**FR-DOC-IN-11.** Dry run reviews the file and shows the comments in the job. It does not write the copy, the sidecar, the branch, or the chat card.

### 3.4 Acceptance

1. **Given** a Markdown spec that exists only on the desktop, **when** the user drops it onto a document review that has a branch selected, **then** the review runs against that branch’s code, the comments are written to a local copy, and the branch is unchanged.
2. **Given** the same file and a destination path the user filled in, with Commit and Push on, **when** the review finishes, **then** that path on the branch contains the document plus the review section, pushed without force.
3. **Given** a `.png` dropped on the list, **when** the drop completes, **then** the file is not added and the form names it.
4. **Given** an uploaded PDF that has extractable text, **when** the review finishes with Commit off, **then** a sidecar Markdown file holds the comments and the PDF bytes are unchanged.
5. **Given** no working folder, **when** the user uploads a document and a requirements folder, **then** the review completes and says that no codebase was available.

---

## 4. Document review — who owns the request

**FR-OWN.** Every document-review message names the owner of the request, so a chat reader can see who should act.

The owner is the person responsible for the document and for answering the review. The reviewer is the person who ran PRISM. They are different fields. v6.5.1 only records the reviewer.

**FR-OWN-1.** The document-review form has an Owner field. The job does not start while Owner is empty. The command line requires the same value (`--owner`).

**FR-OWN-2.** Owner is a person’s name or username, the same kind of text as the app username (trimmed, no blank). It is stored on the job. The next document-review form is pre-filled with the last owner, and the user can change it.

**FR-OWN-3.** The Google Chat card shows Owner as its own labelled field, above the reviewer role, so it is the first fact a reader sees after the title. “Reviewed by” remains the person who ran PRISM. The card never uses one name for both.

**FR-OWN-4.** The job screen and the jobs list show the owner on a document review. The review section written into the document includes the owner on that version’s heading, beside the persona and the reviewer.

**FR-OWN-5.** Pull-request jobs do not gain an Owner field. Their existing “Reviewed by” line is unchanged.

### Acceptance

1. **Given** a document review with no owner, **when** the user starts it, **then** it does not start and the form says an owner is required.
2. **Given** owner “Priya” and reviewer “Alex”, **when** the chat card is posted, **then** the card shows Owner Priya and Reviewed by Alex.
3. **Given** a finished document review, **when** the user opens the job, **then** the owner is visible without opening the chat card.

---

## 5. Document review — chat card layout

**FR-CARD.** The document-review Google Chat card presents one decision, who owns it, and where the documents are, in a layout that a reader can scan without the card filling the screen.

Today the card is a single vertical list: role, reviewer, verdict, impact, comments, outcome, then for every repository a branch line of the form `Backend · long-repository-name → branch-name`, then a wrapped block of full paths with a verdict and comment counts on each line. Several repositories repeat that pattern until the card is taller than the chat window.

### 5.1 Information, in this order

**FR-CARD-1.** The card has three parts and no other part:

1. **Decision.** Title is “Document review”. The subtitle is the decision in words (Approve or Request changes) and the owner (“Owner: …”).
2. **Summary.** Reviewer role, reviewed by, comment counts, impact. These are one compact group, two facts per line where the chat card format allows columns. Comment counts are one line, for example `1 blocker, 2 major, 1 minor`. A zero count is omitted. Impact is `N/10` plus the one-line reason only when the reason is present; the reason is clipped to a single line.
3. **Documents.** One block per repository. The block heading is a single line: role, short repository name, branch. Under it, one line per document: file name and that document’s decision. A second line is added only when two documents in the card share a file name; that line adds the parent folder, not the full path from the repository root.

**FR-CARD-2.** The run id, the commit outcome (committed, pushed, dry run, kept locally), and a count of documents that could not be reviewed sit in a footer line. They are not separate full-width rows.

**FR-CARD-3.** At most five document lines are shown per repository. Further documents are one line: `and N more`. The job screen still lists every document.

**FR-CARD-4.** Repository names are shown as the last path segment when the full name is longer than 40 characters. The branch name is shown in full unless it exceeds 60 characters, in which case the start and the end are kept and the middle is ellipsized. File names are not ellipsized; the five-line cap is what keeps the card short.

**FR-CARD-5.** A one-repository review does not print the word “repository” or a role prefix that duplicates the only target. A two-repository review labels Backend and Frontend so the two blocks cannot be confused.

**FR-CARD-6.** Text that comes from file names, branch names, or the owner is shown as text. It is not interpreted as formatting.

### Acceptance

1. **Given** a review of two repositories, four documents, and long branch names, **when** the card is posted, **then** a reader can see the decision, the owner, and both branches without scrolling past a screenful of path text.
2. **Given** twelve documents in one repository, **when** the card is posted, **then** five document lines and `and 7 more` appear, and the job screen still lists twelve.
3. **Given** two files both named `spec.md` in different folders, **when** the card is posted, **then** each line includes the parent folder.
4. **Given** a file name containing `<` or `&`, **when** the card is posted, **then** the name is visible as the file name and does not change the card’s layout.

---

## 6. Document review — the decision is binary

**FR-VER.** A document review ends in exactly one of two decisions: **Approve** or **Request changes**.

Approve means work may start from this document. Request changes means work may not start until the open findings are addressed and the document is reviewed again. There is no third decision.

### 6.1 What is removed for documents

**FR-VER-1.** Document reviews no longer produce “Approve with comments” or “Block” as the decision. Those two labels do not appear on the document heading, the job, the jobs list, the command-line output, the findings export, or the chat card for any review run on v7.0.

**FR-VER-2.** Comment severities are unchanged: Blocker, Major, Minor, Suggestion. A Blocker is a comment, not a decision. The decision is computed from the comments.

**FR-VER-3.** Pull-request reviews keep their four verdicts (Approve, Approve with comments, Request changes, Block). This section applies only to document reviews.

### 6.2 How the decision is chosen

**FR-VER-4.** PRISM sets the decision. The reviewer’s own wording cannot override it.

- Any open Blocker or Major comment → **Request changes**.
- No open Blocker and no open Major (only Minor, only Suggestion, or no comments) → **Approve**.

“Open” includes a comment that is still open, partly resolved, or not assessed on an incremental review. Resolved and withdrawn comments do not count.

**FR-VER-5.** Minor and Suggestion comments are still written into the document when the decision is Approve. Approve means those comments do not stop the work. It does not mean the comments were deleted.

**FR-VER-6.** On an incremental review, the same rule is applied to the comments that remain after the verification pass. A previous version’s heading is left as it was written, including a historical “Approve with comments” from a 6.5.1 run. The new version uses only Approve or Request changes. The chat card and the job show the new version’s decision.

**FR-VER-7.** The chat card and the job state the meaning in the same words every time: Approve — work may start; Request changes — work may not start. The decision is not conveyed by an emoji alone.

### Acceptance

1. **Given** a document whose review finds one Major and two Minors, **when** the review is written, **then** the decision is Request changes, the Major is present, and the heading does not say Approve.
2. **Given** a document whose only comments are Suggestions, **when** the review is written, **then** the decision is Approve and the Suggestions are in the document.
3. **Given** the reviewer writes “Approve” in its report while a Blocker is in the comment list, **when** PRISM saves the review, **then** the saved decision is Request changes.
4. **Given** a document last reviewed on 6.5.1 as “Approve with comments” with a Major still open, **when** a v7.0 incremental review confirms that Major is still open, **then** the new version is Request changes and the old heading is still readable above it.
5. **Given** a pull-request review that warrants comments but no defect, **when** it finishes, **then** it may still be “Approve with comments”.

---

## 7. Reviewers do not approve a very large or expensive change

**FR-SCALE.** A change that is very large or very expensive to build, roll back, or review safely is never given an approval. Raising the impact score is part of the same judgment, and a later confirmation prompt cannot turn that change into an approval.

This applies to both reviewer agents: the pull-request reviewer and the document reviewer. It applies when nobody is present to confirm, including a dry run, a command-line run, and a code-generation request.

### 7.1 What “large or expensive” means

**FR-SCALE-1.** Every review classifies the change as **normal** or **large**. Large means the change is very large or expensive: it cuts across many modules, migrates data or schema, changes infrastructure or a pipeline, moves a security boundary, or (for a document) would be costly or risky to build as specified. The size of the diff in lines is not, by itself, enough to classify it as large, and a short diff is not automatically normal.

**FR-SCALE-2.** The classification is a line in the review report, `Scale: normal` or `Scale: large`, with one sentence saying why. PRISM reads that line. A report that omits it is asked once to supply it. If it is still missing and the impact score is 8 or higher, PRISM classifies the change as large. If it is still missing and the impact score is 7 or lower, PRISM classifies it as normal and records that the scale line was missing.

### 7.2 What PRISM does with the classification

**FR-SCALE-3.** A large change has an impact score of at least 8. If the reviewer reports a lower score, PRISM raises the stored score to 8 and keeps the reviewer’s reason, prefixed so it is clear the score was raised because the change is large.

**FR-SCALE-4.** A large change’s decision is not an approval.

- Pull request: the verdict is Request changes. Approve and Approve with comments are discarded. Block is kept when the reviewer reported Block.
- Document: the decision follows FR-VER, and a large document is Request changes even when it has no Blocker or Major comment. The document states that the decision is Request changes because the change is large or expensive, so a later reader does not think the writing was clean and the work may start.

**FR-SCALE-5.** A large change is not merged. PRISM does not show “Proceed with merge” for it. An answer of proceed, a missing confirmation callback, or a code-generation client cannot merge it. The job ends held, with the reason that the change is large or expensive.

**FR-SCALE-6.** A normal change is unchanged from 6.5.1, including the existing rule that impact of 7 or higher pauses for a person to confirm before merge. A normal change may be Approve. Confirmation remains a gate on risky approvals; it is not a way to approve a large change.

**FR-SCALE-7.** The agent instructions state FR-SCALE-1, FR-SCALE-3, and FR-SCALE-4. PRISM enforces them in code after the report is parsed, so a report that approves a large change does not get stored as an approval.

### Acceptance

1. **Given** a pull request the reviewer classifies as large and scores 4/10 with Approve, **when** the review is stored, **then** the verdict is Request changes, the impact is 8/10, and no merge confirmation is offered.
2. **Given** a large change and a code-generation client that asks PRISM to merge, **when** the review finishes, **then** the merge does not run.
3. **Given** a one-line authentication fix classified as normal with impact 8, **when** auto-merge is on, **then** PRISM pauses for confirmation and merges only if the person chooses proceed.
4. **Given** a document with no Blocker or Major that the reviewer classifies as large, **when** the review is written, **then** the decision is Request changes and the section says the change is large or expensive.
5. **Given** a report with impact 9 and no Scale line, **when** the reviewer does not supply a scale after one prompt, **then** the change is stored as large and is not approved.

---

## 8. Live chat inside the job details

**FR-CHAT.** Both kinds of job have a live chat inside the job details: a pull-request review and a document review. The user can ask it about the job that is open at any moment, in any state of that review. Every answer comes from that job’s details. A question that is not about this job is outside the chat’s scope.

The panel lives inside the job details screen, in the area that today is the conversation log. It is not a separate window, not a jobs-list control, and not limited to one tab of the new-job form. That log is titled **OUTPUT** on every job details screen, whether or not the chat is open. The chat starts collapsed, so the log keeps today’s size until the user expands the chat.

6.5.1 already has two related controls, and both stay, on both kinds of job:

- a steering note, while the job is running, which is delivered into the review and is capped at four rounds;
- a read-only follow-up after the job has finished.

The live chat is a third surface. It answers questions about the open job. It does not steer the review, and the review’s state does not turn it on or off. A document review uses the document reviewer; a pull-request review uses the pull-request reviewer. The panel, the rules, and the place it sits on the details screen are the same for both.

**FR-CHAT-1.** The job details screen has a live chat for the job that is open, whether that job is a pull-request review or a document review. The chat accepts a question in every state that job can be in, including queued, running, waiting on the reviewer, merging, held, stopped, failed, and finished. A state never greys the chat out or waits for a verdict first. Switching to another job’s details shows that job’s chat, including a switch from a pull-request job to a document job and the reverse, and keeps that job’s expanded or collapsed state. The jobs list does not host the chat.

**FR-CHAT-1a. Titles.** The log that 6.5.1 labels **CONVERSATION** is labelled **OUTPUT** at all times, on both a pull-request job and a document job, with the chat collapsed or expanded. The title does not change when the chat opens or closes.

**FR-CHAT-1b. Layout.** The chat shares that output section, and only that section.

- Collapsed: the section keeps today’s size and shows only **OUTPUT**. The rest of the job details (progress, verdict, questions, buttons) do not move to make room for a chat that is closed.
- Expanded: that same section splits horizontally, 70% and 30%. The 70% side stays **OUTPUT** and keeps the reviewer’s log. The 30% side is titled **CHAT**. The split and both titles are the same on a pull-request job and a document job.
- Collapsing again restores the single **OUTPUT** section. The chat thread is kept and reappears on the next expand.
- The split follows the section’s width. Narrower windows keep the 70/30 ratio; they do not stack the chat under the output or hide the output to make the chat full width.

**FR-CHAT-2.** A reply is based on the current job details, as they stand when the question is asked: status, settings, repository and branch or merge request, owner, verdict or decision, impact, scale, findings, document comments, coverage, and what **OUTPUT** has already shown. Accuracy to those details comes before completeness. A name, id, file, severity, or verdict in the reply matches the job. Where the job details do not contain the answer, the reply says so. It does not fill the gap from general knowledge, from another job, or from a guess about what the review will conclude. When the review has not produced findings yet, the reply says that and answers only from the details already on the job. A later question uses the details as they are then, including findings that have appeared since the previous reply.

**FR-CHAT-3.** The live chat is read-only. An answer cannot change the verdict, the decision, the impact, the document, the branch, or the merge. A question that asks it to approve, merge, edit, or ignore a finding is declined in a sentence that points the user at the existing controls (steering while the job runs, retry when they want a new review).

**FR-CHAT-4.** The chat’s scope is this job. A generic question, or any question that is not about the job on screen, is declined and is not answered in part. That includes general programming help, definitions and how-tos that do not turn on this job’s details, questions about another repository or another job, and small talk. The decline is one or two sentences and points back at this job. The input hint invites questions about this job. It does not invite open-ended help.

**FR-CHAT-5.** Sending a chat question does not consume a steering round and does not interrupt the review that is in progress. The review’s own “needs input” panel still appears when the reviewer asks a question; the live chat does not answer that question on the user’s behalf.

**FR-CHAT-6.** The panel keeps the thread for as long as the job is in memory. Closing PRISM drops it with the job, the same way the rest of a job is dropped. The transcript distinguishes the user’s question, the live chat’s answer, and the reviewer’s own log, so the three are not one stream.

**FR-CHAT-7.** The command line can ask the same way, for a pull-request job or a document job, in any state of that job. The reply is printed. It obeys FR-CHAT-2 through FR-CHAT-5 and FR-CHAT-9. There is no 70/30 split on the command line.

**FR-CHAT-8.** The live chat uses that job’s reviewer session as its source of memory when that session still exists (the pull-request reviewer for a pull-request job, the document reviewer for a document job), and the job record when it does not. Either way the answer stays inside FR-CHAT-2. The user does not pick a model for the chat separately; it uses the job’s model.

**FR-CHAT-9. Tone.** Tone is the wording of the typed reply, the way a person reads it in the **CHAT** pane or on the command line. The chat has no microphone, no speech recognition, and no spoken playback.

The wording is that of a colleague: professional, friendly, and to the point. A reply is there to make the job clear to the person reading it.

- Short and interesting. A reply is a few sentences or a short list, and each sentence carries a point a colleague would actually write. It does not answer with a long paragraph, a chain of paragraphs, a dry status dump, or a write-up the person did not ask for.
- On the question. It answers what was asked and stops. It does not add background, a recap of the whole review, a list of other findings, or a suggested next step unless the question asked for that.
- Technical when the question is technical. A file, a line, a finding id, or a verdict is named plainly. The surrounding words stay ordinary.
- No agent narration. It does not describe its tools, its plan, or that it is about to look something up. It does not open with a restatement of the question.
- A decline (off-topic, or a request to approve or merge) is one or two sentences, in the same tone.

The command-line reply uses this same tone. The **OUTPUT** pane does not: that pane is still the reviewer’s own transcript.

### Acceptance

1. **Given** a pull-request job’s details screen with the chat collapsed, **when** the user opens it while the review is running, **then** the log section is the full width and is labelled **OUTPUT**.
2. **Given** that screen, **when** the user expands the chat, **then** the section splits 70/30, the wider pane stays labelled **OUTPUT** and keeps the reviewer’s log, and the narrower pane is labelled **CHAT** and accepts a question.
3. **Given** a document-review job’s details screen, collapsed and then expanded, **when** the titles are read, **then** the log is **OUTPUT** in both states and the open chat pane is **CHAT**.
4. **Given** an expanded chat, **when** the user collapses it, **then** the section returns to a single **OUTPUT** pane and the questions already asked are still there on the next expand.
5. **Given** a pull-request job that has finished with three findings, **when** the user asks what the highest-severity finding is, **then** the answer names that finding in a few sentences, does not list the other two, and does not offer to merge.
6. **Given** a document review that has finished with a Major comment, **when** the user asks what must change before work can start, **then** the answer names that comment and the Request changes decision, and stops there.
7. **Given** a document review that is still running and has no report yet, **when** the user asks whether it is approved, **then** the answer says the review has not reported yet and does not predict a decision.
8. **Given** a job that is queued, failed, waiting on the reviewer, or already finished, **when** the user asks what state it is in, **then** the chat answers from that job’s details in each of those states.
9. **Given** a finished review whose highest finding is a specific id and file in the job details, **when** the user asks which finding blocks the work, **then** the reply names that id and file, and no other.
10. **Given** a running job of either kind, **when** the user asks a question and also has not sent a steering note, **then** the review continues and the steering-round count stays at zero.
11. **Given** the user asks the chat to approve the change, **when** the answer arrives, **then** the stored verdict is unchanged and the answer declines in one or two sentences.
12. **Given** a pull-request job and a document job, **when** the user asks on one job’s details about a finding that exists only on the other, **then** the chat declines or says that finding is not in this job.
13. **Given** the chat on a job, **when** the user asks a generic question with no bearing on that job (“what is a fast-forward merge?”, “write a Python function”), **then** the chat declines in one or two sentences and does not explain the general topic.
14. **Given** a question that names one finding, **when** the answer arrives, **then** it is not a multi-paragraph review, a tool-by-tool account, or a restatement of the question before the point.

---

## 9. GitLab repositories

**FR-GL.** High priority. A pull-request job can review and merge a GitLab merge request with the same safety as a CodeCommit pull request. CodeCommit behavior is unchanged.

Document review of a GitLab clone already goes through git. v7.0 does not add a separate document-review path for GitLab. Uploaded documents, owner, the binary decision, and the chat card apply whichever host the clone uses. A document review does not require the AWS CLI when every selected remote is GitLab.

### 9.1 Choosing the host

**FR-GL-1.** PRISM decides the host from the clone’s remote address. A CodeCommit remote stays a CodeCommit job. A GitLab remote (gitlab.com or a self-managed GitLab host) is a GitLab job. Anything else is reported as an unsupported host and the job does not start.

**FR-GL-2.** The form shows the host it detected and the project it detected (the GitLab group and project, or the CodeCommit repository name). The user can correct the project name when the remote does not match the name on the server. The user cannot point a CodeCommit remote at GitLab or the reverse in the same job.

**FR-GL-3.** A multi-repository workspace can contain one GitLab clone and one CodeCommit clone. Each job reviews one target, on that target’s host. Mapping into Backend and Frontend is unchanged.

**FR-GL-4.** The command line accepts a GitLab project and a merge-request number in place of a CodeCommit repository and pull-request id. The help text names both hosts.

### 9.2 Credentials

**FR-GL-5.** GitLab access uses a personal or project access token with permission to read the merge request, update its description, and merge it. The token is read from the environment or from the local PRISM settings. It is never written into a job record, a log export, a chat card, or the command line of a child process that the log records.

**FR-GL-6.** `prism doctor` checks GitLab when a GitLab remote is present or a GitLab token is configured: the token works, the project is visible, and the merge request the user named (when they pass one) is readable. It still checks the AWS CLI and CodeCommit when a CodeCommit remote is present. A machine that only uses GitLab is not required to have the AWS CLI for a GitLab job.

**FR-GL-7.** An authentication or permission failure stops the job and shows the host’s error. PRISM does not refresh tokens and does not fall back to the other host.

### 9.3 The pipeline

**FR-GL-8.** A GitLab job runs the same stages as a CodeCommit job: review, update the description, merge check, sync the source with the target when a fast-forward is not possible, merge. Stages the user turned off are skipped, as they are today. The progress bar names the same stages.

**FR-GL-9.** The reviewer receives the merge request’s source branch, target branch, title, and diff. It uses the same pull-request reviewer and the same verdicts. FR-SCALE applies. The reviewer does not merge, push, or call the GitLab API to write; PRISM does those steps.

**FR-GL-10.** The findings block is written into the merge-request description with the same start and end markers used for CodeCommit. The author’s own text is preserved. A stale PRISM block is replaced. Dry run does not write it.

**FR-GL-11.** Merge is fast-forward only. PRISM merges when the project allows a fast-forward merge and the merge request is open and fast-forward mergeable after any sync. PRISM does not squash, does not create a merge commit, and does not force-push. If the project’s merge method cannot fast-forward, the job stops and says so, and the merge request stays open.

**FR-GL-12.** When the source branch has diverged, sync fetches the target and merges it into the source with an ordinary merge commit, then pushes the source without force. Conflicts abort the merge, restore the worktree, and go through the existing conflict-analyst flow. Nothing is pushed until every conflict has an answer.

**FR-GL-13.** Every CodeCommit merge gate has a GitLab equivalent, enforced by PRISM and not by the reviewer:

- the verdict is one that 6.5.1 already allows to merge, and FR-SCALE did not withdraw it;
- the merge request is open;
- the source commit is the commit that was reviewed;
- auto-merge is on and this is not a dry run;
- a large change is never merged (FR-SCALE-5);
- a normal change with impact 7 or higher waits for confirmation.

**FR-GL-14.** Two jobs on the same GitLab project and the same merge-request number are refused, for the same reason two jobs on the same CodeCommit pull request are refused: both would write the description.

### Acceptance

1. **Given** a clone whose remote is a GitLab project and an open merge request, **when** the user starts a review with auto-merge on and the verdict is Approve at impact 3, **then** the description contains the findings block and the merge request is merged by fast-forward.
2. **Given** the same merge request classified as large, **when** the review finishes, **then** the merge request stays open and the verdict stored is Request changes.
3. **Given** a GitLab project whose merge method cannot fast-forward, **when** merge is attempted, **then** the job stops with that reason and the source branch is not force-pushed.
4. **Given** a diverged merge request that merges cleanly, **when** sync is on, **then** the target is merged into the source, the source is pushed, and the merge request is then fast-forwarded.
5. **Given** a conflict during sync, **when** the user aborts, **then** no push occurs and the clone’s previous branch is unchanged.
6. **Given** a token without permission to merge, **when** the job reaches merge, **then** the job stops with the permission error and the description update from the earlier stage is left as the host saved it.
7. **Given** a CodeCommit pull request reviewed on the same build, **when** it is approved and merged, **then** the CodeCommit path behaves as in 6.5.1.

---

## 10. Alerts for the develop branch

**FR-ALERT.** `prism-aws-alerts` reports merges and deployments to the `develop` branch in their own Google Chat group. The groups that watch `qa`, `staging`, and `main` stay as they are.

The mechanism already routes each chat target by branch and threads a deployment reply onto the merge message. v7.0 uses that mechanism for `develop` instead of adding a second alerting system. This section is CodeCommit and CodePipeline only. GitLab merge alerts are out of scope.

**FR-ALERT-1.** The alerts configuration includes a chat target that watches only `develop`. Its webhook is the develop group’s webhook. It is a different target from the targets that watch `qa`, `staging`, or `main`.

**FR-ALERT-2.** A pull request merged into `develop` posts the existing “Pull Request Merged” card to the develop target only, among the configured targets. A pull request merged into `qa`, `staging`, or `main` does not post to the develop target.

**FR-ALERT-3.** A pipeline that deploys `develop` is listed with the other pipeline-to-branch mappings, for each repository that deploys develop. When that deployment succeeds or fails, the reply is threaded onto the develop merge message in the develop group, with the same card style already used for other environments: environment name, succeeded or failed, pull request, commit, and author.

**FR-ALERT-4.** When a develop deployment cannot be matched to a merge message, it follows the existing unlinked-deployment setting. With that setting left on, the standalone card goes to the develop target because that target watches `develop`, and it does not go to the other groups.

**FR-ALERT-5.** The example configuration and the alerts README show the develop target and the develop pipeline entries as placeholders, in the same way the qa, staging, and main entries are placeholders. Real webhook URLs stay out of git.

**FR-ALERT-6.** Adding the develop target does not change thread tracking, the thirty-day retention, or the rule that a chat target name is unique.

### Acceptance

1. **Given** the develop target and the existing release targets, **when** a pull request merges to `develop`, **then** only the develop group receives the merge card.
2. **Given** that merge’s commit is deployed by the develop pipeline, **when** the pipeline succeeds, **then** the develop group receives a threaded deployment reply and the release groups receive nothing for that deployment.
3. **Given** a merge to `main`, **when** the alerts run, **then** the develop group receives nothing.
4. **Given** a develop deployment whose commit has no stored merge thread, and unlinked deployments are on, **when** the pipeline finishes, **then** the develop group receives a standalone deployment card.

---

## 11. Cross-cutting rules

These bind every requirement above.

**FR-X-1. Tenant of a job.** A job’s host, project, merge request or pull request, documents, owner, and webhook are fixed when the job is created. Editing the form afterward does not change a job that already exists.

**FR-X-2. Secrets.** GitLab tokens and both webhook URLs are secrets. They are masked in `prism config`, omitted from `prism jobs` output, and stripped from a saved diagnostic zip. The owner’s name is not a secret.

**FR-X-3. Agents do not write.** Reviewers still only report. PRISM writes descriptions, review sections, commits, pushes, and merges. FR-SCALE and FR-VER are applied by PRISM after the report is parsed.

**FR-X-4. Partial reviews.** A review that PRISM had to cut short is still not an approval and is still not merged. A cut-short document review is Request changes.

**FR-X-5. Dry run.** Dry run performs the read and the decision, posts nothing, merges nothing, and writes no file.

**FR-X-6. One findings shape.** `prism jobs` output keeps the existing findings fields. New values fit the existing fields: document `verdict` is `approve` or `request-changes`; a finding may carry `scale` of `normal` or `large`. Old clients that ignore unknown fields keep working.

**FR-X-7. Desktop and command line.** Every behavior in this document is available in the desktop app and on the command line, except the live chat’s on-screen thread inside the job details (the command line prints the reply instead, for both a pull-request job and a document job) and drag-and-drop (the command line takes file paths).

---

## 12. Out of scope for v7.0

- GitLab pipeline or merge alerts. Alerts stay on CodeCommit and CodePipeline.
- Changing pull-request verdicts to a binary decision. Section 6 is document reviews only.
- Replacing the steering note or the post-job follow-up. Section 8 adds the live chat beside them, on the job details of both review kinds.
- Spoken chat. The chat is typed text. “Tone” in FR-CHAT-9 is how those replies are worded.
- Hosting other than GitLab and CodeCommit (GitHub, Bitbucket, local patches).
- Persisting jobs or chat transcripts across a restart.
- Rewriting review sections that 6.5.1 already wrote into documents, other than appending a new version under FR-VER-6.
- Editing DynamoDB table shapes for alerts. Develop uses the existing thread table.

---

## 13. Requirement index

| Id | Section | One-line statement |
|---|---|---|
| FR-DOC-IN | 3 | Document reviews accept dropped and uploaded files, including files not on a branch. |
| FR-OWN | 4 | Chat and the job name the owner of the document review, separately from the reviewer. |
| FR-CARD | 5 | The document chat card is a decision, a short summary, and a short document list. |
| FR-VER | 6 | A document review decides Approve or Request changes, and majors or blockers cannot be an approval. |
| FR-SCALE | 7 | A large or expensive change is impact at least 8, is not approved, and cannot be confirmed through to a merge. |
| FR-CHAT | 8 | Chat works in every review state, answers only from the open job, and declines generic questions. The log is OUTPUT; the open pane is CHAT. |
| FR-GL | 9 | GitLab merge requests are reviewed and fast-forward merged under the same gates as CodeCommit. |
| FR-ALERT | 10 | Develop merges and deployments notify a separate chat group. |

---

## 14. Suggested build order

GitLab is the priority the request called out, and the document-decision rule changes a gate other work displays. A practical order:

1. **FR-VER** and **FR-SCALE** — both change what “approved” means, and GitLab merge must enforce them from the first GitLab merge.
2. **FR-GL** — host detection, credentials, description update, fast-forward merge, doctor.
3. **FR-OWN** and **FR-CARD** — the chat card is rebuilt once, with the owner and the two document decisions.
4. **FR-DOC-IN** — upload and drop, then the write rules for files that are not on a branch.
5. **FR-CHAT** — the OUTPUT title, the 70/30 CHAT pane on both job details screens, the written colleague tone, and the running-job command, after findings have a stable shape.
6. **FR-ALERT** — configuration and the develop pipeline mapping; no change to the Lambda contract if the existing branch routing is used as specified.

Each step is acceptable on its own scenarios before the next step starts. FR-GL is not done until a large GitLab change is refused under FR-SCALE and a CodeCommit merge still passes its existing tests.
