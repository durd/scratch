#!/usr/bin/env python3
"""
Back up F5 devices via their REST APIs (HTTPS/443 only):
  - BIG-IP / TMOS:  UCS archive          (iControl REST)
  - F5OS (rSeries): config database XML  (F5OS API, /api on 443)

Works with Python 3.6+ and needs only `requests` (RHEL 8: dnf install python3-requests).

Hosts (arguments or -f file, one per line, # for comments):
  bigip01            TMOS (default when no prefix)
  tmos:bigip02       TMOS
  f5os:rseries01     F5OS

Credentials from environment variables (prompted for only when run on a terminal):
  F5_USER, F5_PASSWORD     same account used for all devices
  F5_UCS_PASSPHRASE        only with --encrypt (TMOS only; F5OS backups are
                           protected by the device primary key instead)

Examples:
  ./f5_ucs_backup.py bigip01 f5os:rseries01 -o /backup/f5
  ./f5_ucs_backup.py -f hosts.txt -o /backup/f5 --encrypt -k 14 -d 1 --insecure
"""
import argparse
import datetime
import getpass
import os
import re
import sys
import time

import requests

CHUNK = 1024 * 1024          # 1 MB, iControl REST max chunk size
TASK_TIMEOUT = 30 * 60       # seconds to wait for UCS creation
POLL_INTERVAL = 10
TYPES = ("tmos", "f5os")


def log(host, msg, stream=None):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("{} [{}] {}".format(ts, host, msg), file=stream or sys.stdout, flush=True)


def safe(host):
    return host.replace(":", "_")


# --------------------------------------------------------------------------- TMOS
class BigIP:
    ext = ".ucs"

    def __init__(self, host, user, password, verify):
        self.host = host
        self.base = "https://{}".format(host)
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["Content-Type"] = "application/json"
        self.token = None
        # Token auth works for both local and remote (TACACS+/RADIUS/LDAP) users
        r = self.s.post(self.base + "/mgmt/shared/authn/login", timeout=30, json={
            "username": user,
            "password": password,
            "loginProviderName": "tmos",
        })
        r.raise_for_status()
        self.token = r.json()["token"]["token"]
        self.s.headers["X-F5-Auth-Token"] = self.token

    def logout(self):
        if self.token:
            try:
                self.s.delete(self.base + "/mgmt/shared/authz/tokens/" + self.token, timeout=30)
            except requests.RequestException:
                pass

    def create(self, name, passphrase=None):
        body = {"command": "save", "name": name}
        if passphrase:
            body["options"] = [{"passphrase": passphrase}]

        # Asynchronous task: avoids the REST timeout on large archives
        url = self.base + "/mgmt/tm/task/sys/ucs"
        r = self.s.post(url, json=body, timeout=60)
        r.raise_for_status()
        task_id = r.json()["_taskId"]

        # A task does nothing until it is set to VALIDATING
        r = self.s.put("{}/{}".format(url, task_id), json={"_taskState": "VALIDATING"}, timeout=60)
        r.raise_for_status()

        deadline = time.time() + TASK_TIMEOUT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                r = self.s.get("{}/{}".format(url, task_id), timeout=60)
                r.raise_for_status()
            except requests.RequestException:
                continue  # restjavad can be briefly unresponsive during save
            state = r.json().get("_taskState")
            if state == "COMPLETED":
                return
            if state == "FAILED":
                raise RuntimeError("UCS task failed: {}".format(r.text))
        raise RuntimeError("UCS task timed out after {}s".format(TASK_TIMEOUT))

    def download(self, name, dest):
        url = self.base + "/mgmt/shared/file-transfer/ucs-downloads/" + name
        start, total = 0, None
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            os.chmod(tmp, 0o600)
            while total is None or start < total:
                end = start + CHUNK - 1
                if total is not None:
                    end = min(end, total - 1)
                headers = {
                    "Content-Type": "application/octet-stream",
                    "Content-Range": "{}-{}/{}".format(start, end, total or 0),
                }
                r = self.s.get(url, headers=headers, timeout=120)
                r.raise_for_status()
                # Response header looks like "0-1048575/52428800"
                cr = r.headers.get("Content-Range", "")
                if "/" not in cr:
                    raise RuntimeError("No Content-Range in response")
                total = int(cr.split("/")[1])
                if not r.content:
                    raise RuntimeError("Empty chunk at offset {}".format(start))
                f.write(r.content)
                start += len(r.content)
        size = os.path.getsize(tmp)
        if size != total:
            raise RuntimeError("Size mismatch: got {} of {} bytes".format(size, total))
        os.rename(tmp, dest)
        return size

    def delete(self, name):
        r = self.s.delete(self.base + "/mgmt/tm/sys/ucs/" + name, timeout=60)
        r.raise_for_status()

    def list(self):
        """File names (no path) of all UCS archives in /var/local/ucs."""
        r = self.s.get(self.base + "/mgmt/tm/sys/ucs", timeout=60)
        r.raise_for_status()
        names = []
        for item in r.json().get("items", []):
            path = item.get("apiRawValues", {}).get("filename") or item.get("name", "")
            if path:
                names.append(os.path.basename(path))
        return names


