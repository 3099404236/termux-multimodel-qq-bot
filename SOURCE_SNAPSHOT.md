# Source snapshot provenance

## Authority

The source of truth for this release was the code running on the Redmi phone on 2026-08-01. It was exported read-only through Android Debug Bridge using `run-as com.termux`; no production source file, process, service or configuration was changed or restarted.

## Included source

- `/root/AstrBot` from the Termux proot Ubuntu rootfs;
- `/root/antigravity-bridge` bridge source and launch scripts;
- the Termux-side ChatGPT web bridge source;
- the latency probe plugin;
- Termux stack launch, monitoring and NapCat watchdog scripts.

## Excluded state

- AstrBot `data/`, logs, PID files and databases;
- Antigravity work directories, transcripts, session state, dependencies and backups;
- ChatGPT browser data, cookies, history, cache, login state and `node_modules`;
- NapCat configuration, QR images, account state and logs;
- all OAuth codes, API keys and device-specific credentials.

## Publication-only normalization

- Python files were formatted with Ruff and lint-cleaned without intentional behavior changes.
- The latest running bridge was synchronized into the two compatibility locations that still held older copies in the AstrBot source tree.
- Device-specific QQ identifiers in watchdog/test fixtures were replaced with the local-only `NAPCAT_UIN` environment variable or an explicit test placeholder.
- Public documentation, report sources, CI metadata and deployment examples were added.

The public repository is therefore a sanitized, reproducible derivative of the phone snapshot, not a copy of the phone's runtime state.
