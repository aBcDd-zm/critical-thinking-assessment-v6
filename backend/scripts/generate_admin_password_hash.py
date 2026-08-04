"""Generate an Argon2id hash for the one V6 review-console administrator.

Run this only in a trusted local/server terminal, then copy the printed value
to ``ADMIN_PASSWORD_HASH`` in the server environment.  The plaintext password
is never written to disk by this helper.
"""

from __future__ import annotations

from getpass import getpass

from argon2 import PasswordHasher


def main() -> None:
    password = getpass("New administrator password: ")
    confirmation = getpass("Confirm administrator password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match; no hash was generated.")
    if len(password) < 12:
        raise SystemExit("Use a password with at least 12 characters.")

    password_hash = PasswordHasher().hash(password)
    print("\nSet this in the server environment (do not commit it):")
    print(f"ADMIN_PASSWORD_HASH={password_hash}")


if __name__ == "__main__":
    main()