# --------------------------------------------------------------------------- F5OS
class F5OS:
    ext = ".xml"
    path = "configs/"

    def __init__(self, host, user, password, verify):
        self.host = host
        self.base = "https://{}/api/data".format(host)
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["Content-Type"] = "application/yang-data+json"
        self.s.headers["Accept"] = "application/yang-data+json"
        # Basic auth once; the response carries a token used for the rest
        r = self.s.get(self.base + "/openconfig-system:system/aaa",
                       auth=(user, password), timeout=30)
        r.raise_for_status()
        self.token = r.headers.get("X-Auth-Token")
        if not self.token:
            raise RuntimeError("F5OS login returned no X-Auth-Token")
        self.s.headers["X-Auth-Token"] = self.token

    def logout(self):
        pass  # tokens expire on their own after 15 minutes

    def create(self, name, passphrase=None):
        r = self.s.post(self.base + "/openconfig-system:system/f5-database:database/f5-database:config-backup",
                        json={"f5-database:name": name}, timeout=300)
        r.raise_for_status()

    def download(self, name, dest):
        # Same form post the webUI uses; the token goes in the form body
        url = self.base + "/f5-utils-file-transfer:file/f5-file-download:download-file/f5-file-download:start-download"
        tmp = dest + ".part"
        with self.s.post(url, timeout=120, stream=True,
                         headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "*/*"},
                         data={"file-name": name, "file-path": self.path, "token": self.token}) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                os.chmod(tmp, 0o600)
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        size = os.path.getsize(tmp)
        with open(tmp, "rb") as f:
            head = f.read(200).lstrip()
        if size == 0 or head.startswith(b"{"):  # empty file or a JSON error instead of XML
            os.remove(tmp)
            raise RuntimeError("Download failed: {}".format(head.decode(errors="replace")))
        os.rename(tmp, dest)
        return size

    def delete(self, name):
        r = self.s.post(self.base + "/f5-utils-file-transfer:file/delete",
                        json={"f5-utils-file-transfer:file-name": self.path + name}, timeout=60)
        r.raise_for_status()

    def list(self):
        r = self.s.post(self.base + "/f5-utils-file-transfer:file/list",
                        json={"f5-utils-file-transfer:path": self.path}, timeout=60)
        if r.status_code == 204:
            return []
        r.raise_for_status()
        entries = r.json().get("f5-utils-file-transfer:output", {}).get("entries", [])
        return [e.get("name", "").strip() for e in entries]


DRIVERS = {"tmos": BigIP, "f5os": F5OS}


# --------------------------------------------------------------------------- common
def own_archives(host, names, ext):
    """Backups created by this script for this host, oldest first."""
    pattern = re.compile(r"^{}_\d{{8}}-\d{{6}}{}$".format(re.escape(safe(host)), re.escape(ext)))
    return sorted(n for n in names if pattern.match(n))  # timestamp sorts by name


def prune_local(host, outdir, keep, ext):
    for old in own_archives(host, os.listdir(outdir), ext)[:-keep]:
        os.remove(os.path.join(outdir, old))
        log(host, "pruned local {}".format(old))


