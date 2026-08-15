## Secret safety

- Treat `.env`, `policy-prompt.txt`, `*.session`,
  process environments and container environments as confidential.
- Never read, print, diff, search, source, copy or include their contents
  in tool output.
- Never run full-output commands such as:
  - `docker compose config`
  - `docker compose config --environment`
  - `docker inspect`
  - `env`, `printenv`, `set`, `export -p`
  - shell tracing with `set -x`
- Do not attempt to redact secret-bearing output after generating it.
- For Compose validation, use only `scripts/compose-check-safe`.
- Before any command that may resolve an env file or expose secrets,
  stop and ask the user for approval.
- These restrictions also apply to subagents.
- If a secret is accidentally emitted, stop immediately, do not quote it,
  and recommend rotation.