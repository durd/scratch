# Scratch

A small collection of network automation scripts for common operations on Cisco Catalyst Center and F5 devices.

This repository is a personal scratch space for utilities that are quick to use, focused on specific operational tasks, and designed to be run directly from a shell.

## Setup

All scripts need Python 3.12 or newer (any patch release, e.g. Ubuntu 24.04's 3.12.3)
and the packages in `requirements.txt`; they exit with a clear message on older
Pythons. Set up a virtual environment in `venv/` (ignored by git):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Run the scripts with the venv's Python, e.g. `venv/bin/python f5_ucs_backup.py ...`,
or `source venv/bin/activate` first. On hosts where `python3` is older than 3.12
(RHEL 8's system Python is 3.6), create the venv with a newer interpreter,
e.g. `python3.12 -m venv venv`.

## Included scripts

- [`cc_deploy_template.py`](#cc_deploy_templatepy): push a Day-N template to Catalyst Center devices or sites
- [`f5_ucs_backup.py`](#f5_ucs_backuppy): back up BIG-IP UCS archives and F5OS config databases
- [`catc_backup_cleanup.py`](#catc_backup_cleanuppy): delete old Catalyst Center backups

How each script gets its credentials is described in its section.

### `cc_deploy_template.py`
Deploys a single Day-N template to Catalyst Center managed devices or site scopes.

Tested against Catalyst Center 3.2.3. Other releases may return API responses
the script doesn't expect, so start with `--dry-run` (and `--debug`) there.

Features:
- Target devices individually with `--device`, or from a file with `--device-file`
- Target all devices under a site with `--site`
- Optionally recurse below a site path with `--recursive`
- Filter by device family with `--family`, device role (access, distribution, core, ...) with `--role`, and SNMP contact with `--contact`
- Exclude devices by model with `--exclude-platform` (e.g. `2960S`), SNMP contact with `--exclude-contact`, and name with `--exclude-device` or `--exclude-file`
- Apply per-device parameter overrides from JSON
- Shows the targets and the template parameters each device gets before deploying
- Staged rollouts with `--batches`, asking before each next batch
- Stops if the template isn't in a network profile assigned to the targets' sites (directly or through a composite)
- Supports dry-run and confirmation prompts; `--debug` also prints the JSON sent to CC and CC's raw task output
- Waits for task and deployment status completion
- Prints when it started, and on any exit when it finished and how long it took
- Writes the deployed config to device memory by default, like CC provisioning (skip with `--no-save`)
- `--save-only` writes memory on the targets without deploying, e.g. after a deploy whose save didn't run

Template notes:
- This script is intended for a template that is already committed in Catalyst Center.
- The template does not need any device series/models attached to it.
- It does need a network profile: see [Network profile check](#network-profile-check).

#### Network profile check

Before showing the targets, the script checks that the template belongs on their
sites, the way CC's own provisioning does:

1. It finds the network profiles the template is attached to, either directly or
   inside a composite template attached to the profile.
2. Every target device must be on a site where one of those profiles applies. A
   profile on an area applies to the buildings and floors below it, except where
   a site below has another profile of the same type assigned.

Each profile is shown with the top sites it covers. If a device isn't covered, the
script lists those devices with their site and stops before deploying. With
`--debug` it also prints CC's site list for each profile and which profile
covers each device. It also stops when the
template isn't in any profile, or when CC can't list a profile's templates
(2.3.7.9 and later can). `--dry-run` runs the check too.

`--only-covered` skips the devices on uncovered sites instead of stopping, and
deploys to the rest. They're listed with their site, as when it stops.

`--skip-profile-check` turns the check off, e.g. for a one-off template that
isn't in a profile.

When you use it on new sites or profiles, try it first with `--dry-run` on a site
you know is covered and one you know isn't.

#### Choosing devices

`--device-file FILE` adds the devices in a file to the `--device` targets: one
hostname or management IP per line, `#` comments allowed. Paths are cut to their
last part, so the output of `grep -l` works as is (`./core/sw01` becomes `sw01`):

```bash
grep -rlPz --exclude-dir=.git 'ntp server 10\.9\.9\.9' . > devices.txt
```

The file names must be the hostnames, without an extension like `.cfg`.

`--device` and `--device-file` entries that aren't in CC's inventory, or match
several devices, are listed with the reason and skipped, and the count is shown
again below the target table. `--strict` stops instead.

Filters only narrow the targets from `--device`, `--device-file` and `--site`.
Matching ignores case.

| Option | Matches |
| --- | --- |
| `--family switch` | text in the device family |
| `--role access` | the exact role; `UNKNOWN` roles are left out |
| `--contact noc` | text in the SNMP contact (keeps matches) |
| `--exclude-contact TEXT` | text in the SNMP contact (skips matches) |
| `--exclude-platform 2960S` | text in the model or series, e.g. `WS-C2960S-48FPS-L` |
| `--exclude-device NAME` | hostname (short or full) or management IP |
| `--exclude-file FILE` | the same, one per line, `#` comments allowed |

- Names match exactly (`sw1` isn't `sw10`). For more, use wildcards `*` `?` `[...]`,
  e.g. `'switch-*'` or `'10.1.1.*'`. Quote them on the command line.
- In contacts, a wildcard must match the whole contact: `'noc@*'` matches `noc@example.com`
  but not `NOC Team <noc@example.com>`. CC reads contacts at inventory sync, so recent
  changes on a device may not show yet. The target table shows the contact when used.
- Excluded devices are listed with the reason. Exclusions that match nothing are
  reported, as they're usually typos.

#### Staged rollouts

`--batches 1,2,4` deploys in batches of 1, 2, then 4 devices at a time (the last
size repeats), in hostname order.

- After each batch it asks `Continue? [y/N/a/s]`. `a` runs the rest, `N` stops and
  lists what's left, `s` skips the next batch and asks again about the one after.
  Skipped devices are listed at the end.
- With `--yes`, or after `a`, it doesn't ask, but stops if a batch didn't reach all its
  devices. Devices CC skips as already on this template version count as reached;
  they aren't saved again.
- Each batch is saved before the next one starts.

#### Saving without deploying

`--save-only` takes the same `--device`/`--device-file`/`--site` targets, filters and `--batches`
as a deploy, and only runs write memory on them. `--template` isn't needed, and
`--dry-run` shows the devices it would save. Write memory saves everything
in the running config, including changes not made by this script.

Use it when a deploy went through but the save didn't. Running the deploy
again won't save those devices: CC skips them as already on that template
version, and the script only saves devices it deployed to.

CC 3.2.3 cuts its deploy task text off at 1023 characters. That text is the
only place CC says which devices it deployed to and which it skipped as
already on that template version. So the script sends each batch to CC in
several small deploy requests, sized from the template name so CC's answer
fits (6 devices for a short name), and saves the batch once at the end. If an
answer is cut off anyway, its devices are counted as deployed and saved.

When CC deploys something, it also gives a deployment ID. The script then reads
the result per device: deployed, already on this version, or failed. It saves
the deployed ones as soon as CC says they're done, without the fixed
`--save-wait` pause. Without a deployment ID it goes by the task text. In a
request with both new and already-deployed devices, that text doesn't say which
were skipped, so all of them are saved. That's harmless, just an extra write
memory.

Batches of about 10 devices have worked well on 3.2.3. Batches of 30 devices
that already had the template were very slow or never finished.

Example:

```bash
./cc_deploy_template.py \
  --host cc.example.com \
  --template "Fix-NTP" \
  --device 10.1.1.10 \
  --device sw02 \
  --param ntp_server=10.0.0.1

./cc_deploy_template.py \
  --host cc.example.com \
  --template "Fix-NTP" \
  --site "Global/Area/Building" \
  --family switch \
  --role access \
  --dry-run

./cc_deploy_template.py \
  --host cc.example.com \
  --template "Fix-NTP" \
  --site "Global/Area/Building/Floor 1" \
  --exclude-platform 2960S \
  --exclude-file not-provisioned.txt \
  --dry-run

./cc_deploy_template.py \
  --host cc.example.com \
  --template "Fix-NTP" \
  --device-file devices.txt \
  --dry-run
```

Several template parameters: repeat `--param`. Each value is read as JSON when it
is valid JSON, otherwise kept as text:

```bash
./cc_deploy_template.py \
  --host cc.example.com \
  --template "Access-Baseline" \
  --site "Global/Area/Building" \
  --param ntp_server=10.0.0.1 \
  --param mgmt_vlan=10 \
  --param 'trunk_vlans=[10,20,30]' \
  --param 'banner=Authorized access only' \
  --dry-run
```

| `--param` | Template gets |
| --- | --- |
| `ntp_server=10.0.0.1` | text `10.0.0.1` (not valid JSON) |
| `mgmt_vlan=10` | number `10` |
| `'trunk_vlans=[10,20,30]'` | list `[10, 20, 30]` |
| `enabled=true` | boolean `true` |
| `site_code=007` | text `007` (leading zero isn't valid JSON) |
| `'mgmt_vlan="10"'` | text `10`, use JSON quotes to force a string |
| `'banner=Authorized access only'` | text, quote the whole argument for the shell |

Per-device values go in a JSON file keyed by hostname or management IP. They
override `--param` values with the same name:

```json
{
  "sw01.example.com": {"mgmt_vlan": 110, "trunk_vlans": [110, 120]},
  "10.1.1.11": {"mgmt_vlan": 210}
}
```

```bash
./cc_deploy_template.py \
  --host cc.example.com \
  --template "Access-Baseline" \
  --site "Global/Area/Building" \
  --param ntp_server=10.0.0.1 \
  --param mgmt_vlan=10 \
  --params-file params.json \
  --dry-run
```

Values in the file can be nested lists and dicts. They're passed to CC unchanged;
whether a template can loop over them depends on CC and the template language,
so try a nested value on one lab device first.

`--params-file -` reads the same JSON from stdin, e.g. generated by another tool.
The username and deploy confirmation are then asked on the terminal; without one
(cron, CI) set `CC_USER`/`CC_PASS` and pass `--yes`:

```bash
generate-params | ./cc_deploy_template.py \
  --host cc.example.com \
  --template "Access-Baseline" \
  --site "Global/Area/Building" \
  --params-file - \
  --dry-run
```

The dry run shows the parameters each device ends up with, so check it before
deploying.

Required environment variables:

```bash
export CC_USER="myuser"
export CC_PASS="mypassword"
```

---

### `f5_ucs_backup.py`
Backs up BIG-IP / TMOS UCS archives and F5OS config database snapshots using the device REST APIs.

Features:
- Supports both TMOS and F5OS devices
- Reads credentials from environment variables or prompts interactively
- Optionally encrypts TMOS UCS backups
- Keeps local and/or device-side backups based on retention settings
- Supports host lists from CLI arguments or a file
- Works with certificate verification override via `--insecure`

Example:

```bash
./f5_ucs_backup.py bigip01 f5os:rseries01 -o /backup/f5
./f5_ucs_backup.py -f hosts.txt -o /backup/f5 --encrypt -k 14 -d 1 --insecure
```

Required environment variables:

```bash
export F5_USER="myuser"
export F5_PASSWORD="mypassword"
export F5_UCS_PASSPHRASE="secret"   # only for --encrypt on TMOS
```

---

### `catc_backup_cleanup.py`
Deletes old Catalyst Center backup artifacts.

This script supports two modes:

1. `storage` mode
   - Runs directly against the NFS/SFTP backup share
   - Deletes backup-id folders older than a configured number of days
   - Useful when the physical appliance does not expose a delete API

2. `api` mode
   - Uses the Catalyst Center REST API to identify and delete old backups
   - Best for deployments where the backup API is available

Examples:

```bash
./catc_backup_cleanup.py storage \
  --backup-root /mnt/backup-share \
  --days 30 \
  --apply

./catc_backup_cleanup.py api \
  --host https://catc.example.com \
  --username admin \
  --days 30 \
  --apply
```

---

## Repository structure

```text
.
├── catc_backup_cleanup.py
├── cc_deploy_template.py
├── f5_ucs_backup.py
├── requirements.txt
├── AGENTS.md
└── README.md
```

## Notes

- These scripts are intentionally lightweight and operationally focused.
- They assume direct access to target systems and standard Linux/macOS shell execution.

## License

No explicit license has been set for this repository.
