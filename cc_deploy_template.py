#!/usr/bin/env python3
r"""Push a single Day-N CLI template to devices via Catalyst Center.

Only the template's config is pushed (no full device provision).
Targets: individual devices (--device) and/or all devices on sites (--site).

Credentials: env vars CC_USER / CC_PASS, otherwise you are prompted.

Needs Python 3.12+ and `requests`. Tested against Catalyst Center 2.3.7.11.

Examples:
  # Two devices, by IP or hostname
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --device 10.1.1.10 --device sw02 --param ntp_server=10.0.0.1

  # Devices assigned directly to a building, switches only
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area/Building" --family switch

  # Only access switches under an area
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area" --recursive --family switch --role access

  # A floor, but not its 2960S switches or two named devices
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area/Building/Floor 1" --exclude-platform 2960S \
      --exclude-device sw07 --exclude-device 10.1.1.99

  # Staged rollout: 1 device, then 2, then 4 at a time, asking between batches
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area/Building" --batches 1,2,4

  # Everything under an area, including buildings/floors below it
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area" --recursive --dry-run

  # Same, and also print the JSON request that would be sent to CC
  cc_deploy_template.py --host cc.example.com --template "Fix-NTP" \
      --site "Global/Area" --recursive --dry-run --debug

Per-device params (--params-file), keyed by hostname or management IP:
  {"sw01.example.com": {"vlan": 10}, "10.1.1.11": {"vlan": 20}}
"""
import argparse
import fnmatch
import getpass
import ipaddress
import json
import os
import re
import sys
import time

import requests
import urllib3

if sys.version_info < (3, 12):
    sys.exit(f"Needs Python 3.12 or newer, this is {sys.version.split()[0]}")

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


def red(text):
    """Red on a terminal, plain when piped or NO_COLOR is set."""
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR"):
        return f"\033[31m{text}\033[0m"
    return text


def print_table(rows, headers, highlight=None):
    """Print rows as an indented table, each column as wide as its longest value.

    highlight(header, value) -> True prints that cell in red.
    """
    rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells, check=False):
        # Pad outside the colour codes, so they don't count towards the width
        out = []
        for h, c, w in zip(headers, cells, widths):
            pad = " " * (w - len(c))
            out.append((red(c) if check and highlight and highlight(h, c) else c) + pad)
        return ("  " + "  ".join(out)).rstrip()

    print(line(headers))
    print(line(["-" * w for w in widths]))
    for row in rows:
        print(line(row, check=True))


def show_value(v):
    """Template param value as text: strings as-is, everything else as JSON."""
    return v if isinstance(v, str) else json.dumps(v)


def print_params(targets, devices):
    """Show the template params each device gets, once if they're all the same."""
    params = [t["params"] for t in targets]
    if not any(params):
        print("\nNo template parameters.")
        return
    if all(p == params[0] for p in params):
        print("\nTemplate parameters:" if len(params) == 1
              else "\nTemplate parameters (same for all devices):")
        print_table([[k, show_value(v)] for k, v in params[0].items()], ["Parameter", "Value"])
        return
    keys = list(dict.fromkeys(k for p in params for k in p))
    rows = []
    for t in targets:
        d = devices[t["id"]]
        rows.append([d.get("hostname")] + [show_value(t["params"][k]) if k in t["params"] else "-"
                                           for k in keys])
    print("\nTemplate parameters per device:")
    print_table(sorted(rows, key=lambda r: r[0] or ""), ["Hostname", *keys])


def load_params_file(path):
    """Per-device params from a JSON file, or from stdin when path is '-'."""
    try:
        if path == "-":
            data = json.load(sys.stdin)
        else:
            with open(path) as f:
                data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Can't read params from {'stdin' if path == '-' else path}: {e}")
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        sys.exit('Params must be a JSON object like {"hostname or IP": {"key": "value"}}')
    return data


def read_exclude_file(path):
    """Hostnames or IPs, one per line; blank lines and # comments are ignored."""
    try:
        with open(path) as f:
            lines = [line.split("#", 1)[0].strip() for line in f]
    except OSError as e:
        sys.exit(f"Can't read exclude file {path}: {e}")
    return [line for line in lines if line]


def matches_name(d, name):
    """Management IP, full hostname, or short hostname ('sw01' for 'sw01.example.com').

    With * ? or [...] in name, a wildcard match against any of them ('switch-*', '10.1.1.*').
    """
    host = (d.get("hostname") or "").lower()
    candidates = (d.get("managementIpAddress") or "", host, host.split(".", 1)[0])
    name = name.lower()
    if any(c in name for c in "*?["):
        return any(fnmatch.fnmatchcase(c, name) for c in candidates)
    return name in candidates


def ask(prompt, hint, hidden=False):
    """Ask on the terminal, also when stdin is piped (e.g. --params-file -).

    Exits with hint (what to set or pass instead) when there is no terminal.
    """
    if sys.stdin.isatty():
        return getpass.getpass(prompt) if hidden else input(prompt)
    try:
        # Separate read and write handles: "r+" on a tty fails or returns
        # without waiting on macOS, as the buffered file expects to seek
        tty_in, tty_out = open("/dev/tty"), open("/dev/tty", "w")
    except OSError:
        sys.exit(f"No terminal to ask for '{prompt.strip()}', {hint}")
    with tty_in, tty_out:
        if hidden:
            return getpass.getpass(prompt)  # Reads from /dev/tty itself
        tty_out.write(prompt)
        tty_out.flush()
        return tty_in.readline().rstrip("\n")


