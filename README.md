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

Tested against Catalyst Center 2.3.7.11. Other releases may return API responses
the script doesn't expect, so start with `--dry-run` (and `--debug`) there.

Features:
- Target devices individually with `--device`
- Target all devices under a site with `--site`
- Optionally recurse below a site path with `--recursive`
- Filter by device family with `--family` and by device role (access, distribution, core, ...) with `--role`
- Exclude devices by model with `--exclude-platform` (e.g. `2960S`) and by name with `--exclude-device` or `--exclude-file`
- Apply per-device parameter overrides from JSON
- Shows the targets and the template parameters each device gets before deploying
- Staged rollouts with `--limit N`: deploy to the first N targets only
- Supports dry-run and confirmation prompts; `--debug` also prints the JSON sent to CC and CC's raw task output
- Waits for task and deployment status completion
- Writes the deployed config to device memory by default, like CC provisioning (skip with `--no-save`)

Template notes:
- This script is intended for a template that is already committed in Catalyst Center.
- The template does not need any device series/models or network profiles attached to it to be deployed by this script.
- `--exclude-platform` matches text in the device's model or series, case-insensitive; `2960S` catches `WS-C2960S-48FPS-L`. `--exclude-device` and `--exclude-file` take hostnames (short or full) or management IPs; the file has one per line and `#` comments. A plain name must match exactly (`sw1` doesn't match `sw10`). Wildcards `*`, `?` and `[...]` match against the full hostname, short hostname and IP, e.g. `'switch-*'` or `'10.1.1.*'`. Quote them on the command line, or zsh stops with "no matches found"; in the file they need no quotes. Excluded devices are listed with the reason, and exclusions that matched no target are reported, as they're usually typos.
- `--limit N` deploys to the first N targets, sorted by hostname, after all filters and exclusions. The order is the same every run, so a staged rollout is the same command with a growing N (`--limit 1`, `2`, `4`, ...). N counts devices from earlier runs too: CC skips those as already deployed with the same parameters, unless you add `--force`. The target table marks which devices are in this run.
- Device family and role filtering are optional and only narrow the target set. Role is matched exactly as CC shows it (case-insensitive), and devices with role `UNKNOWN` are left out when `--role` is given.

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
