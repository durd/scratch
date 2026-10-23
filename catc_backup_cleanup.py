#!/usr/bin/env python3
"""Delete Catalyst Center backups older than N days.

There is no delete/retention API on the physical (on-prem) appliance as of
2.3.7.11, and Cisco's own confirmed workaround is to remove old backup-id
folders directly from the backup storage (NFS/SFTP target). Catalyst
Center's DB/UI syncs automatically once the files are gone.

Run this ON the backup storage host (or against a mounted/mapped path to it),
pointing --backup-root at the top of the share. Each service has its own
subdirectory there, and each contains folders named by backup UUID - this
script deletes any such UUID folder older than --days, in any service dir.

For the legacy API-based approach (works on ESXi VA / newer releases that
do have the backup API), see the --host/--username API mode below.

Needs Python 3.12.13+ and `requests`.
"""

import argparse
import base64
import getpass
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import urllib3

if sys.version_info < (3, 12, 13):
    sys.exit(f"Needs Python 3.12.13 or newer, this is {sys.version.split()[0]}")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def parse_args():
    p = argparse.ArgumentParser(description="Delete Catalyst Center backups older than N days")
    sub = p.add_subparsers(dest="mode", required=True)

    # --- storage mode: works on-prem, no API needed ---
    s = sub.add_parser("storage", help="Delete old backup-id folders directly from the backup share")
    s.add_argument("--backup-root", required=True, type=Path, help="Path to the top of the backup share")
    s.add_argument("--days", type=int, default=30, help="Delete folders older than this many days (default: 30)")
    s.add_argument("--apply", action="store_true", help="Actually delete. Without this flag, only lists what would be deleted.")

    # --- api mode: for deployments that do have the backup API (ESXi VA / newer releases) ---
    a = sub.add_parser("api", help="Delete old backups via the Catalyst Center REST API (if available)")
    a.add_argument("--host", required=True, help="Catalyst Center URL, e.g. https://catc.example.com")
    a.add_argument("--username", required=True, help="API username")
    a.add_argument("--password", help="API password (omit to be prompted securely)")
    a.add_argument("--days", type=int, default=30, help="Delete backups older than this many days (default: 30)")
    a.add_argument("--apply", action="store_true", help="Actually delete. Without this flag, only lists what would be deleted.")
    a.add_argument("--verify-ssl", action="store_true", help="Verify TLS certificate (default: off)")
    a.add_argument("--delete-delay", type=float, default=15.0, help="Seconds to wait between deletes to respect the 5/min rate limit (default: 15)")

    return p.parse_args()


def storage_cleanup(args):
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    candidates = []

    # backup-id folders live one level under each service folder, i.e.
    # <backup-root>/<service>/<backup-id>/
    for service_dir in args.backup_root.iterdir():
        if not service_dir.is_dir():
            continue
        for entry in service_dir.iterdir():
            if entry.is_dir() and UUID_RE.match(entry.name):
                candidates.append(entry)

    old = []
    for entry in candidates:
        mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            old.append((entry, mtime))

    print(f"Found {len(candidates)} backup-id folders total, {len(old)} older than {args.days} days.")
    for entry, mtime in old:
        print(f"  - {entry} (modified {mtime.date()})")
        if args.apply:
            shutil.rmtree(entry)

    if not args.apply and old:
        print("\nDry run only — rerun with --apply to actually delete these.")


def get_token(host, username, password, verify_ssl):
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    r = requests.post(
        f"{host}/dna/system/api/v1/auth/token",
        headers={"Authorization": f"Basic {auth}"},
        verify=verify_ssl,
    )
    r.raise_for_status()
    return r.json()["Token"]


def get_backups(host, token, verify_ssl):
    r = requests.get(
        f"{host}/dna/system/api/v1/backups",
        headers={"X-Auth-Token": token},
        verify=verify_ssl,
    )
    r.raise_for_status()
    return r.json()["response"]


def parse_created(created_date):
    # createdDate is usually epoch millis as a string; fall back to ISO parsing.
    try:
        return datetime.fromtimestamp(int(created_date) / 1000, tz=timezone.utc)
    except ValueError:
        return datetime.fromisoformat(created_date.replace("Z", "+00:00"))


def delete_backup(host, token, backup_id, verify_ssl):
    r = requests.delete(
        f"{host}/dna/system/api/v1/backups/{backup_id}",
        headers={"X-Auth-Token": token},
        verify=verify_ssl,
    )
    r.raise_for_status()
    return r.json()


def api_cleanup(args):
    host = args.host.rstrip("/")
    password = args.password or getpass.getpass("Password: ")

    token = get_token(host, args.username, password, args.verify_ssl)
    backups = get_backups(host, token, args.verify_ssl)
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)

    old_backups = [b for b in backups if parse_created(b["createdDate"]) < cutoff]

    print(f"Found {len(backups)} backups total, {len(old_backups)} older than {args.days} days.")

    for b in old_backups:
        age = parse_created(b["createdDate"])
        print(f"  - {b['name']} ({b['id']}) created {age.date()}")
        if args.apply:
            resp = delete_backup(host, token, b["id"], args.verify_ssl)
            print(f"    -> delete triggered, taskId={resp['response']['taskId']}")
            time.sleep(args.delete_delay)

    if not args.apply and old_backups:
        print("\nDry run only — rerun with --apply to actually delete these.")


def main():
    args = parse_args()
    if args.mode == "storage":
        storage_cleanup(args)
    else:
        api_cleanup(args)


if __name__ == "__main__":
    main()
