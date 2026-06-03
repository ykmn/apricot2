#!/usr/bin/env python3
"""Increment the patch version in app/main.py.
Called automatically by .git/hooks/pre-commit."""
import re
from pathlib import Path

MAIN_PY = Path(__file__).parent.parent / "app" / "main.py"


def bump() -> None:
    content = MAIN_PY.read_text(encoding="utf-8")
    m = re.search(r'VERSION = "(\d+)\.(\d+)\.(\d+)"', content)
    if not m:
        print("[bump_version] VERSION not found in app/main.py — skipping")
        return
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    new_patch = str(int(patch) + 1).zfill(3)
    new_ver = f"{major}.{minor}.{new_patch}"
    new_content = content.replace(
        f'VERSION = "{major}.{minor}.{patch}"',
        f'VERSION = "{new_ver}"',
    )
    MAIN_PY.write_text(new_content, encoding="utf-8")
    print(f"[bump_version] {major}.{minor}.{patch} → {new_ver}")


if __name__ == "__main__":
    bump()
