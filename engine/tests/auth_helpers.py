"""Authenticated clients for engine tests that exercise private routes."""

import uuid

from afterword_engine import profiles


def authenticated_headers() -> dict[str, str]:
    profiles.initialize_auth()
    with profiles.auth_connect() as con:
        admin = con.execute(
            "SELECT id FROM accounts WHERE profile_id='legacy' AND role='admin' "
            "AND status='active' ORDER BY created_at,id LIMIT 1"
        ).fetchone()

    if admin is None:
        if profiles.account_count() == 0:
            setup_token = profiles.issue_setup_token()
            account = profiles.create_first_admin(
                setup_token, "test-admin", "test-password-with-sufficient-length"
            )
        else:
            account = profiles.create_account(
                f"test-admin-{uuid.uuid4().hex[:8]}",
                "test-password-with-sufficient-length",
                role="admin",
            )
        account_id = account["id"]
    else:
        account_id = admin["id"]

    session, _ = profiles.create_session(account_id)
    return {"x-bookward-session": session}
