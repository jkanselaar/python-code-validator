# What the numbers are

Every figure below comes from a benchmark in the service's repository, scored by
running the programs rather than by reading them. The interesting comparison is
not against another repair tool — it is against what a Python project already
runs: `ruff` and `mypy`.

## Real bugs, with the examples the author wrote

[QuixBugs](https://github.com/jkoppel/QuixBugs) is 40 classic algorithm
implementations with one real defect each, collected by other people for exactly
this purpose. 22 of them document what the function is for with `>>>` examples.
The hidden test inputs — not the documented examples — decide whether a repair is
right, so a change that only satisfies the two documented cases counts as wrong.

| | on those 22 defects |
| --- | --- |
| `ruff` flags the defect | **0** |
| `mypy` flags the defect | **0** |
| this service refuses the program | **17 (77%)** |
| this service returns a repair that passes the hidden tests | **9 (41%)** |
| false alarms on the 22 *correct* versions | **0** |

`ruff` does report three of the 22 files — an unsorted import block, an
unnecessary `range` start, a `yield` loop that could be `yield from`. None of
those is the bug. That is the point: the defect is a program that parses, lints,
type-checks and runs, and produces the wrong answer.

The gap between 77% refused and 41% repaired is deliberate. A wrong answer that
is refused is a wrong answer an agent does not ship, even where nothing here
knows how to fix it.

## Programs that crash when they run

20 programs that raise on their own inputs, none of them a syntax error:

| | |
| --- | --- |
| caught without running the code | **16 (80%)** |
| of those, caught by a check `ruff`/`mypy` do not make | **14** |
| false alarms on the 20 working versions | **0** |

## Reproducing it

The corpus and the runners live in the service repository, and the numbers are a
CI gate there rather than a claim in a README — each benchmark fails the build
when it drops below its floor:

```
make bench-intent      # 41% repaired, 77% refused, on QuixBugs with examples
make bench-supplied    # the same, examples passed in the call instead
make bench-runtime     # 80% of crashing programs caught statically
make bench-quixbugs    # the same defects with the examples stripped out
```

`make bench-quixbugs` scores **0%**, and it is in this list on purpose: strip the
`>>>` examples and there is nothing left that says what `bitcount` was supposed
to return. No amount of static analysis recovers intent that was never written
down. That is why the tools here ask for examples, and why an agent that states
what it was asked to build gets an answer no linter can give it.

## What this does not measure

The service also runs a security policy over the AST, a `bandit` pass and a
credential scan, and it executes code in a container with no network and a
read-only filesystem. Those are gates, not scores: there is no honest percentage
to quote for them, so none is quoted.
