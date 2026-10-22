#!/usr/bin/env python3
"""Push a single Day-N CLI template to devices via Catalyst Center.

Only the template's config is pushed (no full device provision).
Targets: individual devices (--device) and/or all devices on sites (--site).

Credentials: env vars CC_USER / CC_PASS, otherwise you are prompted.

Examples:
  # Two devices, by IP or hostname
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --device 10.1.1.10 --device sw02 --param ntp_server=10.0.0.1

  # Devices assigned directly to a building, switches only
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area/Building" --family switch

  # Everything under an area, including buildings/floors below it
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area" --recursive --dry-run

Per-device params (--params-file), keyed by hostname or management IP:
  {"sw01.example.com": {"vlan": 10}, "10.1.1.11": {"vlan": 20}}
"""
import argparse
import getpass
import ipaddress
import json
import os
import re
import sys
import time

import requests
import urllib3

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DEVICE_PATH = "/dna/intent/api/v1/network-device"


class CatalystCenter:
    def __init__(self, host, user, password, verify):
        self.base = f"https://{host}"
        self.s = requests.Session()
        self.s.verify = verify
        r = self.s.post(f"{self.base}/dna/system/api/v1/auth/token",
                        auth=(user, password), timeout=30)
        r.raise_for_status()
        self.s.headers.update({"X-Auth-Token": r.json()["Token"],
                               "Content-Type": "application/json"})

    def get(self, path, **params):
        for _ in range(5):
            r = self.s.get(self.base + path, params=params, timeout=60)
            if r.status_code != 429:
                break
            wait = int(r.headers.get("Retry-After", 30))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
        if r.status_code == 204:
            return {"response": []}
        if not r.ok:
            sys.exit(f"GET {path} failed: {r.status_code} {r.text}")
        return r.json()

    def post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=60)
        if not r.ok:
            sys.exit(f"POST {path} failed: {r.status_code} {r.text}")
        return r.json()


def find_template(cc, name, project):
    """Return (template_id, latest_version_id, version_number)."""
    params = {"projectNames": project} if project else {}
    templates = cc.get("/dna/intent/api/v1/template-programmer/template", **params)
    if isinstance(templates, dict):
        templates = templates.get("response", [])

    matches = [t for t in templates if t.get("name") == name]
    if not matches:
        sys.exit(f"Template '{name}' not found")
    if len(matches) > 1:
        projects = ", ".join(t.get("projectName", "?") for t in matches)
        sys.exit(f"Template name exists in several projects ({projects}), use --project")

    t = matches[0]
    versions = t.get("versionsInfo") or []
    if not versions:
        sys.exit(f"Template '{name}' has no committed version, commit it first")
    latest = max(versions, key=lambda v: int(v.get("version", 0)))
    return t["templateId"], latest["id"], latest.get("version")


def resolve_device(cc, name):
    """Return the inventory record for a management IP or hostname ('name' or 'name.<domain>')."""
    try:
        ipaddress.ip_address(name)
        is_ip = True
    except ValueError:
        is_ip = False

    if is_ip:
        devices = cc.get(DEVICE_PATH, managementIpAddress=name).get("response", [])
        if not devices:
            sys.exit(f"No device with management IP {name} in inventory")
        return devices[0]

    devices = cc.get(DEVICE_PATH, hostname=name).get("response", [])
    if not devices:
        devices = cc.get(DEVICE_PATH, hostname=f"{re.escape(name)}\\..*").get("response", [])
    if len(devices) == 1:
        return devices[0]
    if not devices:
        sys.exit(f"Device '{name}' not found in inventory")
    found = ", ".join(d["hostname"] for d in devices)
    sys.exit(f"Device '{name}' matches several devices ({found}), use the full hostname")


def find_site(cc, path):
    """Return (site_id, site_type) for a full site path like Global/Area/Building."""
    sites = cc.get("/dna/intent/api/v1/sites", nameHierarchy=path).get("response", [])
    sites = [s for s in sites if s.get("nameHierarchy") == path]
    if not sites:
        sys.exit(f"Site '{path}' not found (use the full path, e.g. Global/Area/Building)")
    return sites[0]["id"], sites[0].get("type", "?")


