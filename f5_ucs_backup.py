#!/usr/bin/env python3
"""
Create, download and clean up BIG-IP UCS archives via iControl REST.

Works with Python 3.6+ (RHEL 8 platform python) and needs only `requests`
(RHEL 8: dnf install python3-requests).

Credentials are read from environment variables (or prompted for):
  F5_USER, F5_PASSWORD        login (local or remote/TACACS user)
  F5_UCS_PASSPHRASE           only used with --encrypt

Examples:
  ./f5_ucs_backup.py bigip01 bigip02 -o /backup/f5
  ./f5_ucs_backup.py -f hosts.txt -o /backup/f5 --encrypt --ca-bundle /etc/pki/tls/certs/corp-ca.pem
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


def log(host, msg, stream=None):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("{} [{}] {}".format(ts, host, msg), file=stream or sys.stdout, flush=True)


class BigIP:
    def __init__(self, host, user, password, verify):
        self.host = host
        self.base = "https://{}".format(host)
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers["Content-Type"] = "application/json"
        self.token = None
        self._login(user, password)

    def _login(self, user, password):
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

    def create_ucs(self, name, passphrase=None):
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

    def download_ucs(self, name, dest):
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
                f.write(r.content)
                start += len(r.content)
                if not r.content:
                    raise RuntimeError("Empty chunk at offset {}".format(start))
        size = os.path.getsize(tmp)
        if size != total:
            raise RuntimeError("Size mismatch: got {} of {} bytes".format(size, total))
        os.rename(tmp, dest)
        return size

    def delete_ucs(self, name):
        r = self.s.delete(self.base + "/mgmt/tm/sys/ucs/" + name, timeout=60)
        r.raise_for_status()

    def list_ucs(self):
        """Return file names (no path) of all UCS archives in /var/local/ucs."""
        r = self.s.get(self.base + "/mgmt/tm/sys/ucs", timeout=60)
        r.raise_for_status()
        names = []
        for item in r.json().get("items", []):
            path = item.get("apiRawValues", {}).get("filename") or item.get("name", "")
            if path:
                names.append(os.path.basename(path))
        return names


def own_archives(host, names):
    """Archives created by this script for this host, oldest first."""
    prefix = host.replace(":", "_")
    pattern = re.compile(r"^{}_\d{{8}}-\d{{6}}\.ucs$".format(re.escape(prefix)))
    return sorted(n for n in names if pattern.match(n))  # timestamp sorts by name


def prune(host, outdir, keep):
    """Keep only the newest `keep` archives for this host in outdir."""
    for old in own_archives(host, os.listdir(outdir))[:-keep]:
        os.remove(os.path.join(outdir, old))
        log(host, "pruned local {}".format(old))


def prune_device(dev, host, keep):
    """Keep only the newest `keep` script-created archives on the device."""
    for old in own_archives(host, dev.list_ucs())[:-keep]:
        dev.delete_ucs(old)
        log(host, "pruned on device {}".format(old))


def backup(host, args, user, password, passphrase):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "{}_{}.ucs".format(host.replace(":", "_"), stamp)
    dest = os.path.join(args.outdir, name)

    dev = BigIP(host, user, password, args.verify)
    try:
        log(host, "creating {}{}".format(name, " (encrypted)" if passphrase else ""))
        dev.create_ucs(name, passphrase)
        log(host, "downloading")
        size = dev.download_ucs(name, dest)
        log(host, "saved {} ({:.1f} MB)".format(dest, size / 1048576))
        if args.keep:
            prune(host, args.outdir, args.keep)
        if args.device_keep:
            prune_device(dev, host, args.device_keep)
        else:
            dev.delete_ucs(name)
            log(host, "removed archive from device")
    finally:
        dev.logout()


def main():
    p = argparse.ArgumentParser(description="Back up BIG-IP UCS archives via iControl REST")
    p.add_argument("hosts", nargs="*", help="BIG-IP hostnames/IPs")
    p.add_argument("-f", "--hosts-file", help="file with one host per line (# for comments)")
    p.add_argument("-o", "--outdir", default=".", help="where to store archives (default: .)")
    p.add_argument("--encrypt", action="store_true",
                   help="encrypt archive with passphrase from F5_UCS_PASSPHRASE (or prompt)")
    p.add_argument("-k", "--keep", type=int, default=0, metavar="N",
                   help="keep only the newest N local archives per host (default: 0 = keep all)")
    p.add_argument("-d", "--device-keep", type=int, default=0, metavar="N",
                   help="keep the newest N archives on the device "
                        "(default: 0 = delete from device after download)")
    tls = p.add_mutually_exclusive_group()
    tls.add_argument("--ca-bundle", help="CA bundle to verify the BIG-IP certificate")
    tls.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    args = p.parse_args()

    hosts = list(args.hosts)
    if args.hosts_file:
        with open(args.hosts_file) as fh:
            hosts += [l.strip() for l in fh if l.strip() and not l.lstrip().startswith("#")]
    if not hosts:
        p.error("no hosts given")

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
    for host in hosts:
        try:
            backup(host, args, user, password, passphrase)
        except Exception as e:  # keep going with the other devices
            log(host, "FAILED: {}".format(e))
            failed.append(host)

    if failed:
        log("summary", "FAILED: {}".format(", ".join(failed)), sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
