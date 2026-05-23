# codex-shim

Run **Codex Desktop** with any model declared in your `~/.factory/settings.json`
(or any custom JSON file), plus an optional passthrough to your **ChatGPT
subscription's GPT‑5.5** — without recompiling Codex.

The shim is a small local Python server that pretends to be an OpenAI Responses
API endpoint. Codex points at it; the shim routes each request to whatever
upstream the matching Factory BYOK entry uses (OpenAI / Anthropic /
generic-chat-completion-api / ChatGPT subscription).

> Status: tested on Codex Desktop **0.133.0-alpha.1** for macOS arm64 and
> Windows (MSIX). On macOS the picker patch and integrity re-sign are applied
> in-place; on Windows `codex-shim patch-app` copies the read-only MSIX app to a
> writable directory and patches both the ASAR and the executable.

---

## Why

Codex Desktop only shows the models its server-side Statsig config whitelists.
If you have OpenAI / Anthropic / Z.ai / DeepSeek / Gemini / OpenRouter / Factory
keys you'd like to use **as first-class models in the picker**, this gets you
there. It also lets you keep your ChatGPT subscription's GPT‑5.5 visible
alongside everything else.

---

## Install

```bash
git clone https://github.com/<you>/codex-shim ~/Documents/codex-shim
cd ~/Documents/codex-shim
python3 -m pip install --user aiohttp pytest    # only runtime dep is aiohttp
ln -s "$PWD/bin/codex-shim" ~/.local/bin/codex-shim
ln -s "$PWD/bin/codex-app"  ~/.local/bin/codex-app
ln -s "$PWD/bin/codex-model" ~/.local/bin/codex-model
```

On **Windows** (PowerShell):

```powershell
git clone https://github.com/<you>/codex-shim $HOME\Documents\codex-shim
cd $HOME\Documents\codex-shim
python -m pip install aiohttp pytest
# The bin/*.cmd wrappers work without PATH changes when run from the repo root.
```

Requires Python 3.11+.

---

## Quick start

### 1. Generate the catalog and start the shim

```bash
codex-shim generate          # reads ~/.factory/settings.json, writes catalog
codex-shim start             # background daemon on 127.0.0.1:8765
codex-shim list              # show generated slugs and upstream routes
codex-shim status            # health probe
```

### 2. Point Codex Desktop at it (no global config changes)

```bash
codex-shim app .             # launch Codex with the shim wired in
```

That command applies opt-in `-c` overrides only for this launch. Your
`~/.codex/config.toml` is left untouched. After this Codex Desktop sees every
entry from `~/.factory/settings.json` plus an optional `OpenAI GPT-5.5
(ChatGPT)` slug as picker entries.

If your Codex Desktop's model picker only shows "default" and refuses to render
the catalog entries, you also need the **picker patch** below.

### 3. (Optional) Switch the active Desktop model

```bash
codex-model list
codex-model openai-gpt-5-5    # or any other slug from `list`
codex-app                     # relaunch Codex with new default
```

---

## Custom config file

The shim defaults to `~/.factory/settings.json` (the file Factory.ai writes
when you save BYOK custom models). You can point it at any file:

```bash
codex-shim --settings /path/to/my-models.json generate
codex-shim --settings /path/to/my-models.json start
```

