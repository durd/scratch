#!/usr/bin/env python3
r"""Push a single Day-N CLI template to devices via Catalyst Center.

Only the template's config is pushed (no full device provision).
Targets: individual devices (--device) and/or all devices on sites (--site).

Credentials: env vars CC_USER / CC_PASS, otherwise you are prompted.

Needs Python 3.12+ and `requests`. Tested against Catalyst Center 3.2.3.

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
import atexit
import fnmatch
import getpass
import ipaddress
import json
import os
import re
import sys
import time
from datetime import datetime

import requests
import urllib3

if sys.version_info < (3, 12):
    sys.exit(f"Needs Python 3.12 or newer, this is {sys.version.split()[0]}")

PROGRESS_MAX = 1023  # CC 3.2.3 cuts task progress text at this length
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DEVICE_PATH = "/dna/intent/api/v1/network-device"
TEMPLATE_PATH = "/dna/intent/api/v1/template-programmer/template"
PROFILE_PATH = "/dna/intent/api/v1/networkProfilesForSites"


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

    def get(self, path, none_on=(), **params):
        """GET path. A status in none_on (e.g. (404,)) returns None instead of exiting."""
        for _ in range(5):
            r = self.s.get(self.base + path, params=params, timeout=60)
            if r.status_code != 429:
                break
            wait = int(r.headers.get("Retry-After", 30))
            print(f"  Rate limited, waiting {wait}s...")
            time.sleep(wait)
        if r.status_code == 204:
            return {"response": []}
        if r.status_code in none_on:
            return None
        if not r.ok:
            sys.exit(f"GET {path} failed: {r.status_code} {r.text}")
        return r.json()

    def post(self, path, body):
        r = self.s.post(self.base + path, json=body, timeout=60)
        if not r.ok:
            sys.exit(f"POST {path} failed: {r.status_code} {r.text}")
        return r.json()


def find_template(cc, name, project):
    """Return (template_id, latest_version_id, version_number, project_name)."""
    params = {"projectNames": project} if project else {}
    templates = cc.get(TEMPLATE_PATH, **params)
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
    return t["templateId"], latest["id"], latest.get("version"), t.get("projectName")


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


def get_all(cc, path, none_on=()):
    """Every item of a paged (offset/limit) list. None on a status in none_on."""
    items, offset, limit = [], 1, 500
    while True:
        page = cc.get(path, none_on=none_on, offset=offset, limit=limit)
        if page is None:
            return None
        page = page.get("response") or []
        items += page
        if len(page) < limit:
            return items
        offset += limit


def sub_sites(cc, path):
    """IDs of all sites below path (areas, buildings, floors), not including path itself."""
    prefix = path + "/"
    return [s["id"] for s in get_all(cc, "/dna/intent/api/v1/sites")
            if (s.get("nameHierarchy") or "").startswith(prefix)]


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
    """{device ID: site ID} on the site, and with recursive also on every site below it."""
    site_ids = [site_id]
    if recursive:
        site_ids += sub_sites(cc, path)
        print(f"  Checking {len(site_ids)} site(s) under {path}...")
    found = {}
    for sid in site_ids:
        for i in direct_device_ids(cc, sid):
            found.setdefault(i, sid)
    return found


def template_profiles(cc, template_id, name, project):
    """Network profiles the template is attached to, directly or inside a composite.

    Returns {profile ID: (profile name, composite name or None)}, or None when
    this CC can't list a profile's templates (that endpoint is 2.3.7.9 and later).
    """
    profiles = get_all(cc, PROFILE_PATH, none_on=(404,))
    if profiles is None:
        return None
    # The full list, as a composite can live in another project than its members
    listing = cc.get(TEMPLATE_PATH)
    if isinstance(listing, dict):
        listing = listing.get("response", [])
    flagged = any("composite" in t for t in listing)
    composite_ids = {t.get("templateId") for t in listing if t.get("composite")}

    members = {}  # Composite ID -> (name, member records), fetched once

    def composite(tid):
        if tid not in members:
            t = cc.get(f"{TEMPLATE_PATH}/{tid}", none_on=(404,)) or {}
            t = t.get("response", t)  # Some releases wrap the record, some don't
            members[tid] = (t.get("name") or tid,
                            t.get("containingTemplates") or [])
        return members[tid]

    found, answered = {}, False
    for prof in profiles:
        # Any 4xx: some profile types (Assurance, Telemetry, ...) may not take
        # templates. Skipping one can only make the check stricter.
        attached = cc.get(f"{PROFILE_PATH}/{prof.get('id')}/templates",
                          none_on=range(400, 500))
        if attached is None:
            continue
        answered = True
        for t in attached.get("response") or []:
            tid = t.get("id")
            if tid == template_id:
                found[prof["id"]] = (prof.get("name") or prof["id"], None)
                break
            if flagged and tid not in composite_ids:
                continue
            comp_name, contained = composite(tid)
            # Members point at the template ID; name + project also catches older references
            if any(c.get("id") == template_id
                   or (c.get("name"), c.get("projectName")) == (name, project)
                   for c in contained):
                found[prof["id"]] = (prof.get("name") or prof["id"], comp_name)
                break
    return found if answered or not profiles else None


def check_profiles(cc, template_id, name, project, devices, device_site, debug=False,
                   only_covered=False):
    """Check every device sits on a site covered by a profile the template is in.

    Exits if one doesn't, or with only_covered returns the IDs of those that
    don't, to be skipped. device_site has the site ID for devices found through
    --site; the others are looked up.
    """
    print("\nChecking the template's network profiles...")
    how = "skip the check with --skip-profile-check"
    profiles = template_profiles(cc, template_id, name, project)
    if profiles is None:
        sys.exit(f"Can't check network profiles: this Catalyst Center doesn't list the "
                 f"templates of a profile (needs 2.3.7.9 or later); {how}")
    if not profiles:
        sys.exit(f"Template '{name}' isn't attached to any network profile, directly or "
                 f"in a composite; {how}")
    paths = {s.get("id"): s.get("nameHierarchy") or ""
             for s in get_all(cc, "/dna/intent/api/v1/sites")}
    allowed = {}  # Site path -> profile name
    for pid, (pname, via) in profiles.items():
        assigned = get_all(cc, f"{PROFILE_PATH}/{pid}/siteAssignments", none_on=(404,)) or []
        if debug:
            print(f"\n  siteAssignments of {pname}:\n{json.dumps(assigned, indent=2)}")
        sites = sorted({paths[a["id"]] for a in assigned if paths.get(a.get("id"))})
        for site in sites:
            allowed.setdefault(site, pname)
        # Only the top sites; the ones below them are covered anyway
        top = [x for x in sites if not any(x.startswith(y + "/") for y in sites)]
        print(f"  Profile {pname}" + (f" (in composite {via})" if via else "")
              + f": {', '.join(top) or 'no sites'} ({len(sites)} site(s))")

    outside = []
    for d in devices.values():
        sid = device_site.get(d["id"])
        if sid:
            site = paths.get(sid, "")
        else:
            r = cc.get(f"/dna/intent/api/v1/networkDevices/{d['id']}/assignedToSite",
                       none_on=(404,)) or {}
            r = r.get("response") or {}
            site = r.get("siteNameHierarchy") or paths.get(r.get("siteId"), "")
        # The device's own site only, not the sites above it: siteAssignments
        # already lists the sites that inherit the profile, and leaves out sites
        # below that have another profile of their own (seen on 3.2.3)
        if debug:
            print(f"  {d.get('hostname')}: {site or '(no site)'} -> "
                  + (allowed[site] if site in allowed else "not covered"))
        if site not in allowed:
            outside.append((d, site))
    if outside:
        print(f"\n{len(outside)} device(s) are on sites none of these profiles is assigned to:")
        print_table([[d.get("hostname"), d.get("managementIpAddress"), site or "(no site)"]
                     for d, site in sorted(outside, key=lambda x: x[0].get("hostname") or "")],
                    ["Hostname", "Management IP", "Site"])
        if not allowed:
            print("\nThe profiles aren't assigned to any site.")
        if only_covered:
            print(f"Skipping them (--only-covered), {len(devices) - len(outside)} device(s) "
                  "left.")
            return {d["id"] for d, _ in outside}
        sys.exit(f"Template '{name}' doesn't belong on those sites; skip those devices "
                 f"with --only-covered, or {how}")
    print(f"  All {len(devices)} device(s) are on sites these profiles are assigned to")
    return set()


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
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

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


def matches_contact(d, text):
    """Text anywhere in the SNMP contact, or with * ? [...] a wildcard match of all of it."""
    contact = (d.get("snmpContact") or "").lower()
    text = text.lower()
    if any(c in text for c in "*?["):
        return fnmatch.fnmatchcase(contact, text)
    return text in contact


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


def report_progress(progress, devices, target_ids):
    """Print CC's applicable / non-applicable targets as readable lists.

    Returns (deployed IDs, IDs CC skipped as already on this template version).

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

    # An open [ at the end: CC cut the text off inside the list
    m = re.search(r"(?<![a-z])ApplicableTargets:\s*(\[[^\]]*\]|\{[^}]*\}|None|\[[^\]]*$)",
                  progress)
    applicable = UUID_RE.findall(m.group(1)) if m else []
    cut = bool(m) and m.group(1).startswith("[") and not m.group(1).endswith("]")

    # Messages like "Template x:14 is already deployed with same params, Hence not deploying"
    notes = list(dict.fromkeys(
        n.strip(" .,") for n in re.findall(r"([^.,\]]*?), Hence not deploying on devices:", progress)))

    if not skipped and not m:
        print(f"Task done: {progress}")
        return [], []

    # Devices named after "Hence not deploying on devices:" were skipped too
    # (2.3.7.11 names them, 3.2.3 doesn't)
    named = []
    for part in re.findall(r"Hence not deploying on devices:\s*(\[[^\]]*\]|[0-9a-f\-, ]*)", progress):
        for uuid in UUID_RE.findall(part):
            skipped.setdefault(uuid, "not deployed")
            named.append(uuid)
    current = named if "already deployed" in progress else []

    if cut:
        # CC 3.2.3 cuts the task text at about 1000 characters, so a batch of
        # more than ~24 devices ends mid-list. Non-applicable devices are named
        # before the list, so the rest of the batch passed the check too.
        rest = [u for u in target_ids if u not in applicable and u not in skipped]
        print(f"\nCC's task text was cut off after {len(applicable)} device(s). CC named "
              f"{len(skipped) or 'no'} device(s) as not applicable before that, so the other "
              f"{len(rest)} in this batch are counted as deployed too.")
        applicable += rest

    print(f"\nPassed CC's target check, {len(applicable)} device(s):")
    for uuid in applicable:
        print(f"  {label(uuid)}")
    if skipped:
        print(f"\nNot applicable, {len(skipped)} device(s):")
        for key, reason in skipped.items():
            print(f"  {label(key)}: {reason}")
    # 3.2.3 repeats the note once per skipped device, without naming the device
    for note in notes:
        count = re.findall(re.escape(note) + r", Hence not deploying on devices:", progress)
        if len(count) > 1 or not UUID_RE.search(progress.split(note, 1)[1]):
            ids = [u for u in target_ids if u in applicable]
            which = ("all of them" if len(count) >= len(ids)
                     else f"CC doesn't say which, so all {len(ids)} are counted as deployed")
            print(f"\nCC: {note}, on {len(count)} of {len(ids)} device(s) ({which})")
        else:
            print(f"\nCC: {note}")

    # 3.2.3 leaves the device list empty ("...on devices:.") and ends with
    # "Template has no clis to push hence not deploying template", in lower case
    if "hence not deploying" in progress.rsplit(":", 1)[-1].lower() or (
            progress.rstrip().lower().endswith("hence not deploying")):
        hint = " Use --force to push again." if "already deployed" in progress else ""
        print(f"\nResult: nothing was deployed.{hint}")
        # 3.2.3: one "already deployed" per device, so all of them if there are enough
        rest = [u for u in applicable if u not in skipped]
        if not current and len(re.findall(r"already deployed with same params", progress)) >= len(rest):
            current = rest
        return [], current
    else:
        print("\nResult: deploy started for the devices above. Check Deployment History in CC.")
        return [u for u in applicable if u not in skipped], current


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
    """Wait for a CC task to end and return its details (progress, data)."""
    # /tasks/{id} replaces /task/{id}, which is sunset since 2.3.7.6
    end = time.time() + timeout
    while time.time() < end:
        task = cc.get(f"/dna/intent/api/v1/tasks/{task_id}").get("response") or {}
        if task.get("status") in ("SUCCESS", "FAILURE") or task.get("endTime"):
            detail = cc.get(f"/dna/intent/api/v1/tasks/{task_id}/detail").get("response") or {}
            if task.get("status") == "FAILURE":
                sys.exit(f"Task failed: {detail.get('failureReason') or detail.get('progress')}")
            return detail
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
        # An empty endTime: still running (3.2.3 sets it when the devices are done)
        if (status.get("status") not in ("INIT", "IN_PROGRESS", None)
                and status.get("endTime", "-") != ""):
            return status
        time.sleep(5)
    sys.exit("Timed out waiting for deployment")


