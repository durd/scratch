# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

Standalone Python scripts for network operations on Cisco Catalyst Center and F5
(TMOS / F5OS). Each script is a single self-contained file, run directly from a
shell by an operator. There is no package, no shared module and no test suite.
`README.md` documents each script for users. Keep it in sync when flags or
behavior change.

## Constraints

- **Python 3.12.13+ for every script.** Each one checks this at startup, so
  modern syntax and stdlib features are fine. Keep the scripts on the same
  minimum version.
- **Dependencies are in `requirements.txt`**: only `requests` and `urllib3`.
  Don't add others. Use the local venv (`python3 -m venv venv`,
  `venv/bin/pip install -r requirements.txt`) and run scripts with
  `venv/bin/python`.
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
