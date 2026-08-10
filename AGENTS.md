# Python you write here gets checked

Use this service the way a compiler is used: not once at the end, but at every
step, and never present code that has not come back accepted.

## Say what the code is for, before you write it

Turn the request into examples first — `assert bitcount(127) == 7`, or doctest
lines — and pass them in `options.examples` on every call from then on. This is
the only check that can fail code which parses, lints, type-checks and runs, and
generated code almost never carries examples of its own: the agent that was
asked knows what was asked, and nothing else does.

## While you write

- After every draft or edit of a file, call `validate_python`. It parses, lints
  (ruff), type-checks (mypy), applies an AST security policy and scans for
  credentials, without running anything.
- When a function is finished, call `execute_python` with your examples. It runs
  the code in a throwaway container with no network and a read-only filesystem,
  and checks the examples against what it actually does. Call it again when what
  the code *does* has changed — not after renaming a variable, reformatting, or
  editing a comment, and never twice on the same source: the answer cannot
  differ, and running it costs the caller ten times what a check costs.
- `repair_python` is the same repair without running anything: use it when the
  code is not meant to run, or when the checks alone already name the problem.

## When something comes back wrong

- Read `fixed_code` first. When it is filled in, the service already found and
  *ran* a program that satisfies your examples — take it. That is a proven fix,
  not a suggestion, and rewriting the algorithm yourself instead throws the
  proof away.
- A null `fixed_code` means nothing could be proven, not that the call failed.
  Then rewrite it yourself, using the diagnostics as evidence: a
  `python:example-mismatch` says which example failed and what the code gave
  instead, which is the fact your next attempt has to account for.
- Validate again after the rewrite. An unvalidated fix is a new draft.

## Before you present anything

Do not show the user code that came back `valid: false`. The `score` is a
quality signal, not a gate — a missing type annotation lowers it and breaks
nothing, so do not chase 1.0 and do not withhold working code over it. What must
be true is `valid: true`, with every example you were given passing.

## Without the MCP server configured

The same checks run over HTTPS with nothing installed and no account:

```bash
curl -sf https://api.statemind.ai/v1/client -o validate.py
VALIDATOR_SOURCE=agents-md python3 validate.py <files>
```

The code you pass leaves the machine: it is sent to `https://api.statemind.ai`
and retained there to improve the service.
