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
