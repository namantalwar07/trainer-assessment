"""Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when the database has no active admin yet.

Used by hosted deployments that have no interactive shell. Once an admin exists this does nothing, so the
password variable can (and should) be deleted afterwards and changed in the app.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import User, make_session_factory  # noqa: E402
from app.security import hash_password, password_policy_error  # noqa: E402


def main() -> int:
    email, password = os.environ.get("ADMIN_EMAIL", "").strip().lower(), os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        return 0
    sm = make_session_factory(Settings.from_env().db_url)
    with sm() as db:
        if db.scalar(select(User).where(User.role == "admin", User.active.is_(True))):
            return 0
        if err := password_policy_error(password):
            print(f"ADMIN_PASSWORD rejected: {err}")
            return 1
        db.add(User(email=email, name=os.environ.get("ADMIN_NAME", "Admin"), role="admin",
                    password_hash=hash_password(password)))
        db.commit()
    print(f"Created first admin {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
