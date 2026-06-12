#!/usr/bin/env python3
"""Generate an Argon2 hash for a password to use in config/users.yaml.

Usage:
    python tools/hash_password.py

The script prompts for a password (input hidden), prints the hash, and shows
the YAML snippet to paste into users.yaml.
"""
import getpass
import sys


def main() -> None:
    try:
        from argon2 import PasswordHasher
    except ImportError:
        print("argon2-cffi is not installed. Run: pip install argon2-cffi", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password: ")
    if not password:
        print("Empty password — aborted.", file=sys.stderr)
        sys.exit(1)

    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match — aborted.", file=sys.stderr)
        sys.exit(1)

    ph = PasswordHasher()
    hashed = ph.hash(password)

    print("\nArgon2 hash:")
    print(hashed)
    print("\nYAML snippet (add to config/users.yaml instead of 'password:'):")
    print(f"    password_argon2: \"{hashed}\"")


if __name__ == "__main__":
    main()
