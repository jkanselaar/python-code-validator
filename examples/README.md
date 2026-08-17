# Examples

Three files you can run against the live service, each showing one thing the
service catches that a linter cannot.

`check.py` is the client used below: standard library only, and unlike
`validate.py` in the repository root it also asks for `execute`, which is the
mode that runs the code against its examples.

```bash
cd examples
python3 check.py intent-first/bitcount.py --mode execute
python3 check.py secure-python/loader.py
```

Without `VALIDATOR_API_KEY` set, `check.py` mints a free key on first use and
keeps it in `.validator-key` next to it. A free key covers `static` only, so the
`execute` line above needs a key with credits; the static run needs nothing.

## `intent-first/bitcount.py`

Parses, lints, type-checks and runs — and returns the wrong number. The doctest
lines in the docstring are the intent, so `execute` runs them in a container and
answers `python:example-mismatch` with the number it actually got, and
`fixed_code` with a version that passes them.

## `secure-python/loader.py`

No syntax error and nothing to type-check, but it decodes a string and hands it
to `eval`, and carries a key in the source. The AST policy follows the call
through the dynamic import and the credential scan finds the key, so `static`
alone refuses it. Nothing in the file executes anything harmful — it is here to
be rejected.

## `sandbox-execution/collatz.py`

Correct, and its examples hold: this is what an accepted answer looks like
(`valid: true`, `runtime.ran: true`, `runtime.sandbox: docker`). It is the file
to run first if you want to see the sandbox report a clean run before you look
at a rejection.