Schema expected (Factory's own format):

```json
{
  "customModels": [
    {
      "model": "gpt-5.5",
      "provider": "openai",
      "baseUrl": "https://api.openai.com/v1",
      "apiKey": "sk-…",
      "displayName": "OpenAI GPT-5.5",
      "maxContextLimit": 400000
    },
    {
      "model": "claude-opus-4-7-20251109",
      "provider": "anthropic",
      "baseUrl": "https://api.anthropic.com/v1",
      "apiKey": "sk-ant-…",
      "displayName": "Claude Opus 4.7"
    },
    {
      "model": "deepseek-v4-pro",
      "provider": "anthropic",
      "baseUrl": "https://api.deepseek.com/anthropic",
      "apiKey": "…",
      "displayName": "DeepSeek V4 Pro",
      "noImageSupport": true
    }
  ]
}
```

The shim **never copies your API keys** into the generated catalog. Keys stay
in your settings file and are read fresh on every request.

Supported `provider` values:

| provider | upstream API |
|---|---|
| `openai` | OpenAI/`/v1/chat/completions` |
| `generic-chat-completion-api` | OpenAI-shaped chat completions |
| `anthropic` | Anthropic `/v1/messages` |

---

## Picker patch for Codex Desktop

### macOS

Codex Desktop has a Statsig server-side allowlist (`use_hidden_models: true`)
that hides any model whose slug isn't on a hardcoded list. Custom catalog
entries fall into the hidden bucket and never render in the picker.

A single‑boolean ASAR patch flips the allowlist branch off so the picker only
checks the local `hidden` flag (which our catalog never sets).

> **Always back up `app.asar` and `Info.plist` before patching.**

```bash
APP=/Applications/Codex.app
sudo cp -R "$APP" "$APP.unpatched-$(date +%Y%m%d-%H%M%S)"

# 1. Extract the ASAR
cd /tmp && rm -rf codex-asar-patch && mkdir codex-asar-patch && cd codex-asar-patch
npx --yes @electron/asar extract "$APP/Contents/Resources/app.asar" extracted

# 2. Patch the picker filter (this match is single-occurrence, unique to that file)
PATCH_FILE=$(grep -RIl 'useHiddenModels' extracted/webview/assets/model-queries-*.js | head -n1)
sed -i.bak -E 's/let u=c\.useHiddenModels&&o!==`amazonBedrock`,d;/let u=!1,d;/' "$PATCH_FILE"
diff "$PATCH_FILE.bak" "$PATCH_FILE" || true   # confirm exactly one change
rm "$PATCH_FILE.bak"

# 3. Repack
npx --yes @electron/asar pack extracted app.asar.new
sudo cp app.asar.new "$APP/Contents/Resources/app.asar"
```

That alone will crash Codex on next launch with `EXC_BREAKPOINT`. Electron's
`ElectronAsarIntegrity` field in `Info.plist` is a SHA-256 of the **JSON
header** of the asar archive (not the whole file). Recompute it and re-sign:

```bash
# 4. Compute new header hash
HEADER_HASH=$(python3 - "$APP/Contents/Resources/app.asar" <<'PY'
import struct, hashlib, sys
with open(sys.argv[1], 'rb') as f:
    data_size, header_size, _, json_size = struct.unpack('<4I', f.read(16))
    header_json = f.read(json_size)
print(hashlib.sha256(header_json).hexdigest())
PY
)
echo "new header hash: $HEADER_HASH"

# 5. Patch Info.plist (replaces the hash for Resources/app.asar)
sudo /usr/libexec/PlistBuddy -c \
  "Set :ElectronAsarIntegrity:Resources/app.asar:hash $HEADER_HASH" \
  "$APP/Contents/Info.plist"

# 6. Ad-hoc re-sign (drops Apple signature; Gatekeeper will warn once)
sudo codesign --force --deep --sign - "$APP"

# 7. Launch
open "$APP"
```

To roll back: `sudo rm -rf "$APP" && sudo mv "$APP.unpatched-…" "$APP"`.

### Windows

On Windows the Codex Desktop is installed as a read-only MSIX package under
`Program Files\WindowsApps`. `codex-shim patch-app` automates the full process:

1. Locates the Codex MSIX bundle (via directory listing or PowerShell
   `Get-AppxPackage`).
2. Extracts the ASAR and patches the picker filter (same `useHiddenModels`
   boolean flip).
3. Copies the entire app tree to `.codex-shim/codex-desktop-patched/app/`.
4. Repacks the patched ASAR into the copy.
5. Patches the embedded ASAR integrity SHA-256 hash inside `Codex.exe` so
   Electron accepts the modified archive.

```powershell
codex-shim patch-app
# Output: Patched Codex Desktop copied to: …\.codex-shim\codex-desktop-patched\app
# Launch it with: …\.codex-shim\codex-desktop-patched\app\Codex.exe
```

Then start the shim (use the same Python that has `aiohttp` installed) and launch
Codex:

```powershell
codex-shim start
codex-shim app .
```

`app` launches the patched copy under `.codex-shim/codex-desktop-patched/app/`
when `patch-app` has been run. Run `patch-app` first if you see a warning that
the patched executable is missing.

To roll back:

```powershell
codex-shim restore-app   # removes patched copies + ~/.codex shim config
codex-shim stop          # stops the local server
```

Close Codex first if folders are "still in use". Then launch Codex from the Start
menu (not `codex-desktop-patched-*/Codex.exe`). The read-only MSIX install is never
modified.

---

## ChatGPT GPT‑5.5 passthrough (optional)

If you have a ChatGPT plan with Codex access (`~/.codex/auth.json` exists with
`auth_mode: chatgpt`), the shim exposes one synthetic slug
`openai-gpt-5-5` (display name `OpenAI GPT-5.5 (ChatGPT)`) that proxies
straight to `https://chatgpt.com/backend-api/codex/responses` with your access
token. It bypasses Factory entirely and uses your ChatGPT subscription quota.

It's already in `.codex-shim/custom_model_catalog.json` after `codex-shim
generate`. Just select it in the picker.

If you don't want it, delete the entry with slug `openai-gpt-5-5` from the
generated catalog, or run with `CODEX_SHIM_DISABLE_CHATGPT=1` (TODO).

---

## How the routing works

```
Codex Desktop ── /v1/responses ──▶ codex-shim (127.0.0.1:8765)
                                     │
                                     ├── slug "openai-gpt-5-5"
                                     │       └─▶ chatgpt.com/backend-api/codex/responses
                                     │           (Authorization: Bearer <auth.json access_token>)
                                     │
                                     ├── provider "openai" / "generic-…"
                                     │       └─▶ baseUrl/chat/completions
                                     │           (Authorization: Bearer apiKey)
                                     │
                                     └── provider "anthropic"
                                             └─▶ baseUrl/messages
                                                 (x-api-key: apiKey, anthropic-version: …)
```

The shim translates Codex's Responses-API request into the upstream's shape
(chat completions or Anthropic Messages) and translates the streamed reply
back. Extended-thinking blocks from Anthropic-shaped upstreams (Claude,
DeepSeek, GLM) round-trip through `reasoning.encrypted_content` items.

---

## MCP

Codex Desktop forwards three generic MCP tools to every model:

- `list_mcp_resources`
- `list_mcp_resource_templates`
- `read_mcp_resource`

It does **not** flatten individual MCP server tools into the function list.
That's a Codex client behavior, not a shim limitation. Shim-routed models
receive the same MCP tools as built-in OpenAI models. The model is expected
to call `list_mcp_resources` to discover what's available.

---

## Commands

```
codex-shim generate         regenerate catalog/config without starting daemon
codex-shim start            start local shim daemon
codex-shim status           health check + model count
codex-shim stop             stop daemon
codex-shim restart          restart daemon
codex-shim list             list generated slugs and Factory routes
codex-shim model list       list slugs currently usable in the picker
codex-shim model use <slug> set the Desktop default model
codex-shim codex -- <args>  exec `codex` CLI through the shim
codex-shim app [path]       launch Codex Desktop through the shim
codex-shim patch-app        patch Codex Desktop picker filter + integrity
codex-shim restore-app      restore Codex Desktop from pre-patch backup

codex-app [path]            shortcut for `codex-shim app`
codex-model [list|<slug>]   shortcut for `codex-shim model …`
```

All commands accept `--settings <path>` and `--port <port>`.

---

## File layout

```
codex_shim/             python source (server + cli + translation)
bin/codex-shim          main entrypoint (POSIX shell)
bin/codex-app           shortcut wrapping `codex-shim app` (POSIX shell)
bin/codex-model         shortcut wrapping `codex-shim model …` (POSIX shell)
bin/codex-shim.cmd      main entrypoint (Windows)
bin/codex-app.cmd       shortcut for `codex-shim app` (Windows)
bin/codex-model.cmd     shortcut for `codex-shim model …` (Windows)
.codex-shim/            generated catalog, config, logs, pid (gitignored)
tests/                  pytest suite
```

The shim never edits `~/.codex/config.toml`. All Codex overrides are passed
inline as `-c key=value` arguments per launch.

---

## License

MIT — see `LICENSE`.

Codex Desktop is a trademark of OpenAI. This project is unaffiliated.
