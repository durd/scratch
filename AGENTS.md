# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

Standalone Python scripts for network operations on Cisco Catalyst Center and F5
(TMOS / F5OS). Each script is a single self-contained file, run directly from a
shell by an operator. There is no package, no shared module and no test suite.
`README.md` documents each script for users. Keep it in sync when flags or
behavior change.

## Constraints

- **Python version is per script.** `cc_deploy_template.py` needs Python
  3.12.13+ and checks this at startup. `f5_ucs_backup.py` and
  `catc_backup_cleanup.py` target Python 3.6 (RHEL 8 system Python), so avoid
  newer syntax and stdlib features there: no walrus (`:=`), no
  `argparse.BooleanOptionalAction`, no `match`, no `dict | dict`, no
  `str.removeprefix`, no built-in generics like `list[str]` in annotations.
- **Only dependency is `requests`** (plus `urllib3`, which ships with it). Don't
  add others. Operators install it with `dnf install python3-requests`.
- **One file per script.** Don't split a script into modules or share code
  between scripts. Some duplication is accepted so each file can be copied
  around on its own.
- **Credentials** come from environment variables (`CC_USER`/`CC_PASS`,
  `F5_USER`/`F5_PASSWORD`, ...) with an interactive prompt as fallback. Never
  hard-code them, log them, or add them to examples.

## Style

- Match the existing code: plain functions, short docstrings, `sys.exit("message")`
  for fatal errors, `print()` for progress (no logging framework).
- `argparse` with `RawDescriptionHelpFormatter` and usage examples in the module
  docstring.
- Comments explain *why* (API quirks, CC behavior), not what the code does.

## Safety

These scripts change production network devices and delete backups.

- Changes must be safe by default. Keep dry-run / confirmation behavior
  (`--dry-run`, `--yes`, `--apply`) working, and don't make destructive actions
  run without the operator asking.
- Never run a script against a real host to test a change. Verify with
  `python3 -m py_compile <script>` and `--help`, and say what you couldn't test.
- Catalyst Center API responses vary between releases. Handle missing fields
  defensively (`.get()`), and note in a comment which release a workaround is for
  if you know it.

## Commits

Small, focused commits with an imperative summary line, e.g.
"Make --save the default in cc_deploy_template". Update `README.md` in the same
commit when user-facing behavior changes.
