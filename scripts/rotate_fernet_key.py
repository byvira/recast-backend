"""Rotate FERNET_SECRET_KEY — re-encrypt every workspace_connections token.

FERNET_SECRET_KEY encrypts OAuth access/refresh tokens in workspace_connections
(see app/pipelines/publish/token_store.py). Rotating the key without migrating
existing rows makes every stored token permanently undecryptable — every
connected account would have to reconnect.

This script decrypts each token with the OLD key and re-encrypts it with the
NEW key, one document at a time (never a batch held in memory), so a crash
mid-run leaves already-migrated documents migrated and the rest untouched —
safe to just re-run. It never deletes anything and never touches a document
that has no access_token/refresh_token to migrate.

Usage:
    # 1. Generate a new key if you don't have one yet:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    # 2. Dry run first — reports what WOULD change, writes nothing:
    python -m scripts.rotate_fernet_key --old-key <b64> --new-key <b64> --dry-run

    # 3. Real run — only after the dry run looks right:
    python -m scripts.rotate_fernet_key --old-key <b64> --new-key <b64> --yes

After a successful real run:
    - Set FERNET_SECRET_KEY to the NEW key in .env / Render.
    - Restart the app.
    - Confirm a live OAuth-dependent flow (publish, or the token-refresh
      worker) works before discarding the OLD key.

Needs the real MONGODB_URL from .env (same as the app) — this only talks to
the database, not to any OAuth provider.
"""

import argparse
import asyncio
import sys

from cryptography.fernet import Fernet, InvalidToken

from app.db.mongo import workspace_connections, get_client as get_mongo_client

_TOKEN_FIELDS = ("access_token", "refresh_token")


async def _rotate(old_key: str, new_key: str, dry_run: bool) -> int:
    try:
        old_fernet = Fernet(old_key.encode())
        new_fernet = Fernet(new_key.encode())
    except Exception as e:  # noqa: BLE001
        print(f"Invalid key: {e}")
        return 1

    total = 0
    migrated = 0
    skipped = 0
    failed = 0

    async for doc in workspace_connections.find({}):
        total += 1
        workspace_id = doc.get("workspace_id")
        platform = doc.get("platform")
        label = f"{workspace_id}/{platform}"

        updates: dict[str, str] = {}
        mismatch = False

        for field in _TOKEN_FIELDS:
            encrypted = doc.get(field)
            if not encrypted:
                continue
            try:
                plaintext = old_fernet.decrypt(encrypted.encode()).decode()
            except InvalidToken:
                print(f"  SKIP {label}: {field} doesn't decrypt with --old-key "
                      f"(already rotated, or corrupted) — left untouched")
                mismatch = True
                break
            updates[field] = new_fernet.encrypt(plaintext.encode()).decode()

        if mismatch:
            skipped += 1
            continue
        if not updates:
            continue  # no tokens on this doc — nothing to migrate

        if dry_run:
            print(f"  WOULD update {label} ({len(updates)} field(s))")
            migrated += 1
            continue

        try:
            await workspace_connections.update_one({"_id": doc["_id"]}, {"$set": updates})
            print(f"  OK {label}")
            migrated += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED to write {label}: {e}")
            failed += 1

    print()
    print(f"Total documents seen:                {total}")
    print(f"{'Would migrate' if dry_run else 'Migrated'}:{'':16}{migrated}")
    print(f"Skipped (didn't match --old-key):    {skipped}")
    print(f"Failed writes:                       {failed}")

    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--old-key", required=True, help="Current FERNET_SECRET_KEY value")
    parser.add_argument("--new-key", required=True, help="New FERNET_SECRET_KEY value")
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    parser.add_argument("--yes", action="store_true", help="Required to write changes for real")
    args = parser.parse_args()

    if args.old_key == args.new_key:
        print("--old-key and --new-key are identical — nothing to rotate.")
        return 1

    if not args.dry_run and not args.yes:
        print("Refusing to write changes without --yes (run with --dry-run first).")
        return 1

    try:
        return asyncio.run(_rotate(args.old_key, args.new_key, args.dry_run))
    finally:
        get_mongo_client().close()


if __name__ == "__main__":
    sys.exit(main())
