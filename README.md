# Python Code Validator

A hosted service that proves AI-generated Python does what you asked. State the
intent — assertions or doctest lines — and the code is run against it inside a
container with no network and a read-only filesystem; a fix comes back only when
every example passes. On the QuixBugs defects that is 41% repaired and 77%
refused as not doing what they say, with no false alarms on the corrected
programs.

The checks that need no intent come with it: syntax and lint diagnostics, an AST
security policy that also catches calls hidden behind dynamic imports and runtime
attribute lookups, a bandit pass, a credential scan and deterministic repair —
one verdict with a score. Asking the same question twice inside ten minutes is
answered from the first answer and costs nothing (`x-msvc-repeat: 1`).

This repository holds the client side: the MCP configuration, the CI script and
the pre-commit hook. The service itself runs at `https://api.statemind.ai`, so
there is nothing to install or host.

## A key, without an account

```bash
curl -s -X POST https://api.statemind.ai/v1/keys
# {"api_key": "msvc_free_…", "tier": "free", "calls_per_day": 100, "modes": ["static"]}
```

100 validations a day, metered per UTC day. Every answer carries the state of
the allowance (`x-quota-remaining`, `x-quota-reset`), so a client can back off
before it is cut off.

## MCP

Registered in the official MCP registry as
`ai.statemind/python-code-validator`, a name verified against the domain that
serves it rather than a GitHub account. Any MCP client adds it with one
block:

```json
{
  "mcpServers": {
    "python-code-validator": {
      "type": "http",
      "url": "https://api.statemind.ai/mcp",
      "headers": { "Authorization": "Bearer msvc_free_…" }
    }
  }
}
```

- Claude Code: `claude mcp add --transport http python-code-validator https://api.statemind.ai/mcp --header "Authorization: Bearer msvc_free_…"`
- Cursor: `~/.cursor/mcp.json`, same block.
- VS Code / Copilot: `.vscode/mcp.json` under `"servers"`.

A client that only launches a command uses the stdio bridge in this repository
instead, which forwards the same tool over HTTPS:

```json
{
  "mcpServers": {
    "python-code-validator": {
      "command": "python3",
      "args": ["/path/to/python-code-validator/mcp_stdio.py"]
    }
  }
}
```

Or as a container, which the `Dockerfile` here builds:

```bash
docker build -t python-code-validator .
docker run -i --rm -e VALIDATOR_API_KEY python-code-validator
```

Gemini CLI installs the same bridge as an extension, with the instruction file
that makes it get used:

```bash
gemini extensions install jkanselaar/python-code-validator
```

Three tools, named after what they do to the code:

| tool | runs the code | key |
| --- | --- | --- |
| `validate_python` | no | free |
| `repair_python` — also returns `fixed_code` | no | paid |
| `execute_python` — also runs it in a sandbox | **yes** | paid |

The old single `python_code_validator` tool, with its `mode` argument, still
answers for clients that already configured it, but is no longer listed.

## Saying what the code was supposed to do

Every check above passes on a function that computes the wrong answer. The one
thing that catches it is the intent, and the agent that asked for the code is
the only one who has it — so pass it along:

```json
{"code": "def bitcount(n): …", "mode": "execute",
 "options": {"examples": "assert bitcount(127) == 7"}}
```

Doctest lines (`>>> bitcount(127)` then `7`) work the same way, as do `>>>`
examples already written in the source. `execute_python` runs them in the
sandbox: one that does not hold is a `python:example-mismatch` error, and the
repair search returns a fix only when every example passes. On the QuixBugs
defect set — real bugs, hidden test inputs deciding correctness — that repairs
41% and refuses 77% as not doing what they say, with no false alarms on the
corrected programs.

Repeating a call costs nothing: the same key asking the same question — same
mode, same code, same examples — is answered from the answer it already got,
marked `x-msvc-repeat: 1`, so an agent that checks its work at every step is not
billed for verdicts that cannot have changed.

## Making the agent use it

Configuring the server is not what gets it called: the instruction file is.
[`AGENTS.md`](AGENTS.md) in this repository is that text, written to be dropped
into any project under whichever name the client reads:

```bash
mkdir -p .github
curl -sf https://raw.githubusercontent.com/jkanselaar/python-code-validator/main/AGENTS.md \
  | tee AGENTS.md CLAUDE.md GEMINI.md .github/copilot-instructions.md >/dev/null
```

Cursor reads rules with front matter instead, so that one is a separate file —
copy [`.cursor/rules/python-code-validator.mdc`](.cursor/rules/python-code-validator.mdc)
into `.cursor/rules/` of the project.

The short version, if you would rather add a line to instructions you already
have:

> Write what the code should do as `assert` examples before writing the code,
> and pass them in `options.examples`. Call `validate_python` after every edit
> and `execute_python` once a function is finished, not again until what it
> does has changed. When a call returns `fixed_code`, take it — the service ran
> it against your examples. Do not present code that came back `valid: false`.

## CI

The service hands out the client, so a workflow needs no checkout of this
repository and no secret:

```yaml
- run: |
    curl -sf https://api.statemind.ai/v1/client -o validate.py
    python3 validate.py --changed-against "origin/${{ github.base_ref }}"
```

Or as an action, from the Marketplace:

```yaml
- uses: jkanselaar/python-code-validator@v1.19.2
  with:
    api-key: ${{ secrets.VALIDATOR_API_KEY }}   # optional; free tier without it
```

The changed Python is validated and offending lines are annotated on the diff,
failing the job on syntax errors and unsafe patterns. Files the service refuses
outright (over its 200 kB limit) are skipped with a warning rather than failing
the run.

## Pre-commit

```yaml
repos:
  - repo: https://github.com/jkanselaar/python-code-validator
    rev: v1.19.2
    hooks:
      - id: python-code-validator
```

## The client itself

`validate.py` is standard library only, so it also works as `python
validate.py file.py` in a Makefile, a git hook or a container:

```
$ python3 validate.py service.py
::error file=service.py,line=88,title=SyntaxError::invalid syntax
FAIL service.py score=0.66

0/1 files accepted
```

`VALIDATOR_API_KEY` is used when set; otherwise the client mints a free key.
`VALIDATOR_URL` points it at another deployment. `VALIDATOR_SOURCE` names the
caller, which is only ever counted: a run inside a workflow says
`github-action` by itself.

## The badge

A repository whose Python is checked on every pull request can say so:

```markdown
[![Python validated](https://img.shields.io/badge/python-validated-2ea44f?logo=python&logoColor=white)](https://api.statemind.ai/?src=badge)
```

[![Python validated](https://img.shields.io/badge/python-validated-2ea44f?logo=python&logoColor=white)](https://api.statemind.ai/?src=badge)

## HTTP

```bash
curl -s https://api.statemind.ai/v1/validate \
  -H "Authorization: Bearer $VALIDATOR_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"code": "def f(:\n    pass\n", "mode": "static"}'
```

`mode` is `static`, `repair` or `execute`; `repair` and `execute` need a
configured key. Submitted code is not logged.

A refused call says what to do about it, so a caller with no operator to ask can
resolve it itself:

```json
{"error": "payment_required",
 "remedy": {"action": "upgrade_key", "hint": "A free key covers static only. …"}}
```

## Licence

MIT.
