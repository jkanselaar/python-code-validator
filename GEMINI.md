# Python you write here gets checked

Validate every Python file you generate or edit with the `validate_python` tool
before presenting it, and do not present code the validator marks invalid — fix
it and validate again. It parses, lints (ruff), type-checks (mypy), applies an
AST security policy and scans for credentials, without running anything.

Two more tools are available when a diagnosis is not enough:

- `repair_python` returns the corrected source in `fixed_code`. A null there
  means nothing could be proven safe to change, not that the call failed.
- `execute_python` runs the repaired source in a throwaway container — no
  network, read-only filesystem — and reports its exit code, stdout and stderr.
  It is a side effect: only use it when the code is meant to run.

Both need a configured key; without one, `validate_python` still works on the
free allowance the bridge mints for itself.

The code you pass leaves the machine: it is sent to `https://api.statemind.ai`
and retained there to improve the service.
