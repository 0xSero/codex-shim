---
name: Bug report
about: Report a problem with the shim, ChatGPT passthrough, or the Desktop patch
labels: bug
---

## What happened

<!-- Short description. What did you run, what did you expect, what did you get? -->

## Environment

- Codex Desktop / CLI version: `codex --version` ->
- OS: macOS arm64 / x86_64 / Linux distro / WSL ->
- Python version: `python3 --version` ->
- codex-shim commit: `git -C <path-to-codex-shim> rev-parse --short HEAD` ->

## Repro

```bash
# Exact commands that reproduce the issue.
```

## Output

```text
# Output of: codex-shim status
```

```text
# Last ~80 lines of .codex-shim/shim.log (redact API keys / auth tokens).
```

## Route

- [ ] Configured BYOK/upstream model (slug: `____`)
- [ ] `gpt-5.5` ChatGPT passthrough
- [ ] Codex Desktop picker / ASAR patch
- [ ] Other (please describe)

## Additional context

<!-- Anything else useful: provider name, model id, did this work in a
previous Codex Desktop build, etc. -->
