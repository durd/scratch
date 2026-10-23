# Scratch

A small collection of network automation scripts for common operations on Cisco Catalyst Center and F5 devices.

This repository is a personal scratch space for utilities that are quick to use, focused on specific operational tasks, and designed to be run directly from a shell.

## Included scripts

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

### `cc_deploy_template.py`
Deploys a single Day-N template to Catalyst Center managed devices or site scopes.

Features:
- Target devices individually with `--device`
- Target all devices under a site with `--site`
- Optionally recurse below a site path with `--recursive`
- Filter by device family with `--family` and by device role (access, distribution, core, ...) with `--role`
- Apply per-device parameter overrides from JSON
- Supports dry-run and confirmation prompts
- Waits for task and deployment status completion
- Writes the deployed config to device memory by default, like CC provisioning (skip with `--no-save`)

Template notes:
- This script is intended for a template that is already committed in Catalyst Center.
- The template does not need any device series/models or network profiles attached to it to be deployed by this script.
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
```

Required environment variables:

```bash
export CC_USER="myuser"
export CC_PASS="mypassword"
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
- All scripts need Python 3.12+ and the packages in `requirements.txt`. They exit with a clear message on older Pythons.

## Requirements

Python 3.12 or newer (any patch release, e.g. Ubuntu 24.04's 3.12.3). Set up a virtual environment in `venv/` (ignored by git):

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Run the scripts with the venv's Python, e.g. `venv/bin/python f5_ucs_backup.py ...`,
or `source venv/bin/activate` first. On hosts where `python3` is older than 3.12
(RHEL 8's system Python is 3.6), create the venv with a newer interpreter,
e.g. `python3.12 -m venv venv`.

## License

No explicit license has been set for this repository.