def stamp(t):
    """Local date and time with its time zone, e.g. 2026-11-04 14:05:12 CET."""
    return datetime.fromtimestamp(t).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def finished(started):
    """Print when the run ended and how long it took."""
    h, rest = divmod(int(time.time() - started), 3600)
    m, s = divmod(rest, 60)
    took = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
    print(f"\nFinished {stamp(time.time())}, took {took}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="Catalyst Center hostname/IP")
    p.add_argument("--template", help="Template name (not needed with --save-only)")
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
    p.add_argument("--contact", action="append", default=[],
                   help="Only devices whose SNMP contact contains this text; wildcards "
                        "* ? [...] match the whole contact (repeatable)")
    p.add_argument("--exclude-contact", action="append", default=[],
                   help="Skip devices whose SNMP contact contains this text, or matches a "
                        "wildcard (repeatable)")
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
    p.add_argument("--skip-profile-check", action="store_true",
                   help="Deploy even to devices on sites the template's network profiles "
                        "(directly or via a composite) aren't assigned to")
    p.add_argument("--only-covered", action="store_true",
                   help="Skip devices on sites the template's network profiles aren't "
                        "assigned to, instead of stopping")
    p.add_argument("--force", action="store_true",
                   help="Force push, even to devices already on this template version")
    p.add_argument("--save", action=argparse.BooleanOptionalAction, default=True,
                   help="Write memory on devices CC deployed to (default on, like CC "
                        "provisioning)")
    p.add_argument("--save-only", action="store_true",
                   help="Don't deploy, only write memory on the target devices, e.g. after a "
                        "deploy whose save didn't run")
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
    if not args.template and not args.save_only:
        p.error("--template is required, unless --save-only")
    if args.save_only and not args.save:
        p.error("--save-only and --no-save don't go together")
    if args.only_covered and args.skip_profile_check:
        p.error("--only-covered and --skip-profile-check don't go together")

    started = time.time()
    print(f"Started {stamp(started)}")
    # Also on sys.exit() and Ctrl-C, so a stopped run shows when and how long too
    atexit.register(finished, started)

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

    if not args.save_only:
        template_id, version_id, version, project = find_template(cc, args.template,
                                                                  args.project)
        print(f"Template '{args.template}' version {version}")

    # Collect target devices, keyed by UUID so duplicates are dropped
    devices = {}
    device_site = {}  # Device ID -> site ID, for devices found through --site
    for name in args.device:
        d = resolve_device(cc, name)
        devices[d["id"]] = d
    for path in args.site:
        site_id, site_type = find_site(cc, path)
        ids = site_device_ids(cc, site_id, path, args.recursive)
        for i, sid in ids.items():
            device_site.setdefault(i, sid)
        scope = "incl. sub-sites" if args.recursive else "directly assigned"
        print(f"Site {path} ({site_type}): {len(ids)} devices {scope}")
        if not ids and not args.recursive:
            print("  Note: no devices directly on this site. If they sit on floors or "
                  "buildings below it, add --recursive.")
        for d in device_records(cc, list(ids)):
            devices[d["id"]] = d

    if args.family:
        wanted = [f.lower() for f in args.family]
        devices = {k: d for k, d in devices.items()
                   if any(w in (d.get("family") or "").lower() for w in wanted)}

    if args.role:
        # Exact match, so 'router' doesn't pick up 'BORDER ROUTER'
        wanted = {r.strip().upper() for r in args.role}
        devices = {k: d for k, d in devices.items() if (d.get("role") or "").upper() in wanted}

    if args.contact:
        # Contact as CC last read it from the device, at the latest inventory sync
        devices = {k: d for k, d in devices.items()
                   if any(matches_contact(d, t) for t in args.contact)}

    if args.exclude_platform or exclude_names or args.exclude_contact:
        unused = set(exclude_names) | set(args.exclude_platform) | set(args.exclude_contact)
        excluded = []
        for d in devices.values():
            model = f"{d.get('platformId') or ''} {d.get('series') or ''}".lower()
            platforms = [t for t in args.exclude_platform if t.lower() in model]
            names = [n for n in exclude_names if matches_name(d, n)]
            contacts = [t for t in args.exclude_contact if matches_contact(d, t)]
            unused -= set(platforms) | set(names) | set(contacts)
            if platforms:
                excluded.append((d, f"platform {platforms[0]}"))
            elif names:
                excluded.append((d, "by name"))
            elif contacts:
                excluded.append((d, f"contact {contacts[0]}"))
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

    if args.save_only:
        pass  # Nothing is deployed, so there's no template to check
    elif args.skip_profile_check:
        print("\nSkipping the network profile check (--skip-profile-check).")
    else:
        uncovered = check_profiles(cc, template_id, args.template, project, devices,
                                   device_site, args.debug, args.only_covered)
        devices = {k: d for k, d in devices.items() if k not in uncovered}
        if not devices:
            sys.exit("No devices to deploy to")

    ordered = sorted(devices.values(), key=lambda d: d.get("hostname") or "")
    batches = split_batches(ordered, args.batches) if args.batches else [ordered]
    show_contact = bool(args.contact or args.exclude_contact)
    columns = (["Hostname", "Management IP", "Family", "Role"]
               + (["Contact"] if show_contact else []))
    rows = []
    for n, batch in enumerate(batches, 1):
        for d in batch:
            rows.append([d.get("hostname"), d.get("managementIpAddress"), d.get("family"),
                         d.get("role") or "UNKNOWN"]
                        + ([d.get("snmpContact") or ""] if show_contact else [])
                        + ([str(n)] if len(batches) > 1 else []))
    if len(batches) > 1:
        columns.append("Batch")
        sizes = ", ".join(str(len(b)) for b in batches)
        print(f"\n{len(ordered)} target device(s) in {len(batches)} batches ({sizes}):")
    else:
        print(f"\n{len(ordered)} target device(s):")
    print_table(rows, columns, highlight=lambda h, v: h == "Role" and v.upper() == "UNKNOWN")

    if args.save_only:
        word = "saved"
        question = f"Save config (write memory) on {len(ordered)} device(s)"

        def run(batch):
            ids = [d["id"] for d in batch]
            save_config(cc, ids, devices, args.timeout)
            return ids
    else:
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
        word = "deployed"
        question = f"Deploy '{args.template}' v{version} to {len(targets)} device(s)"

        def run(batch):
            return deploy(cc, args, template_id, version_id, version,
                          [by_id[d["id"]] for d in batch], devices)

    if args.dry_run:
        print(f"\nDry run, nothing {word}.")
        return

    if not args.yes:
        in_batches = f" in {len(batches)} batches" if len(batches) > 1 else ""
        answer = ask(f"\n{question}{in_batches}? [y/N] ", "pass --yes to run without asking")
        if answer.strip().lower() != "y":
            sys.exit("Aborted")

    ask_between = not args.yes
    skipped, skip = [], False
    for n, batch in enumerate(batches, 1):
        names = ", ".join(d.get("hostname") or d["id"] for d in batch)
        if skip:
            print(f"\n=== Batch {n} of {len(batches)}: skipped ({names})")
            skipped += batch
        else:
            if len(batches) > 1:
                print(f"\n=== Batch {n} of {len(batches)}: {names}")
            done = run(batch)
        rest = [d for b in batches[n:] for d in b]
        if not rest:
            break
        if not skip and len(done) < len(batch):
            print(f"\nBatch {n} {word} on {len(done)} of {len(batch)} device(s).")
            if not ask_between:
                stop(skipped + rest,
                     f"Stopping after batch {n}, as not every device in it was {word}", word)
        skip = False
        if ask_between:
            answer = ask(f"\nContinue with batch {n + 1} ({len(batches[n])} device(s))? "
                         "[y/N/a=all remaining/s=skip it] ",
                         "pass --yes to run all batches without asking")
            answer = answer.strip().lower()
            if answer == "a":
                ask_between = False
            elif answer == "s":
                skip = True
            elif answer != "y":
                stop(skipped + rest, "Stopped", word)
    if skipped:
        print(f"\nSkipped, not {word} on {len(skipped)} device(s):")
        print_table([[d.get("hostname"), d.get("managementIpAddress")] for d in skipped],
                    ["Hostname", "Management IP"])


def stop(rest, why, word="deployed"):
    """End a batched run early, listing the devices it didn't get to."""
    print(f"\n{why}. Not {word} on {len(rest)} device(s):")
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


def request_size(name, version):
    """Devices per deploy request, so CC's whole reply fits in its task progress text.

    CC 3.2.3 cuts the progress at 1023 characters. Per device it can hold the
    device ID (38) and, if the device already has this version, 81 characters
    plus "<name>:<version>". Around them are about 141 characters. Rounded up
    for some margin.
    """
    return max(1, (PROGRESS_MAX - 150) // (120 + len(f"{name}:{version}")))


def deploy(cc, args, template_id, version_id, version, targets, devices):
    """Deploy to targets, wait for the result and save config.

    Returns the IDs of devices deployed to or already on this version (not saved again).

    Sends the targets in several small deploy requests, as CC 3.2.3 cuts off the
    reply of a large one, and then saves once.
    """
    size = request_size(args.template, version)
    parts = [targets[i:i + size] for i in range(0, len(targets), size)]
    deployed, current, confirmed = [], [], True
    for n, part in enumerate(parts, 1):
        if len(parts) > 1:
            print(f"\n--- Deploy request {n} of {len(parts)} ({len(part)} device(s))")
        done, already, ok = deploy_request(cc, args, template_id, version_id, part, devices)
        deployed += done
        current += already
        confirmed = confirmed and ok
    if current:
        print(f"\nDeployed on {len(deployed)}, already on version {version}: {len(current)}, "
              f"of {len(targets)} device(s).")

    if not confirmed and deployed and args.save:
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
    return deployed + current


def deploy_request(cc, args, template_id, version_id, targets, devices):
    """One deploy request.

    Returns (deployed IDs, IDs already on this version, whether CC confirmed the deploy).
    """
    body = deploy_body(template_id, args.force, targets)
    resp = cc.post("/dna/intent/api/v2/template-programmer/template/deploy", body)
    task_id = resp["response"]["taskId"]
    print(f"Deploy task {task_id}, waiting...")

    task = wait_task(cc, task_id, args.timeout)
    progress = task.get("progress", "")
    if args.debug:
        print(f"\nTask progress: {progress}\nTask data: {task.get('data', '')}")
    # Look for a deployment ID outside the target lists, ignoring known template/device IDs
    text = re.sub(r"\[[^\]]*(\]|$)|\{[^}]*\}", "", f"{progress} {task.get('data', '')}")
    known = {template_id, version_id, *devices}
    candidates = [u for u in UUID_RE.findall(text) if u not in known]
    # 3.2.3 labels it when something was deployed: "Template Deployemnt Id: <uuid>"
    labelled = re.search(r"Deploy\w* Id:\s*(" + UUID_RE.pattern + ")", progress, re.I)
    if labelled:
        candidates.insert(0, labelled.group(1))
    if candidates:
        status = wait_deployment(cc, candidates[0], args.timeout)
        if status is not None:
            print(f"Deployment status: {status.get('status')}")
            deployed, already = [], []
            for dev in status.get("devices", []):
                uuid = dev.get("deviceId")
                # 3.2.3 leaves name and IP empty for devices it skipped
                name = dev.get("name") or devices.get(uuid, {}).get("hostname") or uuid
                print(f"  {name}: {dev.get('status')} {dev.get('detailedStatusMessage') or ''}")
                if dev.get("status") != "SUCCESS" or not uuid:
                    continue
                # Skipped devices are SUCCESS too, with "Template already deployed"
                if "already deployed" in (dev.get("detailedStatusMessage") or "").lower():
                    already.append(uuid)
                else:
                    deployed.append(uuid)
            return deployed, already, True
    ids = [t["id"] for t in targets]
    if len(progress) >= PROGRESS_MAX - 3:
        # Cut off despite request_size, e.g. other wording: don't trust the end
        print(f"\nCC's reply was cut off, so all {len(ids)} device(s) of this request are "
              "counted as deployed.")
        report_progress(progress, devices, ids)
        return ids, [], False
    done, already = report_progress(progress, devices, ids)
    return done, already, False


if __name__ == "__main__":
    main()