def sub_sites(cc, path):
    """IDs of all sites below path (areas, buildings, floors), not including path itself."""
    sites, offset, limit = [], 1, 500
    while True:
        page = cc.get("/dna/intent/api/v1/sites", offset=offset, limit=limit).get("response", [])
        sites += page
        if len(page) < limit:
            break
        offset += limit
    prefix = path + "/"
    return [s["id"] for s in sites if (s.get("nameHierarchy") or "").startswith(prefix)]


def direct_device_ids(cc, site_id):
    """Device IDs assigned directly to this site (not to sites below it)."""
    ids, offset, limit = [], 1, 500
    while True:
        page = cc.get("/dna/intent/api/v1/networkDevices/assignedToSite",
                      siteId=site_id, offset=offset, limit=limit).get("response", [])
        ids += [i["deviceId"] for i in page if i.get("siteId") == site_id]
        if len(page) < limit:
            return ids
        offset += limit


def site_device_ids(cc, site_id, path, recursive):
    """Device IDs on the site, and with recursive also on every site below it."""
    site_ids = [site_id]
    if recursive:
        site_ids += sub_sites(cc, path)
        print(f"  Checking {len(site_ids)} site(s) under {path}...")
    ids = []
    for sid in site_ids:
        ids += direct_device_ids(cc, sid)
    return list(dict.fromkeys(ids))  # Drop duplicates, keep order


def device_records(cc, ids):
    """Inventory records for a list of device IDs, fetched in batches."""
    records = []
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        records += cc.get(DEVICE_PATH, id=",".join(batch)).get("response", [])
    return records


def parse_kv(items):
    """key=value pairs. Values are parsed as JSON when possible (lists, numbers)."""
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            sys.exit(f"Bad --param '{item}', expected key=value")
        try:
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def report_progress(progress, devices):
    """Print CC's applicable / non-applicable targets as readable lists.

    CC's text looks like:
      ...nonApplicableTargets: {<uuid>=<reason>, ...}.ApplicableTargets: [<uuid>, ...]...
    Falls back to the raw text if that format isn't found.
    """
    def label(uuid):
        d = devices.get(uuid)
        return f"{d.get('hostname')} ({d.get('managementIpAddress')})" if d else uuid

    skipped = {}
    m = re.search(r"nonApplicableTargets:\s*\{(.*?)\}", progress)
    if m:
        for key, reason in re.findall(r"([^,=\s][^,=]*)=(.*?)(?=,\s*[^,=\s][^,=]*=|$)", m.group(1)):
            skipped[key.strip()] = reason.strip()

    m = re.search(r"(?<![a-z])ApplicableTargets:\s*(\[[^\]]*\]|\{[^}]*\}|None)", progress)
    applicable = UUID_RE.findall(m.group(1)) if m else []

    # Messages like "Template x:14 is already deployed with same params, Hence not deploying"
    notes = list(dict.fromkeys(
        n.strip(" .,") for n in re.findall(r"([^.,\]]*?), Hence not deploying on devices:", progress)))

    if not skipped and not m:
        print(f"Task done: {progress}")
        return []

    # Devices named after "Hence not deploying on devices:" were skipped too
    for part in re.findall(r"Hence not deploying on devices:\s*(\[[^\]]*\]|[0-9a-f\-, ]*)", progress):
        for uuid in UUID_RE.findall(part):
            skipped.setdefault(uuid, "not deployed")

    print(f"\nPassed CC's target check, {len(applicable)} device(s):")
    for uuid in applicable:
        print(f"  {label(uuid)}")
    if skipped:
        print(f"\nNot applicable, {len(skipped)} device(s):")
        for key, reason in skipped.items():
            print(f"  {label(key)}: {reason}")
    for note in notes:
        print(f"\nCC: {note}")

    if "Hence not deploying" in progress.rsplit(":", 1)[-1] or progress.rstrip().endswith(
            "Hence not deploying"):
        hint = " Use --force to push again." if "already deployed" in progress else ""
        print(f"\nResult: nothing was deployed.{hint}")
        return []
    else:
        print("\nResult: deploy started for the devices above. Check Deployment History in CC.")
        return [u for u in applicable if u not in skipped]


