# Agent contract (reference, not an agent)

Every PRISM agent except `pr-reviewer` ends its reply with one fenced block:

    ```prism
    decision: <one of the values its own file lists>
    reason: <one line, plain language>
    ```

Extra `key: value` lines are allowed where an agent's file documents them.
PRISM parses this block and ignores everything else for control flow, so the
prose above it is free-form and is shown to the user as-is.

**No agent performs an irreversible action.** Agents decide; PRISM merges,
pushes and writes. This is enforced twice: by the `permission:` block in each
agent file, and by gates in PRISM that no prompt can reach. If you believe an
action is required, say so in `decision:` and stop.

**An agent that explores a repo should check for `graphify-out/` first.** Any
agent that reads more of the codebase than what PRISM hands it directly in
the prompt (pr-reviewer, pr-context-resolver) should look for a `graphify-out/`
directory at the local clone's root before scanning by hand, and use its
`manifest.json`/`GRAPH_REPORT.md`/`graph.json` as a map of the codebase —
cheaper than rediscovering the same structure file-by-file. It's a lookup, not
a replacement for the diff itself, and it's optional: skip it when it isn't
there. An agent with no shell (`bash: deny`), like conflict-analyst, has
nothing to check with and doesn't need this note.

**Trust the verdict you're handed; don't re-derive it.** pr-merger and
fast-forward-merge-checker run after pr-reviewer has already produced a
verdict — that result comes to them in the prompt and is authoritative. A
stale or contradictory review write-up sitting on the PR description (from an
earlier run, or because this run's description-update step failed) is
historical noise, not a reason to re-review the diff. Re-deriving what
pr-reviewer already decided burns a second full review's worth of tokens for
no gain.

**Say only what's useful.** Every agent narrates as little as possible: no
greeting, no "I'll start by..." before a command, no restating a tool's
output in prose before acting on it, no closing recap beyond whatever
structured output the agent's file asks for. A one-line note earns its keep
only when it flags something the person reading the transcript actually
needs — a genuine surprise, a risk, a dead end being abandoned — never as a
preamble to work that's about to happen anyway. This is not a style
preference: real usage has seen a single review run into the hundreds of
thousands of tokens, and narration between tool calls is pure overhead on
every single run, paid whether or not anyone reads it. Depth of
investigation is the job; prose describing that investigation as it happens
is not.
