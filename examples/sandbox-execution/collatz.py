"""What an accepted answer looks like.

Correct, annotated, and its examples hold, so ``execute`` reports a clean run
from the container: ``valid: true``, ``runtime.ran: true``,
``runtime.sandbox: docker``.

    python3 ../check.py collatz.py --mode execute
"""


def collatz_steps(n: int) -> int:
    """Steps the Collatz sequence takes from ``n`` down to 1.

    >>> collatz_steps(1)
    0
    >>> collatz_steps(6)
    8
    >>> collatz_steps(27)
    111
    """
    if n < 1:
        raise ValueError("n must be positive")
    steps = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps
