"""A file the static checks refuse, with nothing dangerous in it.

The string it decodes is ``2 + 2``, and the key is not a key — the point is that
the AST policy follows the call through ``importlib`` to ``eval`` anyway, and the
credential scan reads the assignment. One static check names both.

    python3 ../check.py loader.py
"""

import base64
import importlib

API_KEY = "put-the-key-here-and-the-scan-will-find-it"


def run(encoded: str = "MiArIDI=") -> int:
    """Result of the expression hidden in ``encoded``."""
    builtins = importlib.import_module("builtins")
    return builtins.eval(base64.b64decode(encoded).decode())
