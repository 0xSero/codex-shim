# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/),
and this project does not yet follow semantic versioning (pre-1.0).

## Unreleased

### Added

- GitHub Actions CI (`.github/workflows/ci.yml`) running pytest and
  `compileall` on Python 3.11 and 3.12.
- `[project.optional-dependencies] dev` in `pyproject.toml` so
  `pip install -e ".[dev]"` pulls `pytest` and `pytest-asyncio` in one step.
- `CONTRIBUTING.md` documenting the dev loop, what kinds of PRs are useful,
  and what to include in bug reports.
- `.github/ISSUE_TEMPLATE/` with structured bug and feature request templates.
- `CHANGELOG.md` (this file).
- Web-based model picker at `GET /picker` (with `GET /api/models` and
  `POST /api/switch`) so the active shim model can be swapped from a browser
  without the CLI. Switching rewrites `model = "..."` and the
  `[model_providers.codex_shim]` `name = "..."` in `~/.codex/config.toml` so
  the Codex Desktop UI shows the selected model name (e.g. "Kimi K2.6")
  instead of the generic "Codex Shim" label. Optional auto-restart of Codex
  Desktop is cross-platform (`taskkill` + `Codex.exe` on Windows,
  `osascript` + `open -a Codex` on macOS). All picker routes are behind the
  existing `Host`-header allowlist, so a visited web page still cannot drive
  them via DNS rebinding.
- Best-effort dump of the last forwarded chat-completions request body to
  `.codex-shim/last_request.json` to make strict-provider tokenization /
  schema errors easier to triage. Upstream error bodies are now logged with
  the model slug before being forwarded back.

### Changed

- Reframed the project around a generic all-model Codex shim instead of any
  single upstream app or model store.
- Made `~/.codex-shim/models.json` the canonical default settings file.
- Renamed the generated Codex provider to `codex_shim` / "Codex Shim".
- Settings now prefer a generic top-level `models` array with snake_case keys,
  while still accepting `customModels` and camelCase aliases for existing
  exports.

### Fixed

- `codex-shim patch-app` now also patches the Codex Desktop sidebar's recent
  thread loader so native `openai` chats remain visible while Desktop is routed
  through the `codex_shim` provider. Tested on Codex Desktop 26.519.41501 /
  `codex-cli 0.133.0-alpha.1` on macOS arm64.
- `patch-app` now updates `ElectronAsarIntegrity` in `Info.plist` after
  repacking `app.asar`, and `restore-app` restores or recomputes that metadata
  before re-signing the app bundle.

## 2026-05-25 — Auth-gated ChatGPT passthrough + docs hardening

### Added

- `settings.chatgpt_passthrough_available()` checks `~/.codex/auth.json` for a
  usable `tokens.access_token`. The synthetic `gpt-5.5` slug is now only
  advertised in `/health`, `/v1/models`, `codex-shim list`, and the generated
  `custom_model_catalog.json` while that token is present.
- `_load_models()` in the CLI wraps model settings loading with actionable
  errors for missing files and invalid JSON.
- `_entrypoint()` in the CLI catches `BrokenPipeError` at the boundary so
  piping `codex-shim list` into `head`/`grep` exits cleanly instead of dumping
  a traceback.
- Regression tests covering auth-gating, CLI error UX, settings aliases, and
  catalog generation.

### Changed

- `/health` payload now includes `chatgpt_passthrough: bool` and reports the
  real model count instead of always-plus-one.
- `cli._resolve_model_slug("gpt-5.5", ...)` raises `SystemExit` telling the
  user to run `codex login` when auth.json is missing, instead of returning a
  slug that would 401 on first request.
- `default_model_slug` picks the first configured BYOK model when passthrough
  is not usable, instead of unconditionally returning `gpt-5.5`.
- README install section recommends `pip install -e .` as the primary path.
- README benchmarking section: replaced an unsupported "7x fewer input tokens
  / 5–10x faster" claim with honest anecdata and a note that no reproducible
  benchmark script ships with the repo yet.

### Fixed

- Codex Desktop picker / `/v1/models` no longer offers `gpt-5.5` when there's
  no Codex login, removing the misleading "select it to get a 401" footgun.

## 2026-05-25 — Initial public hardening

### Added

- Public-grade README rewrite covering install, ChatGPT passthrough, tool
  calls, computer use, prompt catching/proxy patterns, benchmarking, security,
  limitations, troubleshooting, and contributing.
- `pyproject.toml` build-system, `readme`, `license`, `authors`, `keywords`,
  classifiers, and project URLs.
