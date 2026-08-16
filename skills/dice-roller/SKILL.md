---
name: dice-roller
description: Roll dice in standard notation (2d6, d20, 3d8+2). Use when asked to roll dice, pick a random number in a range, or settle something by chance.
license: Apache-2.0
metadata:
  author: kokua
---

# Dice roller

This is Kokua's worked example of a skill: the smallest useful thing, showing the shape without any
host integration. Copy it as a starting point.

## Steps

Run the script with a standard dice expression:

```bash
python3 scripts/roll.py 2d6
python3 scripts/roll.py 3d8+2
```

It prints JSON, so the individual rolls are available and not just the total:

```json
{"expression": "2d6", "rolls": [3, 5], "modifier": 0, "total": 8}
```

## Notes

- No inline dependency block, so it runs on the host's own interpreter. A skill only needs `uv run`
  when it declares dependencies.
- Up to 100 dice with up to 1000 sides, which is a closed range rather than an unbounded loop.
- Run `python3 scripts/roll.py --help` for the full interface.