def prune_device(dev, keep):
    for old in own_archives(dev.host, dev.list(), dev.ext)[:-keep]:
        dev.delete(old)
        log(dev.host, "pruned on device {}".format(old))


def parse_host(line):
    """'f5os:host' -> ('f5os', 'host'); no prefix -> tmos. IPv6 addresses pass through."""
    m = re.match(r"^([A-Za-z0-9]+):(.+)$", line)
    if not m:
        return "tmos", line
    kind, host = m.group(1).lower(), m.group(2)
    if kind in TYPES:
        return kind, host
    if line.count(":") > 1:  # bare IPv6 address
        return "tmos", line
    raise ValueError("unknown device type '{}' in '{}' (use {})".format(
        m.group(1), line, " or ".join(TYPES)))


def backup(kind, host, args, user, password, passphrase):
    driver = DRIVERS[kind]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "{}_{}{}".format(safe(host), stamp, driver.ext)
    dest = os.path.join(args.outdir, name)

    dev = driver(host, user, password, args.verify)
    try:
        if kind == "f5os" and passphrase:
            log(host, "note: --encrypt does not apply to F5OS (secrets use the primary key)")
            passphrase = None
        log(host, "{}: creating {}{}".format(kind, name, " (encrypted)" if passphrase else ""))
        dev.create(name, passphrase)
        log(host, "downloading")
        size = dev.download(name, dest)
        log(host, "saved {} ({:.1f} MB)".format(dest, size / 1048576))
        if args.keep:
            prune_local(host, args.outdir, args.keep, driver.ext)
        if args.device_keep:
            prune_device(dev, args.device_keep)
        else:
            dev.delete(name)
            log(host, "removed backup from device")
    finally:
        dev.logout()


def main():
    p = argparse.ArgumentParser(description="Back up BIG-IP (UCS) and F5OS (config DB) via REST")
    p.add_argument("hosts", nargs="*", help="hosts, optionally prefixed tmos: or f5os:")
    p.add_argument("-f", "--hosts-file", help="file with one host per line (# for comments)")
    p.add_argument("-o", "--outdir", default=".", help="where to store backups (default: .)")
    p.add_argument("--encrypt", action="store_true",
                   help="encrypt UCS archives with passphrase from F5_UCS_PASSPHRASE (TMOS only)")
    p.add_argument("-k", "--keep", type=int, default=0, metavar="N",
                   help="keep only the newest N local backups per host (default: 0 = keep all)")
    p.add_argument("-d", "--device-keep", type=int, default=0, metavar="N",
                   help="keep the newest N backups on the device "
                        "(default: 0 = delete from device after download)")
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", help="CA bundle to verify device certificates")
    tls.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    args = p.parse_args()

    lines = list(args.hosts)
    if args.hosts_file:
        with open(args.hosts_file) as fh:
            lines += [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        p.error("no hosts given")
    try:
        targets = [parse_host(l) for l in lines]
    except ValueError as e:
        p.error(str(e))

    args.verify = args.ca_bundle if args.ca_bundle else not args.insecure
    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    def secret(var, prompt, hidden=True):
        val = os.environ.get(var)
        if val:
            return val
        if not sys.stdin.isatty():  # cron etc.: never prompt
            p.error("{} is not set and there is no terminal to prompt on".format(var))
        return getpass.getpass(prompt) if hidden else input(prompt)

    user = secret("F5_USER", "Username: ", hidden=False)
    password = secret("F5_PASSWORD", "Password: ")
    passphrase = None
    if args.encrypt:
        passphrase = secret("F5_UCS_PASSPHRASE", "UCS passphrase: ")
        if not passphrase:
            p.error("--encrypt needs a passphrase")

    os.makedirs(args.outdir, exist_ok=True)
    os.umask(0o077)

    failed = []
    for kind, host in targets:
        try:
            backup(kind, host, args, user, password, passphrase)
        except Exception as e:  # keep going with the other devices
            log(host, "FAILED: {}".format(e))
            failed.append(host)

    if failed:
        log("summary", "FAILED: {}".format(", ".join(failed)), sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
