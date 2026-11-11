"""Mock mode only: create one demo user per role with RANDOM passwords, printed once. Dummy data only."""

import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Module, User, make_session_factory  # noqa: E402
from app.security import hash_password  # noqa: E402

settings = Settings.from_env()
if not settings.mock_gemini:
    sys.exit("seed_demo.py runs in mock mode only (MOCK_GEMINI=true).")
sm = make_session_factory(settings.db_url)
with sm() as db:
    for role in ("admin", "operator", "reviewer"):
        email = f"{role}@example.test"
        if db.scalar(select(User).where(User.email == email)):
            print(f"{email}: already exists (password unchanged)")
            continue
        pw = "Demo-" + secrets.token_urlsafe(12) + "1"
        db.add(User(email=email, name=f"Demo {role}", role=role, password_hash=hash_password(pw)))
        print(f"{email}  password: {pw}")
    if not db.scalar(select(Module).where(Module.code == "BUSINESSFUNDAMENTALS")):
        db.add(Module(code="BUSINESSFUNDAMENTALS", name="Business Fundamentals",
                      learning_outcomes="DUMMY: participants can compute unit cost and a simple price.",
                      reference_content="DUMMY REFERENCE: unit cost = fixed cost share + variable cost; "
                                        "price = unit cost + margin; break-even = fixed cost / (price - variable cost)."))
    db.commit()
print("Done. Data directory:", settings.data_dir)
