"""Create a synthetic local firm/user/membership and optionally a session.

This is explicit local provisioning tooling, not a production login flow and
not an identity provider.  It writes only to the database path you pass and
refuses to touch the repository's synthetic demo.db.

Example (PowerShell):
    py -3.11 tools/provision_auth.py `
        --db D:\\path\\to\\local-uploads.db `
        --firm "Demo Law Firm" `
        --subject local:demo-lawyer `
        --display-name "Demo Lawyer" `
        --role lawyer `
        --session
"""

import argparse
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth  # noqa: E402
import db  # noqa: E402
from app_config import DEFAULT_DEMO_DB  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Provision a synthetic local firm, user, membership, and session."
    )
    parser.add_argument("--db", required=True, help="Explicit writable SQLite path")
    parser.add_argument("--firm", required=True, help="Firm name")
    parser.add_argument("--subject", required=True, help="Stable external user subject")
    parser.add_argument("--display-name", default=None, help="Display name")
    parser.add_argument(
        "--role",
        default=auth.ROLE_LAWYER,
        choices=sorted(auth.VALID_ROLES),
        help="Firm role (default: lawyer)",
    )
    parser.add_argument(
        "--session",
        action="store_true",
        help="Create an opaque session token and print it once",
    )
    parser.add_argument(
        "--ttl",
        type=int,
        default=8 * 60 * 60,
        help="Session lifetime in seconds (default: 28800)",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if db_path == Path(DEFAULT_DEMO_DB).resolve():
        parser.error("Refusing to provision into the read-only synthetic demo.db.")
    if not db_path.parent.is_dir():
        parser.error("The directory for --db does not exist: %s" % db_path.parent)

    with db.database_context(str(db_path)):
        db.init_db()

        firm = auth.find_firm(args.firm)
        firm_id = firm["firm_id"] if firm else auth.create_firm(args.firm)
        user_id = auth.create_user(args.subject, args.display_name)
        auth.add_membership(firm_id, user_id, args.role)

        print("firm_id=%d user_id=%d role=%s db=%s"
              % (firm_id, user_id, args.role, db_path))

        if args.session:
            token = auth.create_session(user_id, firm_id, ttl_seconds=args.ttl)
            print("session_token=%s" % token)
            print(
                "Set this value as the HttpOnly cookie named %r. Treat it as a "
                "secret and do not commit it." % auth.SESSION_COOKIE_NAME
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
