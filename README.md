# codex-shim

Run **Codex Desktop** with any model declared in your `~/.factory/settings.json`
(or any custom JSON file), plus an optional passthrough to your **ChatGPT
subscription's GPT‑5.5** — without recompiling Codex.

The shim is a small local Python server that pretends to be an OpenAI Responses
API endpoint. Codex points at it; the shim routes each request to whatever
upstream the matching Factory BYOK entry uses (OpenAI / Anthropic /
generic-chat-completion-api / ChatGPT subscription).

> Status: tested on Codex Desktop **0.133.0-alpha.1** for macOS arm64.
> Linux/Windows users should be able to skip the ASAR patch section and use the
> shim itself unchanged.

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

### 2. Point Codex CLI at it (no global config changes)

```bash
codex-shim codex -- .        # run Codex CLI with temporary -c overrides
```

That command applies opt-in `-c` overrides only for this launch. Your
`~/.codex/config.toml` is left untouched.

### 3. Point Codex Desktop at it (writes managed config)

```bash
codex-shim app .             # install managed config, then launch Desktop
```

Codex Desktop does not currently accept the same per-launch `-c` overrides as
the CLI wrapper. `codex-shim app`, `codex-shim enable`, and
`codex-shim model use <slug>` therefore write a clearly marked managed block to
`~/.codex/config.toml`. `codex-shim disable` removes those managed shim blocks
without restoring stale whole-file backups. After this Codex Desktop sees every
entry from `~/.factory/settings.json` plus a `GPT-5.5` entry when ChatGPT
passthrough is enabled and usable.

If your Codex Desktop's model picker only shows "default" and refuses to render
the catalog entries, you also need the **picker patch** below.

### 4. (Optional) Switch the active Desktop model

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

## OpenRouter actual-test path

Factory is only the JSON shape this shim already understands. You do not need
Factory.ai to test the shim: any OpenAI-compatible chat-completions provider can
be listed in `customModels`. OpenRouter works with
`baseUrl = "https://openrouter.ai/api/v1"` and provider
`generic-chat-completion-api`.

Start from the committed-safe template, then keep the real API key in the
ignored `.codex-shim/` runtime directory:

```bash
mkdir -p .codex-shim
cp examples/openrouter-settings.example.json .codex-shim/openrouter-settings.json
$EDITOR .codex-shim/openrouter-settings.json
```

In `.codex-shim/openrouter-settings.json`, replace
`REPLACE_WITH_OPENROUTER_API_KEY` with your OpenRouter key. You can also change
`model` to any OpenRouter model id, and `displayName` to the name you want
Codex to show. Do not commit this local file.

Generate, list, and start a shim instance on port `8766` with ChatGPT
passthrough disabled so the test uses only OpenRouter:

```bash
CODEX_SHIM_DISABLE_CHATGPT=1 codex-shim \
  --settings .codex-shim/openrouter-settings.json \
  --port 8766 \
  generate

CODEX_SHIM_DISABLE_CHATGPT=1 codex-shim \
  --settings .codex-shim/openrouter-settings.json \
  list

CODEX_SHIM_DISABLE_CHATGPT=1 codex-shim \
  --settings .codex-shim/openrouter-settings.json \
  --port 8766 \
  start
```

`list` prints the shim slug in the first column. With the example unchanged it
is usually `openai-gpt-4o-mini`. Run Codex through the safe wrapper with that
slug, replacing it if your `list` output differs:

```bash
CODEX_SHIM_DISABLE_CHATGPT=1 codex-shim \
  --settings .codex-shim/openrouter-settings.json \
  --port 8766 \
  codex -- -m openai-gpt-4o-mini .
```

For the usual local OpenRouter workflow, the `codex-openrouter` helper does
those steps for you: it uses `.codex-shim/openrouter-settings.json`, disables
ChatGPT passthrough, starts the shim on port `8766`, detects the first listed
slug, and runs Codex through the safe wrapper. On first run, if the settings
file is missing, empty, or still has the placeholder API key, it prompts for
your OpenRouter key and model and writes the ignored local settings file.

