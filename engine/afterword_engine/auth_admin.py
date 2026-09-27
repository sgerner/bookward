"""Operator commands for identity-registry disaster recovery."""

import argparse
from .backups import AUTH_BACKUP_ID, auth_backup_directory, restore_auth_backup
from .profiles import issue_setup_token


def main():
    parser = argparse.ArgumentParser(description="Bookward account-registry recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List identity-registry snapshots")
    commands.add_parser("setup-token", help="Issue a new first-admin setup token when no accounts exist")
    restore = commands.add_parser("restore", help="Restore a snapshot and revoke every session and API token")
    restore.add_argument("snapshot_id")
    restore.add_argument("--confirm", action="store_true", help="Confirm this operator recovery")
    args = parser.parse_args()
    if args.command == "setup-token":
        try:
            print(issue_setup_token())
        except PermissionError as exc:
            parser.error(str(exc))
        return
    if args.command == "list":
        directory = auth_backup_directory()
        if not directory.is_dir():
            print("No identity-registry snapshots found.")
            return
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if path.is_file() and not path.is_symlink() and AUTH_BACKUP_ID.fullmatch(path.stem):
                print(f"{path.stem}\t{path.stat().st_size} bytes")
        return
    if not args.confirm:
        parser.error("Restoring replaces the account registry; pass --confirm after stopping the engine")
    result = restore_auth_backup(args.snapshot_id)
    print("Identity registry restored. All sessions and API tokens were revoked.")
    if result.get("setup_token"):
        print(f"Initial administrator setup token: {result['setup_token']}")


if __name__ == "__main__":
    main()
