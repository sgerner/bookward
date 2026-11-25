"""Operator commands for identity-registry disaster recovery."""

import argparse
from .backups import AUTH_BACKUP_ID, auth_backup_directory, restore_auth_backup
from .profiles import auth_path, unlock_all_accounts, unlock_username


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Bookward account-registry recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List identity-registry snapshots")
    unlock = commands.add_parser(
        "unlock",
        help="Clear failed sign-in attempts without restoring snapshots or revoking sessions",
    )
    unlock.add_argument(
        "username",
        nargs="?",
        help="Username or account ID to unlock (omit with --all to clear all accounts)",
    )
    unlock.add_argument(
        "--ip",
        dest="remote_address",
        default=None,
        help="Optional client IP address to unlock for the specified account",
    )
    unlock.add_argument(
        "--all",
        action="store_true",
        dest="unlock_all",
        help="Clear failed sign-in attempts across all accounts",
    )
    restore = commands.add_parser("restore", help="Restore a snapshot and revoke every session and API token")
    restore.add_argument("snapshot_id")
    restore.add_argument("--confirm", action="store_true", help="Confirm this operator recovery")
    args = parser.parse_args(argv)
    if args.command == "list":
        directory = auth_backup_directory()
        if not directory.is_dir():
            print("No identity-registry snapshots found.")
            return
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_file() and not path.is_symlink() and AUTH_BACKUP_ID.fullmatch(path.stem):
                print(f"{path.stem}\t{path.stat().st_size} bytes")
        return
    if args.command == "unlock":
        if args.unlock_all and (args.username or args.remote_address):
            parser.error("Do not combine --all with a username or --ip filter")
        if not args.unlock_all and not args.username:
            parser.error("Provide a username to unlock or pass --all")
        if not auth_path().is_file():
            parser.error("Identity registry database not found")
        if args.unlock_all:
            result = unlock_all_accounts()
            print(f"Cleared {result['cleared_attempts']} failed sign-in attempts across all accounts.")
            return
        try:
            result = unlock_username(args.username, args.remote_address)
        except (LookupError, ValueError) as exc:
            parser.error(str(exc))
        scope_note = f" from {args.remote_address.strip()}" if args.remote_address else ""
        print(
            f"Unlocked account {result['username']}{scope_note} "
            f"(cleared {result['cleared_attempts']} failed sign-in attempts)."
        )
        return
    if not args.confirm:
        parser.error("Restoring replaces the account registry; pass --confirm after stopping the engine")
    restore_auth_backup(args.snapshot_id)
    print("Identity registry restored. All sessions and API tokens were revoked.")


if __name__ == "__main__":
    main()