def save_config(cc, ids, devices, timeout):
    """Write memory on the given devices via CC."""
    if not ids:
        print("\nNothing deployed, so nothing to save.")
        return
    print(f"\nSaving config (write memory) on {len(ids)} device(s)...")
    resp = cc.post("/dna/intent/api/v1/network-device-config/write-memory", {"deviceId": ids})
    task = wait_task(cc, resp["response"]["taskId"], timeout)
    for uuid in ids:
        d = devices.get(uuid, {})
        print(f"  {d.get('hostname', uuid)}")
    print(f"Save task done: {task.get('progress')}")


def wait_task(cc, task_id, timeout):
    end = time.time() + timeout
    while time.time() < end:
        task = cc.get(f"/dna/intent/api/v1/task/{task_id}")["response"]
        if task.get("isError"):
            sys.exit(f"Task failed: {task.get('failureReason') or task.get('progress')}")
        if task.get("endTime"):
            return task
        time.sleep(3)
    sys.exit("Timed out waiting for task")


def wait_deployment(cc, deployment_id, timeout):
    path = f"/dna/intent/api/v1/template-programmer/template/deploy/status/{deployment_id}"
    end = time.time() + timeout
    misses = 0
    while time.time() < end:
        r = cc.s.get(cc.base + path, timeout=60)
        if r.status_code == 404:
            misses += 1
            if misses >= 5:
                return None  # Not a deployment ID, or this endpoint doesn't track v2 deploys
            time.sleep(3)
            continue
        if not r.ok:
            sys.exit(f"GET {path} failed: {r.status_code} {r.text}")
        status = r.json()
        if status.get("status") not in ("INIT", "IN_PROGRESS", None):
            return status
        time.sleep(5)
    sys.exit("Timed out waiting for deployment")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="Catalyst Center hostname/IP")
    p.add_argument("--template", required=True, help="Template name")
    p.add_argument("--project", help="Project name (if template name is not unique)")
    p.add_argument("--device", action="append", default=[],
                   help="Management IP or hostname as shown in inventory (repeatable)")
    p.add_argument("--site", action="append", default=[],
                   help="Full site path, e.g. Global/Area/Building (repeatable)")
    p.add_argument("--recursive", action="store_true",
                   help="With --site: include devices in areas/buildings/floors below it")
    p.add_argument("--family", action="append", default=[],
                   help="Only devices whose family contains this text, e.g. 'switch' (repeatable)")
    p.add_argument("--include-unreachable", action="store_true",
                   help="Also deploy to devices CC doesn't list as reachable")
    p.add_argument("--param", action="append", help="key=value for all devices (repeatable)")
    p.add_argument("--params-file", help="JSON: {\"hostname or IP\": {\"key\": \"value\"}}")
    p.add_argument("--force", action="store_true",
                   help="Force push, even to devices already on this template version")
    p.add_argument("--save", action="store_true",
                   help="Write memory on devices CC deployed to")
    p.add_argument("--save-wait", type=int, default=60,
                   help="Seconds to wait before saving when deploy completion can't be "
                        "confirmed (default 60)")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    p.add_argument("--timeout", type=int, default=600, help="Seconds to wait (default 600)")
    p.add_argument("--dry-run", action="store_true", help="Print payload, don't deploy")
    args = p.parse_args()

    if not args.device and not args.site:
        p.error("give at least one --device or --site")

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    user = os.environ.get("CC_USER") or input("Username: ")
    password = os.environ.get("CC_PASS") or getpass.getpass("Password: ")
    cc = CatalystCenter(args.host, user, password, verify=not args.insecure)

    template_id, version_id, version = find_template(cc, args.template, args.project)
    print(f"Template '{args.template}' version {version}")

    # Collect target devices, keyed by UUID so duplicates are dropped
    devices = {}
    for name in args.device:
        d = resolve_device(cc, name)
        devices[d["id"]] = d
    for path in args.site:
        site_id, site_type = find_site(cc, path)
        ids = site_device_ids(cc, site_id, path, args.recursive)
        scope = "incl. sub-sites" if args.recursive else "directly assigned"
        print(f"Site {path} ({site_type}): {len(ids)} devices {scope}")
        if not ids and not args.recursive:
            print("  Note: no devices directly on this site. If they sit on floors or "
                  "buildings below it, add --recursive.")
        for d in device_records(cc, ids):
            devices[d["id"]] = d

    if args.family:
        wanted = [f.lower() for f in args.family]
        devices = {k: d for k, d in devices.items()
                   if any(w in (d.get("family") or "").lower() for w in wanted)}

    if not args.include_unreachable:
        skipped = [d for d in devices.values() if d.get("reachabilityStatus") != "Reachable"]
        if skipped:
            print(f"\nSkipping {len(skipped)} device(s) not reachable in CC "
                  "(use --include-unreachable to include them):")
            for d in skipped:
                print(f"  {d.get('hostname')}  {d.get('managementIpAddress')}  "
                      f"{d.get('reachabilityStatus')}")
            devices = {k: d for k, d in devices.items() if d.get("reachabilityStatus") == "Reachable"}

    if not devices:
        sys.exit("No devices to deploy to")

    print(f"\n{len(devices)} target device(s):")
    for d in sorted(devices.values(), key=lambda d: d.get("hostname") or ""):
        print(f"  {d.get('hostname')}  {d.get('managementIpAddress')}  {d.get('family')}")

    common = parse_kv(args.param)
    per_device = {}
    if args.params_file:
        with open(args.params_file) as f:
            per_device = json.load(f)

    targets = []
    for d in devices.values():
        own = (per_device.get(d.get("hostname"))
               or per_device.get(d.get("managementIpAddress")) or {})
        targets.append({
            "type": "MANAGED_DEVICE_UUID",
            "id": d["id"],
            "versionedTemplateId": version_id,
            "params": {**common, **own},
        })

    body = {
        "templateId": template_id,
        "forcePushTemplate": args.force,
        "isComposite": False,
        "targetInfo": targets,
    }

    if args.dry_run:
        print(json.dumps(body, indent=2))
        return

    if not args.yes:
        answer = input(f"\nDeploy '{args.template}' v{version} to {len(targets)} device(s)? [y/N] ")
        if answer.strip().lower() != "y":
            sys.exit("Aborted")

    resp = cc.post("/dna/intent/api/v2/template-programmer/template/deploy", body)
    task_id = resp["response"]["taskId"]
    print(f"Deploy task {task_id}, waiting...")

    task = wait_task(cc, task_id, args.timeout)
    progress = task.get("progress", "")
    # Look for a deployment ID outside the target lists, ignoring known template/device IDs
    text = re.sub(r"\[[^\]]*\]|\{[^}]*\}", "", f"{progress} {task.get('data', '')}")
    known = {template_id, version_id, *devices}
    candidates = [u for u in UUID_RE.findall(text) if u not in known]
    deployed = None
    if candidates:
        status = wait_deployment(cc, candidates[0], args.timeout)
        if status is not None:
            print(f"Deployment status: {status.get('status')}")
            deployed = []
            for dev in status.get("devices", []):
                print(f"  {dev.get('name')}: {dev.get('status')} "
                      f"{dev.get('detailedStatusMessage', '')}")
                if dev.get("status") == "SUCCESS" and dev.get("deviceId"):
                    deployed.append(dev["deviceId"])

    if deployed is None:
        deployed = report_progress(progress, devices)
        if deployed and args.save:
            # CC's task ends when the deploy is handed off, not when devices are done
            print(f"\nCan't confirm the deploy has finished, waiting {args.save_wait}s "
                  "before saving...")
            time.sleep(args.save_wait)

    if args.save:
        save_config(cc, deployed, devices, args.timeout)


if __name__ == "__main__":
    main()
