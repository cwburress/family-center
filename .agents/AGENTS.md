# Project Rules

## 1. PII Scrubbing and Alerting
- **Scrub PII Before Commit/Push**: It is imperative that all Personally Identifiable Information (PII) — including personal names, email addresses, phone numbers, exact location coordinates/addresses, private API keys/tokens, credentials, and personal calendar IDs — is thoroughly scrubbed or sanitized before any code, configuration, or assets are staged, committed, or pushed to GitHub.
- **Proactive Alerting**: Before staging or pushing any changes to GitHub, inspect all modified or new files for any potential PII or sensitive data and immediately alert the user if any PII is detected so it can be safely anonymized or moved to `.env`.

## 2. Feature Branch & Git Collaboration
- Always build new features on dedicated Git feature branches (`feature/<feature-name>`).
- NEVER merge feature branches into `main` automatically — always wait for explicit user confirmation.
- Deployment and server verification must happen BEFORE committing/pushing to the `main` branch.

## 3. Production Database Safety Safeguards
- NEVER overwrite, upload, or SCP local database files (`*.db`, `*.sqlite`) to a production server.
- Treat production databases on remote servers as the single source of truth for live data.

## 4. Server Process Isolation & Multi-Service Safety
- NEVER run global process termination commands (such as `pkill -9 -f 'gunicorn'`).
- Always target specific ports or process flags (e.g., `pkill -f 'gunicorn.*5004'`) so other services on the same host machine (e.g. NOC Dashboard) are never disrupted.

## 5. Deployment & Git Commit Order Rule
- **Deployment Order**: ALWAYS deploy and sync files to the production server and test/verify BEFORE committing or pushing to GitHub (`origin/main`). Pushing to GitHub must ALWAYS be the final step after server deployment is complete.
