#!/usr/bin/env python
"""Print a fresh Django ``SECRET_KEY``.

Separate from the bootstrap scripts because both of them need it and neither of
them should be doing cryptography inline: shell quoting rules differ enough
between bash and PowerShell that `python -c "import secrets; ..."` is a reliable
source of a broken key on one platform and not the other.

Usage::

    python scripts/gen_secret.py          # 50 url-safe characters (~300 bits)
    python scripts/gen_secret.py 64       # longer
"""
from __future__ import annotations

import secrets
import sys

DEFAULT_LENGTH = 50


def main() -> int:
    if len(sys.argv) > 1:
        try:
            length = int(sys.argv[1])
        except ValueError:
            print(f"error: {sys.argv[1]!r} is not an integer", file=sys.stderr)
            return 2
        if length < 32:
            # Django's own docs suggest at least 50; anything shorter than 32 is
            # below the entropy of the key it is protecting.
            print("error: use at least 32 characters", file=sys.stderr)
            return 2
    else:
        length = DEFAULT_LENGTH

    # `token_urlsafe` uses `secrets`, i.e. the OS CSPRNG. `random` would be
    # predictable from a handful of observed outputs.
    print(secrets.token_urlsafe(length))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
