"""Create a user: python scripts/create_user.py EMAIL ROLE [NAME]   (password is prompted, never echoed)."""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import User, make_session_factory  # noqa: E402
from app.security import ROLES, hash_password, password_policy_error  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[2] not in ROLES:
        print(f"usage: create_user.py EMAIL {'|'.join(ROLES)} [NAME]")
        return 2
    email, role, name = sys.argv[1].lower(), sys.argv[2], " ".join(sys.argv[3:])
    password = getpass.getpass("Password: ")
    if err := password_policy_error(password):
        print(err)
        return 1
    if password != getpass.getpass("Repeat: "):
        print("Passwords do not match")
        return 1
    sm = make_session_factory(Settings.from_env().db_url)
    with sm() as db:
        if db.scalar(select(User).where(User.email == email)):
            print("User already exists")
            return 1
        db.add(User(email=email, name=name, role=role, password_hash=hash_password(password)))
        db.commit()
    print(f"Created {role} {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
