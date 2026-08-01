# Security and privacy

## Public repository boundary

This repository contains source code and documentation only. It must never contain:

- QQ numbers, group IDs, cookies, QR codes or browser profiles;
- Google, OpenAI, Exa or other API/OAuth credentials;
- Antigravity/Gemini session databases or transcripts;
- AstrBot runtime configuration, group chat history, memory databases or logs;
- NapCat configuration files, login state or generated device identifiers.

The 2026-08-01 phone snapshot was exported with all runtime directories excluded and then scanned for secret-like values. Device-specific QQ identifiers found in two watchdog scripts were replaced with the required `NAPCAT_UIN` environment variable.

## Network exposure

The bridges and CDP endpoint bind to `127.0.0.1` by default. Keep them local. For debugging, prefer `adb forward` or an authenticated private tunnel. Never expose an unauthenticated OpenAI-compatible bridge or Chrome debugging port to the public Internet.

## Account and service risk

The ChatGPT web bridge automates an already logged-in browser and is not an official OpenAI API client. Page changes can break it, and automated use may conflict with service terms or account policies. Use official APIs for production or sensitive workloads. The same principle applies to all externally authenticated tools in this stack.

## Reporting

If a committed credential or private chat record is discovered, remove it from the public repository history, rotate the credential, invalidate affected sessions, and publish a security note describing the scope without repeating the secret.