def batch_sizes(value):
    """'1,2,4' -> [1, 2, 4]. Sizes must be 1 or more."""
    try:
        sizes = [int(v) for v in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("expected sizes like 1,2,4")
    if not sizes or min(sizes) < 1:
        raise argparse.ArgumentTypeError("batch sizes must be 1 or more")
    return sizes


def split_batches(items, sizes):
    """Split items into batches of the given sizes; the last size repeats."""
    batches, i = [], 0
    while i < len(items):
        size = sizes[min(len(batches), len(sizes) - 1)]
        batches.append(items[i:i + size])
        i += size
    return batches


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
    p.add_argument("--role", action="append", default=[],
                   help="Only devices with this role in CC, e.g. access, distribution, core, "
                        "'border router' (repeatable, combines with --family)")
    p.add_argument("--exclude-platform", action="append", default=[],
                   help="Skip devices whose model or series contains this text, e.g. 2960S "
                        "(repeatable)")
    p.add_argument("--exclude-device", action="append", default=[],
                   help="Skip this device, by hostname or management IP; wildcards * ? [...] "
                        "work, quote them, e.g. 'switch-*' (repeatable)")
    p.add_argument("--exclude-file",
                   help="Skip the devices in this file, one hostname, IP or wildcard per line "
                        "(# comments)")
    p.add_argument("--include-unreachable", action="store_true",
                   help="Also deploy to devices CC doesn't list as reachable")
    p.add_argument("--param", action="append", help="key=value for all devices (repeatable)")
    p.add_argument("--params-file",
                   help="JSON: {\"hostname or IP\": {\"key\": \"value\"}}, '-' reads it from stdin")
    p.add_argument("--force", action="store_true",
                   help="Force push, even to devices already on this template version")
    p.add_argument("--save", action=argparse.BooleanOptionalAction, default=True,
                   help="Write memory on devices CC deployed to (default on, like CC "
                        "provisioning)")
    p.add_argument("--save-wait", type=int, default=60,
                   help="Seconds to wait before saving when deploy completion can't be "
                        "confirmed (default 60)")
    p.add_argument("--batches", type=batch_sizes, metavar="N[,N...]",
                   help="Deploy in batches of these sizes, by hostname, asking before each "
                        "next batch; the last size repeats, e.g. 1,2,4 or 5")
    p.add_argument("--yes", action="store_true", help="Don't ask for confirmation")
    p.add_argument("--insecure", action="store_true", help="Skip TLS verification")
    p.add_argument("--timeout", type=int, default=600, help="Seconds to wait (default 600)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show targets and template parameters, don't deploy")
    p.add_argument("--debug", action="store_true",
                   help="Print the JSON sent to CC and CC's raw task output")
    args = p.parse_args()

    if not args.device and not args.site:
        p.error("give at least one --device or --site")

    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # Read params before connecting, so a JSON mistake fails fast
    common = parse_kv(args.param)
    per_device = load_params_file(args.params_file) if args.params_file else {}
    exclude_names = args.exclude_device + (read_exclude_file(args.exclude_file)
                                           if args.exclude_file else [])

    user = os.environ.get("CC_USER") or ask("Username: ", "set CC_USER")
    password = os.environ.get("CC_PASS") or ask("Password: ", "set CC_PASS", hidden=True)
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

    if args.role:
        # Exact match, so 'router' doesn't pick up 'BORDER ROUTER'
        wanted = {r.strip().upper() for r in args.role}
        devices = {k: d for k, d in devices.items() if (d.get("role") or "").upper() in wanted}

    if args.exclude_platform or exclude_names:
        unused = set(exclude_names) | set(args.exclude_platform)
        excluded = []
        for d in devices.values():
            model = f"{d.get('platformId') or ''} {d.get('series') or ''}".lower()
            platforms = [t for t in args.exclude_platform if t.lower() in model]
            names = [n for n in exclude_names if matches_name(d, n)]
            unused -= set(platforms) | set(names)
            if platforms or names:
                excluded.append((d, f"platform {platforms[0]}" if platforms else "by name"))
        if excluded:
            print(f"\nExcluding {len(excluded)} device(s):")
            print_table([[d.get("hostname"), d.get("managementIpAddress"), d.get("platformId"),
                          reason]
                         for d, reason in sorted(excluded,
                                                 key=lambda x: x[0].get("hostname") or "")],
                        ["Hostname", "Management IP", "Platform", "Reason"])
            gone = {d["id"] for d, _ in excluded}
            devices = {k: d for k, d in devices.items() if k not in gone}
        if unused:
            # Usually a typo, or a device that isn't in the target set anyway
            print(f"\nNote: these exclusions matched no target device: {', '.join(sorted(unused))}")

    if not args.include_unreachable:
        skipped = [d for d in devices.values() if d.get("reachabilityStatus") != "Reachable"]
        if skipped:
            print(f"\nSkipping {len(skipped)} device(s) not reachable in CC "
                  "(use --include-unreachable to include them):")
            print_table([[d.get("hostname"), d.get("managementIpAddress"),
                          d.get("reachabilityStatus")]
                         for d in sorted(skipped, key=lambda d: d.get("hostname") or "")],
                        ["Hostname", "Management IP", "Reachability"])
            devices = {k: d for k, d in devices.items() if d.get("reachabilityStatus") == "Reachable"}

    if not devices:
        sys.exit("No devices to deploy to")

    ordered = sorted(devices.values(), key=lambda d: d.get("hostname") or "")
    batches = split_batches(ordered, args.batches) if args.batches else [ordered]
    columns = ["Hostname", "Management IP", "Family", "Role"]
    rows = []
    for n, batch in enumerate(batches, 1):
        for d in batch:
            rows.append([d.get("hostname"), d.get("managementIpAddress"), d.get("family"),
                         d.get("role") or "UNKNOWN"] + ([str(n)] if len(batches) > 1 else []))
    if len(batches) > 1:
        columns.append("Batch")
        sizes = ", ".join(str(len(b)) for b in batches)
        print(f"\n{len(ordered)} target device(s) in {len(batches)} batches ({sizes}):")
    else:
        print(f"\n{len(ordered)} target device(s):")
    print_table(rows, columns, highlight=lambda h, v: h == "Role" and v.upper() == "UNKNOWN")

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

    print_params(targets, devices)

    by_id = {t["id"]: t for t in targets}
    if args.debug:
        print("\nDeploy request:")
        print(json.dumps(deploy_body(template_id, args.force, targets), indent=2))

    if args.dry_run:
        print("\nDry run, nothing deployed.")
        return

    if not args.yes:
        in_batches = f" in {len(batches)} batches" if len(batches) > 1 else ""
        answer = ask(f"\nDeploy '{args.template}' v{version} to {len(targets)} "
                     f"device(s){in_batches}? [y/N] ", "pass --yes to deploy without asking")
        if answer.strip().lower() != "y":
            sys.exit("Aborted")

    ask_between = not args.yes
    for n, batch in enumerate(batches, 1):
        if len(batches) > 1:
            names = ", ".join(d.get("hostname") or d["id"] for d in batch)
            print(f"\n=== Batch {n} of {len(batches)}: {names}")
        deployed = deploy(cc, args, template_id, version_id,
                          [by_id[d["id"]] for d in batch], devices)
        rest = [d for b in batches[n:] for d in b]
        if not rest:
            break
        if len(deployed) < len(batch):
            print(f"\nBatch {n} deployed to {len(deployed)} of {len(batch)} device(s).")
            if not ask_between:
                stop(rest, f"Stopping after batch {n}, as not every device in it was deployed")
        if ask_between:
            answer = ask(f"\nContinue with batch {n + 1} ({len(batches[n])} device(s))? "
                         "[y/N/a=all remaining] ", "pass --yes to run all batches without asking")
            answer = answer.strip().lower()
            if answer == "a":
                ask_between = False
            elif answer != "y":
                stop(rest, "Stopped")


def stop(rest, why):
    """End a batched deploy early, listing the devices that weren't deployed to."""
    print(f"\n{why}. Not deployed to {len(rest)} device(s):")
    print_table([[d.get("hostname"), d.get("managementIpAddress")] for d in rest],
                ["Hostname", "Management IP"])
    sys.exit(1)


def deploy_body(template_id, force, targets):
    return {
        "templateId": template_id,
        "forcePushTemplate": force,
        "isComposite": False,
        "targetInfo": targets,
    }


def deploy(cc, args, template_id, version_id, targets, devices):
    """Deploy to targets, wait for the result and save config. Returns deployed device IDs."""
    body = deploy_body(template_id, args.force, targets)
    resp = cc.post("/dna/intent/api/v2/template-programmer/template/deploy", body)
    task_id = resp["response"]["taskId"]
    print(f"Deploy task {task_id}, waiting...")

    task = wait_task(cc, task_id, args.timeout)
    progress = task.get("progress", "")
    if args.debug:
        print(f"\nTask progress: {progress}\nTask data: {task.get('data', '')}")
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

    confirmed = deployed is not None
    if not confirmed:
        deployed = report_progress(progress, devices)
        if deployed and args.save:
            # CC's task ends when the deploy is handed off, not when devices are done
            print(f"\nCan't confirm the deploy has finished, waiting {args.save_wait}s "
                  "before saving...")
            time.sleep(args.save_wait)

    if args.save:
        save_config(cc, deployed, devices, args.timeout)
        if deployed and not confirmed:
            print(f"\nWarning: the save ran after a fixed {args.save_wait}s wait, not a "
                  "confirmed deploy. If a device was still applying the template, part "
                  "of it may be unsaved. Check Deployment History in CC, and raise "
                  "--save-wait for slow devices.")
    return deployed


if __name__ == "__main__":
    main()
