#!/usr/bin/env python3
"""Generate a PBKDF2-SHA256 hash for a password to use in config/users.yaml.

Uses Python's built-in hashlib — no external dependencies required.
Works on any CPython (LibreSSL or OpenSSL).

Usage:
    python tools/hash_password.py
"""
import getpass
import sys


def main() -> None:
    print("Generate a PBKDF2-SHA256 hash for a password (hashlib stdlib, no external deps).")
    password = getpass.getpass("Enter password: ")
    if not password:
        print("Empty password — aborted.", file=sys.stderr)
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match — aborted.", file=sys.stderr)
        sys.exit(1)

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from app.auth import _hash_pbkdf2

    hashed = _hash_pbkdf2(password)
    print("\nPBKDF2-SHA256 hash:")
    print(hashed)
    print("\nYAML snippet (add to config/users.yaml):")
    print(f'    password_pbkdf2: "{hashed}"')


if __name__ == "__main__":
    main()
