"""Create an admin account, or promote an existing user to admin.

    python -m scripts.create_admin --email admin@example.com

The password comes from the ADMIN_PASSWORD environment variable or an interactive prompt,
never from a command-line argument, which would be saved in shell history.
"""

import argparse
import getpass
import logging
import os
import sys

from pydantic import ValidationError

from app.config import get_settings
from app.db import SessionLocal
from app.logging_config import configure_logging
from app.schemas import RegisterRequest
from app.services import users

logger = logging.getLogger("scripts.create_admin")


def main() -> int:
    """Entry point; returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    configure_logging(get_settings().log_level)

    with SessionLocal() as db:
        promoted = users.promote_to_admin(db, args.email.lower())
        if promoted is not None:
            logger.info("existing user promoted", extra={"email": promoted.email})
            return 0

        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
        try:
            body = RegisterRequest(email=args.email, password=password)
        except ValidationError as exc:
            logger.error("invalid email or password", extra={"errors": exc.errors()})
            return 1
        user = users.register_user(db, body.email, body.password, role="admin")
        logger.info("admin created", extra={"user_id": user.id, "email": user.email})
    return 0


if __name__ == "__main__":
    sys.exit(main())
