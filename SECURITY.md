# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately. Do not open a public issue containing
credentials, complete configurations, private paths, or full transaction
journals.

Send a private vulnerability report through GitHub's private vulnerability
reporting feature on the [Jia-Ethan/pi-keysmith](https://github.com/Jia-Ethan/pi-keysmith)
repository, or contact the maintainer through a private channel listed in the
maintainer's profile.

Include:

- the affected version (`pi-keysmith.py --version`) and commit, if available;
- the platform and Python version;
- a minimal reproduction that does not expose credentials or private paths;
- the observed impact and any affected files.

## Scope

This tool modifies a single file (`SYSTEM.md`) in a pi agent configuration
directory and writes its own manifest, transaction journals, and backups. It
does not patch pi binaries, intercept network traffic, or store credentials.

A confirmed deployment changes the global pi system prompt for every new pi
session that loads that configuration directory; it is not a per-project
sandbox. Review the bundled prompt with `--dry-run` before using `--yes`, or
deploy your own `--file`.

## Supported versions

Only the latest signed Release is supported. Candidate builds and the
floating `main` branch are development state and are not formally supported.
