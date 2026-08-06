# Project Rules

## 1. PII Scrubbing and Alerting
- **Scrub PII Before Commit/Push**: It is imperative that all Personally Identifiable Information (PII) — including personal names, email addresses, phone numbers, exact location coordinates/addresses, private API keys/tokens, credentials, and personal calendar IDs — is thoroughly scrubbed or sanitized before any code, configuration, or assets are staged, committed, or pushed to GitHub.
- **Proactive Alerting**: Before staging or pushing any changes to GitHub, inspect all modified or new files for any potential PII or sensitive data and immediately alert the user if any PII is detected so it can be safely anonymized or moved to `.env`.
