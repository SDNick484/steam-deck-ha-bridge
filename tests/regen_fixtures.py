#!/usr/bin/env python3
"""Rewrite tests/fixtures/ from what the code produces now.

Run this on purpose, after changing the published state or the discovery payloads, and read the diff:
those files are an interface other programs depend on (see docs/state-schema.md). Bumping VERSION in
deck_ha_bridge.py also changes the discovery golden, because the version is part of the device block.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.scenarios import generate  # noqa: E402
from tests.support import FIXTURES, ROOT  # noqa: E402


def main() -> None:
    FIXTURES.mkdir(exist_ok=True)
    for name, data in generate().items():
        path = FIXTURES / name
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote", path.relative_to(ROOT))


if __name__ == "__main__":
    main()
