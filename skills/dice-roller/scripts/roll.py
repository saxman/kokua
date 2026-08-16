"""Roll dice in standard notation and print the result as JSON.

Kokua's worked example of a skill script: no dependencies, so it runs on the host interpreter with no
`uv run`; structured output, so a caller can read the individual rolls rather than parsing prose; and a
closed input range, so a malformed expression is refused rather than guessed at.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys

_EXPRESSION = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)

_MAX_DICE = 100
_MAX_SIDES = 1000


def roll(expression: str) -> dict:
    """Roll a dice expression like ``2d6``, ``d20`` or ``3d8+2``.

    Raises :class:`ValueError` with the accepted form when the expression or its bounds are wrong, so
    a caller gets something actionable rather than a guess.
    """
    match = _EXPRESSION.match(expression)
    if match is None:
        raise ValueError(f"{expression!r} is not dice notation. Expected forms like '2d6', 'd20' or '3d8+2'.")

    count = int(match.group(1) or 1)
    sides = int(match.group(2))
    modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0

    if not 1 <= count <= _MAX_DICE:
        raise ValueError(f"dice count must be between 1 and {_MAX_DICE}; got {count}.")
    if not 2 <= sides <= _MAX_SIDES:
        raise ValueError(f"sides must be between 2 and {_MAX_SIDES}; got {sides}.")

    rolls = [random.randint(1, sides) for _ in range(count)]
    return {
        "expression": expression.strip(),
        "rolls": rolls,
        "modifier": modifier,
        "total": sum(rolls) + modifier,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Roll dice in standard notation and print JSON.",
        epilog="Examples:\n  python3 scripts/roll.py 2d6\n  python3 scripts/roll.py 3d8+2\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("expression", help="Dice notation, e.g. 2d6, d20, 3d8+2.")
    args = parser.parse_args()

    try:
        print(json.dumps(roll(args.expression)))
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
