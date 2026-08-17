"""A function that passes every check except the one that matters.

``ruff`` and ``mypy`` are silent on it, it runs without raising, and it returns
the wrong number: the last bit is never counted. The doctest lines below are the
intent, and ``execute`` is the mode that reads them.

    python3 ../check.py bitcount.py --mode execute
"""


def bitcount(n: int) -> int:
    """Number of bits set in ``n``.

    >>> bitcount(127)
    7
    >>> bitcount(128)
    1
    >>> bitcount(0)
    0
    """
    count = 0
    while n > 1:
        count += n & 1
        n >>= 1
    return count