```bash
bin/codex-openrouter .
```

To change the stored OpenRouter model or key later:

```bash
bin/codex-openrouter setup
```

The setup prompt shows current values and lets Enter keep them. The API key is
not echoed; pressing Enter keeps the existing key when one is already present.

Optional overrides:

```bash
CODEX_SHIM_MODEL=openrouter-owl-alpha bin/codex-openrouter .
CODEX_SHIM_PORT=8770 bin/codex-openrouter .
CODEX_SHIM_SETTINGS=/path/to/openrouter-settings.json bin/codex-openrouter .
```

That command uses temporary inline `-c` overrides only. To verify your normal
Codex config was not touched, compare the file before and after:

```bash
sha256sum ~/.codex/config.toml 2>/dev/null || true
# run the codex-shim codex -- ... command above
sha256sum ~/.codex/config.toml 2>/dev/null || true
```

If `~/.codex/config.toml` does not exist, both commands may print nothing; the
safe wrapper should not create it.

Stop the local shim when done:

```bash
codex-shim --port 8766 stop
```

---

## Picker patch for Codex Desktop on macOS

Codex Desktop has a Statsig server-side allowlist (`use_hidden_models: true`)
that hides any model whose slug isn't on a hardcoded list. Custom catalog
entries fall into the hidden bucket and never render in the picker.

A single‑boolean ASAR patch flips the allowlist branch off so the picker only
checks the local `hidden` flag (which our catalog never sets).

> **Always back up `app.asar` and `Info.plist` before patching.**
> The built-in `codex-shim patch-app` command is currently incomplete for modern
> Electron bundles because it does not update `ElectronAsarIntegrity` in
> `Info.plist`; prefer the manual procedure below until that command is fixed
> and tested on macOS.

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

---

## ChatGPT GPT‑5.5 passthrough (optional)

If you have a ChatGPT plan with Codex access (`~/.codex/auth.json` exists with
a usable `tokens.access_token`), the shim exposes one synthetic slug
`gpt-5.5` (display name `GPT-5.5`) that proxies
straight to `https://chatgpt.com/backend-api/codex/responses` with your access
token. It bypasses Factory entirely and uses your ChatGPT subscription quota.

It is included in `.codex-shim/custom_model_catalog.json` after
`codex-shim generate` only when usable ChatGPT auth is present.

If you don't want it, run with `CODEX_SHIM_DISABLE_CHATGPT=1`.

---

## How the routing works

```
Codex Desktop ── /v1/responses ──▶ codex-shim (127.0.0.1:8765)
                                     │
                                     ├── slug "gpt-5.5"
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
codex-shim model use <slug> set the Desktop default model in managed config
codex-shim codex -- <args>  exec `codex` CLI through temporary -c overrides
codex-shim app [path]       install managed config and launch Codex Desktop

codex-app [path]            shortcut for `codex-shim app`
codex-model [list|<slug>]   shortcut for `codex-shim model …`
codex-openrouter [args]     shortcut for the safe OpenRouter CLI workflow
```

All commands accept `--settings <path>` and `--port <port>`.

---

## File layout

```
codex_shim/             python source (server + cli + translation)
bin/codex-shim          main entrypoint
bin/codex-app           shortcut wrapping `codex-shim app`
bin/codex-model         shortcut wrapping `codex-shim model …`
bin/codex-openrouter    shortcut for the safe OpenRouter CLI workflow
.codex-shim/            generated catalog, config, logs, pid (gitignored)
tests/                  pytest suite
```

The safe CLI path, `codex-shim codex -- ...`, never edits
`~/.codex/config.toml`; it passes overrides inline as `-c key=value` arguments
for that launch only. Desktop commands that need persistent Desktop integration
write and later remove marked managed blocks.

---

## License

MIT — see `LICENSE`.

Codex Desktop is a trademark of OpenAI. This project is unaffiliated.
