# Triple-stamp Cursor workhorse runtime rules

These rules apply only when this Cursor session is the Triple-stamp Stage 1
`cursor_workhorse` runtime inside an existing Omnigent run. They do not set the
model for human-directed repository work. Human-directed repository analysis,
architecture, build, and fix sessions follow the user's requested model. This
repository's maintainer requires `gpt-5.6-sol-xhigh`,
displayed as GPT-5.6 Sol Extra High.

- Work directly with first-party file, shell, `WebSearch`, and `WebFetch` tools.
  Public-web calls run unattended through Cursor's yolo mode; a Claude
  supervisor permission denial is not a Cursor permission denial.
- Never invoke a Skill, workflow, Subagent, Task, nested agent, or another
  research agent. In particular, do not invoke the product-question research workflow.
- Emit at most one tool call per assistant turn and wait for its result before
  issuing another. Do not batch or parallelize tool calls in this repository.
- Return the complete evidence packet inline in the same Cursor session. Do not
  return a workflow announcement, delegation stub, or "still researching" text.
- Never invoke `triple-stamp`, `run-with-isaac`, `--self-test`, Omnigent, or
  another recursive triple-stamp run. The outer pipeline already owns the
  workspace lock.
- For this Stage 1 `cursor_workhorse` runtime only, the required model is
  `cursor-grok-4.6-xhigh`, displayed as Cursor Grok 4.6 Extra High. If that
  runtime session's displayed model differs, stop and report the mismatch.
