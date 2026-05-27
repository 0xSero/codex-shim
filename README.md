# codex-shim

Run **Codex Desktop** against any BYOK model you can describe in
`~/.codex-shim/models.json`, plus an optional passthrough to your **ChatGPT
subscription's Codex model** — without rebuilding Codex.

The shim is a local Python/aiohttp server that exposes an OpenAI
Responses-compatible endpoint on loopback. Codex points at the shim; the shim
routes each request to the matching upstream (OpenAI chat completions,
Anthropic Messages, a generic OpenAI-shaped chat endpoint, or ChatGPT Codex
passthrough), then translates streaming responses back into the shape Codex
expects.

> Tested on Codex Desktop **0.133.0-alpha.1** for macOS arm64. The shim server
> and routing layer are plain Python/aiohttp and work on Windows, macOS, Linux,
> WSL, and Git Bash. The only macOS-specific piece is the optional Desktop picker
> ASAR patch, needed when Codex hides custom catalog entries.

---

## What this gives you

Codex Desktop only shows models allowed by its server-side config. If you have
OpenAI / Anthropic / Z.ai / DeepSeek / Gemini / OpenRouter / local proxy models
you want as first-class picker entries, this wires them in locally.

The practical win is that Codex keeps its native UX while model routing moves
local:

- **BYOK models in the normal Codex picker.** No Codex rebuild, no request
  replay workflow.
- **Native Codex agent loops stay intact.** Function calls, tool outputs,
  reasoning blocks, image-capable models, shell-command metadata, and streaming
  SSE are translated instead of flattened into plain text.
- **ChatGPT/Codex passthrough.** If `~/.codex/auth.json` has a valid Codex
  access token, the shim can route Codex's native `/v1/responses` traffic to
  ChatGPT's Codex backend under the `gpt-5.5` slug used by current Codex builds.
- **Prompt-catching/proxy-friendly architecture.** Put a local proxy in front
  of the shim to dedupe boilerplate, inject stable instructions, repair
  pseudo-tool text, or route prompts by policy before they hit an upstream.
- **Maintainer-side wins on real coding-agent runs.** In the maintainer's
  internal Codex tasks, ChatGPT passthrough plus a prompt-catching proxy in
  front of the shim has produced multi-x reductions in billed input tokens
  and noticeably faster wall time vs. the baseline route. No reproducible
  benchmark script ships with the repo yet, so treat that as anecdata — the
  benchmark section below explains how to measure your own setup against
  an explicit oracle before quoting numbers.

---

## Requirements

- Python 3.11+.
- Codex CLI/Desktop installed and authenticated.
- One of:
  - `~/.codex-shim/models.json` with configured BYOK/upstream models;
  - a compatible JSON file passed with `--settings`;
  - `~/.codex/auth.json` containing `tokens.access_token` for ChatGPT/Codex
    passthrough-only use.
- Windows: PowerShell/cmd works when installed via the Python package entry
  point; WSL or Git Bash is needed only for the optional `bin/` shell wrappers.
- macOS only: `npx` and `codesign` if you need the optional Desktop picker
  patch.

---

## Install

Recommended on macOS/Linux/WSL/Git Bash (installs the `codex-shim` entry
point from `pyproject.toml`):

```bash
git clone https://github.com/0xSero/codex-shim ~/codex-shim
cd ~/codex-shim
python3 -m pip install --user -e .
```

Recommended on native Windows PowerShell/cmd:

```powershell
git clone https://github.com/0xSero/codex-shim $HOME\codex-shim
cd $HOME\codex-shim
py -3.11 -m pip install --user -e .
```

That pulls in `aiohttp` and installs the portable Python console command
`codex-shim`. On POSIX-like shells, the optional `codex-app` and `codex-model`
shortcuts live in `bin/`; symlink them if you want them on `PATH` too:

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/codex-app" ~/.local/bin/codex-app
ln -sf "$PWD/bin/codex-model" ~/.local/bin/codex-model
```

If you move the checkout, recreate those symlinks; `codex-shim app` launches
`codex app` through the installed Python entry point and does not need them.

Alternative on macOS/Linux/WSL/Git Bash (no install, run straight from the
checkout):

```bash
git clone https://github.com/0xSero/codex-shim ~/codex-shim
cd ~/codex-shim
python3 -m pip install --user aiohttp
mkdir -p ~/.local/bin
ln -sf "$PWD/bin/codex-shim" ~/.local/bin/codex-shim
ln -sf "$PWD/bin/codex-app" ~/.local/bin/codex-app
ln -sf "$PWD/bin/codex-model" ~/.local/bin/codex-model
```

For running the test suite:

```bash
python3 -m pip install --user pytest pytest-asyncio
```

If your POSIX shell cannot find the commands, make sure `~/.local/bin` is on
`PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

If PowerShell cannot find `codex-shim`, add your Python user Scripts directory
to `Path`. For Python 3.11 installed from python.org, the usual path is:

```powershell
$env:APPDATA\Python\Python311\Scripts
```

You can also skip `PATH` entirely and run through Python:

```powershell
py -3.11 -m codex_shim.cli status
```

---

## Windows support

Yes, the shim works on Windows. The core shim is Python/aiohttp, binds to
`127.0.0.1`, and writes the same Codex provider config that macOS/Linux use.
Use one of these setups:

| Setup | Status | Notes |
|---|---|---|
| Native Windows PowerShell/cmd | Supported | Install with `py -3.11 -m pip install --user -e .` and run `codex-shim ...`. |
| WSL | Supported | Works like Linux. Best when Codex CLI/Desktop is also being driven from WSL. |
| Git Bash | Supported | Works with the POSIX `bin/` wrappers if Python/Codex are on `PATH`. |
| `bin/codex-app`, `bin/codex-model` in PowerShell/cmd | Not native | These are shell scripts. Use `codex-shim app ...` and `codex-shim model ...` instead. |
| `patch-app` / `restore-app` | macOS only | They target `/Applications/Codex.app` and Electron ASAR signing on macOS. |

Native Windows quick check:

```powershell
py -3.11 -m pip install --user -e .
codex-shim generate
codex-shim start
codex-shim status
codex-shim list
```

If `codex-shim` is not on `Path`, use the module form:

```powershell
py -3.11 -m codex_shim.cli generate
py -3.11 -m codex_shim.cli start
py -3.11 -m codex_shim.cli status
```

Path behavior is intentionally ordinary:

- In native Windows, `~/.codex-shim/models.json` means
  `%USERPROFILE%\.codex-shim\models.json` and Codex config lives under
  `%USERPROFILE%\.codex\config.toml`.
- In WSL, `~/.codex-shim/models.json` and `~/.codex/config.toml` are inside the
  Linux home directory unless you explicitly point `--settings` at a Windows
  path under `/mnt/c/...`.
- Do not mix a WSL-generated `~/.codex/config.toml` with native Windows Codex
  and expect both to share files automatically. If Codex is native Windows, run
  the native Windows install path or manually keep the Windows config in sync.
- The local provider URL is still `http://127.0.0.1:8765/v1`.

The optional macOS picker patch is not required for the shim server to work. On
Windows, if Codex can read the generated catalog/provider config, requests route
through the same local endpoint as every other platform.

Windows Store/MSIX Codex Desktop builds are stricter than the CLI. They may treat
custom local/BYOK slugs as unavailable, rewrite `model = "<custom-slug>"` back to
`gpt-5.5`, and add `[tui.model_availability_nux]` entries on launch. That is a
Desktop allowlist behavior, not a shim routing behavior: `codex exec`, the TUI,
and the shim endpoint still use the configured model slug. The macOS `patch-app`
helper does not apply to MSIX packages under `C:\\Program Files\\WindowsApps`.

If Windows has a system proxy such as Clash/V2Ray, make sure loopback bypasses it:

```powershell
setx NO_PROXY "127.0.0.1,localhost,::1"
setx no_proxy "127.0.0.1,localhost,::1"
```

`codex-shim codex -- ...` and `codex-shim app ...` add those loopback entries to
the launched process environment automatically; set them globally too if you run
`codex.exe` directly.

---

## Quick start

### 1. Generate the catalog and start the shim

```bash
codex-shim generate          # reads ~/.codex-shim/models.json if present
codex-shim start             # background daemon on 127.0.0.1:8765
codex-shim list              # show generated slugs and upstream routes
codex-shim status            # health probe + model count
```

Generated runtime files live under the repo-local `.codex-shim/` directory:

```text
.codex-shim/custom_model_catalog.json   # model picker catalog for Codex
.codex-shim/config.toml                  # opt-in Codex provider config
.codex-shim/shim.pid                     # daemon pid
.codex-shim/shim.log                     # stdout/stderr + request summaries
```

The server binds `127.0.0.1` by default. It is meant to be a local loopback
adapter, not an Internet-facing proxy.

### 2. Point Codex Desktop at it

```bash
codex-shim app .             # launch Codex Desktop with the shim wired in
```

`app` generates the catalog, starts the local daemon if needed, and writes a
small managed block into `~/.codex/config.toml` so Codex Desktop uses the local
provider. The previous config is backed up under `.codex-shim/` and the managed
block can be removed with:

```bash
codex-shim disable
```

After this, Codex Desktop sees every entry from `~/.codex-shim/models.json`,
plus the `GPT-5.5` ChatGPT passthrough slug if (and only if) `~/.codex/auth.json`
holds a valid `tokens.access_token`.

If your Codex Desktop's model picker only shows `default` and refuses to render
the catalog entries, apply the macOS picker patch below.

### 3. Switch the active Desktop model

```bash
codex-model list
codex-model gpt-5.5          # or any other slug from `list`
codex-app                   # relaunch Codex with new default
```

`codex-model <slug>` is a shortcut for `codex-shim model use <slug>`. It writes
only the shim-managed block in `~/.codex/config.toml`.

### 4. Use the Codex CLI without writing config

For one-off CLI runs, use inline `-c` overrides instead of changing
`~/.codex/config.toml`:

```bash
codex-shim codex -- "inspect this repo and summarize the architecture"
```

---

## Custom config file

The shim defaults to `~/.codex-shim/models.json`. If that file is missing, the
shim still generates a catalog — and adds the `gpt-5.5` ChatGPT passthrough
entry only when `~/.codex/auth.json` contains a valid `tokens.access_token`.
You can point it at any compatible file:

```bash
codex-shim --settings /path/to/my-models.json generate
codex-shim --settings /path/to/my-models.json start
```

Recommended schema:

```json
{
  "models": [
    {
      "model": "gpt-5.5",
      "provider": "openai",
      "base_url": "https://api.openai.com/v1",
      "api_key": "sk-…",
      "display_name": "OpenAI GPT-5.5",
      "max_context_limit": 400000
    },
    {
      "model": "claude-opus-4-7-20251109",
      "provider": "anthropic",
      "base_url": "https://api.anthropic.com/v1",
      "api_key": "sk-ant-…",
      "display_name": "Claude Opus 4.7"
    },
    {
      "model": "deepseek-v4-pro",
      "provider": "anthropic",
      "base_url": "https://api.deepseek.com/anthropic",
      "api_key": "…",
      "display_name": "DeepSeek V4 Pro",
      "no_image_support": true
    }
  ]
}
```

The loader also accepts camelCase aliases (`baseUrl`, `apiKey`, `displayName`,
`maxContextLimit`, `maxOutputTokens`, `noImageSupport`, `extraHeaders`) and a
legacy top-level `customModels` array, so existing model config exports can be
used directly.

The shim **never copies your API keys** into the generated catalog. Keys stay
in your settings file and are read fresh on every request.

Supported `provider` values:

| provider | upstream API |
|---|---|
| `openai` | OpenAI `/v1/chat/completions` |
| `generic-chat-completion-api` | OpenAI-shaped chat completions |
| `anthropic` | Anthropic `/v1/messages` |

Useful model fields:

| field | behavior |
|---|---|
| `display_name` | Human-readable picker label. |
| `max_context_limit` | Catalog context window and compaction limits. |
| `max_output_tokens` | Default max output when translating to Anthropic. |
| `no_image_support` | When true, catalog advertises text-only input. |
| `extra_headers` | Optional upstream headers merged into requests. |

### Ollama / local OpenAI-compatible chat endpoints

Codex sends the Responses API. Ollama and many local servers expose
OpenAI-shaped `/v1/chat/completions` instead. Keep Codex pointed at the shim with
`wire_api = "responses"`; configure Ollama as `generic-chat-completion-api` so
the shim translates Responses ⇄ chat completions:

```json
{
  "models": [
    {
      "model": "llama3.2",
      "display_name": "Ollama Llama 3.2",
      "provider": "generic-chat-completion-api",
      "base_url": "http://127.0.0.1:11434/v1",
      "api_key": "ollama"
    }
  ]
}
```

`codex-shim --settings /path/to/ollama-launch-models.json generate` also accepts
launch-model style files with a top-level `launchModels` / `launch_models` array,
including bare strings. `provider: "ollama"` is normalized to
`generic-chat-completion-api` with `http://127.0.0.1:11434/v1` when no base URL
is supplied.

Repeated `codex-shim enable`, `codex-shim app`, and `codex-shim model use ...`
runs are idempotent: the shim-managed top-level keys and
`[model_providers.codex_shim]` block are removed before the new managed block is
written, so duplicate profile/provider keys should not accumulate.

Codex may make small background calls to OpenAI model slugs such as
`gpt-5.4-mini` for its own product behavior. Those calls are not Ollama routing
failures; use the shim request log to confirm the actual selected model for the
agent turn.

---

## Picker patch for Codex Desktop on macOS

Codex Desktop has a Statsig server-side allowlist (`use_hidden_models: true`)
that hides any model whose slug is not on a hardcoded list. Custom catalog
entries fall into the hidden bucket and never render in the picker.

A single-boolean ASAR patch flips the allowlist branch off so the picker only
checks the local `hidden` flag (which this catalog never sets). On recent
Codex Desktop builds, the patch also changes the local recent-thread loader
from `modelProviders: null` to `modelProviders: []` so the sidebar continues to
show existing native `openai` chats while Desktop is routed through the
`codex_shim` provider.

The combined patch has been tested on Codex Desktop **26.519.41501** /
`codex-cli 0.133.0-alpha.1` on macOS arm64.

> Back up `app.asar` and `Info.plist` before patching.

```bash
APP=/Applications/Codex.app
sudo cp -R "$APP" "$APP.unpatched-$(date +%Y%m%d-%H%M%S)"

# 1. Extract the ASAR
cd /tmp && rm -rf codex-asar-patch && mkdir codex-asar-patch && cd codex-asar-patch
npx --yes @electron/asar extract "$APP/Contents/Resources/app.asar" extracted

# 2. Patch the picker filter (single occurrence in tested builds)
PATCH_FILE=$(grep -RIl 'useHiddenModels' extracted/webview/assets/model-queries-*.js | head -n1)
sed -i.bak -E 's/let u=c\.useHiddenModels&&o!==`amazonBedrock`,d;/let u=!1,d;/' "$PATCH_FILE"
diff "$PATCH_FILE.bak" "$PATCH_FILE" || true
rm "$PATCH_FILE.bak"

# 3. Patch the sidebar recent-thread provider filter (single occurrence)
SIDEBAR_FILE=$(grep -RIl 'listRecentThreads' extracted/webview/assets/app-server-manager-signals-*.js | head -n1)
python3 - "$SIDEBAR_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = "listRecentThreads({cursor:e,limit:t}){return this.params.requestClient.sendRequest(`thread/list`,{limit:t,cursor:e,sortKey:this.recentConversationSortKey,modelProviders:null,archived:!1,sourceKinds:ke})}"
new = "listRecentThreads({cursor:e,limit:t}){return this.params.requestClient.sendRequest(`thread/list`,{limit:t,cursor:e,sortKey:this.recentConversationSortKey,modelProviders:[],archived:!1,sourceKinds:ke})}"
if text.count(old) != 1:
    raise SystemExit("expected one sidebar provider filter occurrence")
path.write_text(text.replace(old, new, 1))
PY

# 4. Repack
npx --yes @electron/asar pack extracted app.asar.new
sudo cp app.asar.new "$APP/Contents/Resources/app.asar"
```

That alone can crash Codex on next launch with `EXC_BREAKPOINT`. Electron's
`ElectronAsarIntegrity` field in `Info.plist` is a SHA-256 of the **JSON
header** of the ASAR archive (not the whole file). Recompute it and re-sign:

```bash
# 5. Compute new header hash
HEADER_HASH=$(python3 - "$APP/Contents/Resources/app.asar" <<'PY'
import struct, hashlib, sys
with open(sys.argv[1], 'rb') as f:
    data_size, header_size, _, json_size = struct.unpack('<4I', f.read(16))
    header_json = f.read(json_size)
print(hashlib.sha256(header_json).hexdigest())
PY
)
echo "new header hash: $HEADER_HASH"

# 6. Patch Info.plist (replaces the hash for Resources/app.asar)
sudo /usr/libexec/PlistBuddy -c \
  "Set :ElectronAsarIntegrity:Resources/app.asar:hash $HEADER_HASH" \
  "$APP/Contents/Info.plist"

# 7. Ad-hoc re-sign
sudo codesign --force --deep --sign - "$APP"

# 8. Launch
open "$APP"
```

To roll back: `sudo rm -rf "$APP" && sudo mv "$APP.unpatched-…" "$APP"`.

The CLI also has helper commands for patching/restoring `app.asar` and the
matching ASAR integrity metadata:

```bash
codex-shim patch-app
codex-shim restore-app
```

If Codex still crashes after `patch-app`, restore with `codex-shim restore-app`
and re-check the manual patch needles against the installed Desktop build.

---

## ChatGPT/Codex passthrough

If `~/.codex/auth.json` exists and contains `tokens.access_token`, the shim
exposes a synthetic `gpt-5.5` catalog entry that proxies straight to:

```text
https://chatgpt.com/backend-api/codex/responses
```

The entry is **only** advertised in `/health`, `/v1/models`, `codex-shim list`,
and the generated `custom_model_catalog.json` while that token is present. Once
you `codex logout` or the file is missing, the slug stops appearing — so the
picker never shows an option that would 401 on first use. Run `codex login` to
mint a new token and the entry comes back automatically on the next
`codex-shim generate`.

The passthrough keeps Codex's native `/v1/responses` payload intact, changes the
model to `gpt-5.5`, and sends your Codex access token as `Authorization: Bearer
<access_token>` with the ChatGPT account id from `auth.json` when present. It
bypasses configured BYOK routes entirely and uses your ChatGPT subscription quota.

It is already in `.codex-shim/custom_model_catalog.json` after `codex-shim
generate`. Select `GPT-5.5` in the picker, or run:

```bash
codex-model gpt-5.5
```

Older local configs or notes may refer to `openai-gpt-5-5`; the server accepts
that prefix as an alias and routes it to the same passthrough.

---

## How routing works

```text
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
(chat completions or Anthropic Messages) and translates the streamed reply back.
Extended-thinking blocks from Anthropic-shaped upstreams (Claude, DeepSeek,
GLM, etc.) round-trip through `reasoning.encrypted_content` items.

---

## Tool calls and agent loops

Codex expects Responses-API output items. Most BYOK upstreams speak either
OpenAI chat completions or Anthropic Messages. The shim bridges the gap:

| Codex/Responses item | OpenAI-shaped upstream | Anthropic upstream |
|---|---|---|
| `tools: [{type: "function", ...}]` | `tools: [{type: "function", function: ...}]` | `tools: [{name, description, input_schema}]` |
| `function_call` output item | Chat `tool_calls[]` | `tool_use` content block |
| `function_call_output` input item | Chat `role: "tool"` message | `tool_result` user content block |
| streamed argument deltas | `response.function_call_arguments.delta` | `response.function_call_arguments.delta` |
| parallel calls | Preserved via `parallel_tool_calls` where supported | Multiple `tool_use` blocks |

This is the piece that makes the shim useful for real Codex runs instead of only
text chat. A model can ask Codex to run tools, Codex sends the tool output back
through the shim, and the upstream model continues the same loop.

Known edge cases:

- Native Responses-only tool types such as freeform `apply_patch`, hosted
  `web_search`, or `computer_use` are only fully native on the ChatGPT
  passthrough path. Chat-completions and Anthropic upstreams receive function
  tools after translation.
- Some OpenAI-compatible providers advertise tool calls but stream malformed
  JSON arguments. The shim preserves deltas; the provider still has to emit
  valid JSON by the end of the call.
- If a provider ignores `parallel_tool_calls`, Codex may still request one tool
  at a time. That is an upstream behavior, not a catalog issue.

---

## Computer use, shell commands, images, and MCP

The generated catalog advertises the Codex-facing capabilities Codex needs to
run as an agent:

| catalog field | value |
|---|---|
| `shell_type` | `shell_command` |
| `apply_patch_tool_type` | `freeform` |
| `web_search_tool_type` | `text_and_image` |
| `supports_parallel_tool_calls` | `true` |
| `input_modalities` | `text,image` unless `noImageSupport: true` |
| `supports_image_detail_original` | disabled when `noImageSupport: true` |

What that means in practice:

- **Shell/file operations** are still executed by Codex Desktop/CLI. The shim
  only translates the model request and response stream.
- **Images/screenshots** can pass to providers that accept images. Set
  `noImageSupport: true` for text-only upstreams so Codex does not send image
  content they cannot parse.
- **Computer-use/native hosted tools** should use the ChatGPT passthrough path
  for best fidelity. BYOK chat/Anthropic routes can still participate in Codex
  tool loops, but hosted Responses-only tool item types are not equivalent to
  normal function tools.

Codex Desktop forwards three generic MCP tools to every model:

- `list_mcp_resources`
- `list_mcp_resource_templates`
- `read_mcp_resource`

It does **not** flatten individual MCP server tools into the function list.
That is a Codex client behavior, not a shim limitation. Shim-routed models
receive the same MCP tools as built-in OpenAI models. The model is expected to
call `list_mcp_resources` to discover what is available.

---

## Prompt catching and request interception

There are two useful interception layers:

### 1. Built-in request summaries

Every `/v1/responses` request is summarized into `.codex-shim/shim.log`. Use it
while debugging model routing, tool schemas, and prompt size:

```bash
tail -f .codex-shim/shim.log
```

The log is intentionally summary-level so it does not dump API keys or full
prompt bodies by default.

### 2. Local prompt-catching proxy in front of this shim

For deeper control, put a small local proxy in front of `codex-shim` and point
Codex at that proxy. That layer can inspect the full Responses request before
it reaches this shim, then forward to `http://127.0.0.1:8765/v1/responses`.

Common uses:

- inject a stable system/developer preamble;
- strip repeated boilerplate before it burns tokens;
- repair pseudo-tool text such as XML-ish `<invoke ...>` drafts into structured
  tool calls before Codex sees them;
- route some prompts to ChatGPT passthrough and others to BYOK models;
- redact or hash large file blobs in logs.

Minimal aiohttp forwarder shape:

```python
from aiohttp import ClientSession, web

UPSTREAM = "http://127.0.0.1:8765"

async def responses(request):
    body = await request.json()
    body = catch_prompt(body)          # mutate or record the Responses payload
    async with ClientSession() as s:
        async with s.post(f"{UPSTREAM}/v1/responses", json=body, headers=request.headers) as r:
            return web.Response(body=await r.read(), status=r.status, headers=r.headers)

def catch_prompt(body):
    # Keep this deterministic. Codex retries are much easier to debug when the
    # same input produces the same transformed payload.
    return body

app = web.Application()
app.router.add_post("/v1/responses", responses)
web.run_app(app, host="127.0.0.1", port=8766)
```

Then launch Codex with the shim provider URL set to `http://127.0.0.1:8766/v1`
instead of `8765`. Keep prompt catching outside `codex_shim/translate.py` unless
you want every BYOK route to share the same mutation policy.

---

## Benchmarking cost and speed

The right benchmark is an actual Codex task, not a synthetic hello-world
completion. Measure the same repository, prompt, model, and tool budget across
routes.

Suggested quick protocol:

1. Pick one real task that uses tools, e.g. "find the bug, edit the file, run
   the focused test".
2. Run it once through your baseline Codex route and once through `gpt-5.5`
   passthrough or your BYOK model.
3. Record wall time, request count, prompt tokens, output tokens, tool-call
   count, and final test result.
4. Compare only successful end-to-end runs.

Useful shell timing wrapper:

```bash
/usr/bin/time -f 'wall=%E cpu=%P max_rss_kb=%M' codex-shim codex -- "your task here"
```

The `--` separator is accepted and stripped by the wrapper. It is optional, but
it keeps task prompts that start with `-` from being parsed as wrapper flags.

A good report looks like:

```text
Oracle: same repo commit, same prompt, same focused test command
Baseline: 12 requests, 210k input tokens, 19k output tokens, 18m42s, test passed
Shim:      8 requests,  31k input tokens, 11k output tokens,  2m35s, test passed
Result:   6.8x fewer billed input tokens, 7.2x faster wall time
```

The exact multiplier depends on model, prompt catcher policy, repo size,
network path, and how often the agent calls tools.

---

## Commands

```text
codex-shim generate          regenerate catalog/config without starting daemon
codex-shim start             regenerate catalog and start local shim daemon
codex-shim enable            start daemon and write managed ~/.codex/config.toml block
codex-shim status            health check + model count
codex-shim stop              stop daemon
codex-shim disable           remove managed config block and stop daemon
codex-shim restart           stop, regenerate, and start daemon
codex-shim list              list generated slugs and upstream routes
codex-shim model list        list slugs currently usable in the picker
codex-shim model use <slug>  set the Desktop default model in managed config
codex-shim codex -- <args>   exec `codex` CLI through inline shim overrides
codex-shim app [path]        launch Codex Desktop through managed shim config
codex-shim patch-app         patch macOS Codex Desktop picker allowlist
codex-shim restore-app       restore macOS app.asar from patch backup

codex-app [path]             shortcut for `codex-shim app`
codex-model [list|<slug>]    shortcut for `codex-shim model …`
```

Global flags:

- `--settings <path>`: used by catalog/model/start/app/codex flows.
- `--port <port>`: used by daemon/provider flows.

`patch-app` and `restore-app` always target `/Applications/Codex.app`, do not
use `--settings`, and exit with a clear error on Windows/Linux.

---

## Model picker (web UI)

The shim exposes a small browser UI for switching the active model without
restarting the CLI:

- `GET /picker` — self-contained HTML page (dark theme) listing every model
  the shim currently knows about, with the active one highlighted.
- `GET /api/models` — JSON list backing the picker.
- `POST /api/switch` — `{"slug": "...", "restart_codex": true|false}`. The
  shim rewrites `model = "..."` and the `[model_providers.codex_shim]`
  `name = "..."` in `~/.codex/config.toml` so the Codex Desktop UI shows
  the selected model's display name (e.g. "Kimi K2.6") instead of the
  generic "Codex Shim" label, and optionally relaunches Codex Desktop
  (`open -a Codex` on macOS, `taskkill` + `Codex.exe` on Windows).

All picker routes are behind the same `Host`-header allowlist as the rest of
the shim, so a visited web page cannot drive them via DNS rebinding.

---

## Security and privacy

- The shim binds to `127.0.0.1` by default.
- The shim validates the `Host` header on every request and rejects anything
  that is not a loopback name (`127.0.0.1`, `localhost`, `::1`), the configured
  bind host, or an entry in `CODEX_SHIM_ALLOWED_HOSTS`. This blocks DNS-rebinding
  attacks where a web page you visit resolves its own domain to `127.0.0.1` and
  drives the shim with your credentials. If you deliberately bind to a
  non-loopback host, add the host(s) you reach it by to
  `CODEX_SHIM_ALLOWED_HOSTS` (comma-separated).
- API keys stay in your settings file; the generated catalog does not contain
  them.
- Request logs are summary-level by default and avoid full prompt/API-key dumps.
- ChatGPT passthrough reads `~/.codex/auth.json` at request time and forwards
  the access token only to ChatGPT's Codex endpoint.
- If you put a prompt-catching proxy in front of the shim, that proxy controls
  what it logs. Redact or hash large/private prompt bodies there.

---

## Limitations

- Codex internals and model-picker bundles change. The ASAR patch is version
  sensitive by nature.
- The ChatGPT passthrough endpoint is the endpoint current Codex builds use; it
  may move or change shape in a future Codex release.
- BYOK providers vary wildly in tool-call quality. The shim translates shapes;
  it cannot make an upstream model reliably emit valid tool-call JSON.
- Hosted Responses-only tools are highest fidelity on the ChatGPT passthrough
  path. BYOK routes get normal function-tool translation.
- The `bin/codex-app` and `bin/codex-model` shortcuts are POSIX shell scripts.
  In native Windows shells, use the installed `codex-shim` command instead.

---

## Troubleshooting

### Shim will not start

```bash
codex-shim status
tail -n 80 .codex-shim/shim.log
```

Common causes:

- Python is older than 3.11.
- `aiohttp` is not installed in the Python used by the wrapper.
- Port `8765` is already in use. Start on another port:

```bash
codex-shim --port 8766 restart
codex-shim --port 8766 app .
```

### `~/.codex-shim/models.json` is missing

That is fine for ChatGPT passthrough-only use, **provided** `~/.codex/auth.json`
has a valid `tokens.access_token`. In that case `codex-shim generate` writes a
catalog containing just `gpt-5.5`. If neither file is present, the catalog will
be empty and `codex-shim list` will exit non-zero with a hint to run
`codex login` or pass a compatible settings file:

```bash
codex-shim --settings /path/to/my-models.json generate
```

### `codex-shim list` exits 1 with "No models available"

You have neither configured models in `~/.codex-shim/models.json` nor a valid
Codex login. Pick one:

```bash
codex login                       # populate ~/.codex/auth.json
# or
codex-shim --settings /path/to/my-models.json list
```

### Codex shows only `default`

Run:

```bash
codex-shim generate
codex-shim model list
```

If the catalog contains your models but Desktop still hides them, apply the
macOS picker patch. On Windows Store/MSIX Desktop, the same allowlist can rewrite
the active model back to `gpt-5.5`; use `codex-shim codex -- ...` / Codex CLI for
BYOK routes, or a non-MSIX/Desktop build that can read the custom catalog without
rewriting the config.

### Windows proxy sends loopback traffic away from the shim

If `codex.exe` returns proxy/502 errors while the shim is healthy, a system proxy
may be intercepting `http://127.0.0.1:8765`. Set both uppercase and lowercase
bypass variables before launching Codex:

```powershell
$env:NO_PROXY = "127.0.0.1,localhost,::1"
$env:no_proxy = "127.0.0.1,localhost,::1"
```

`codex-shim app ...` and `codex-shim codex -- ...` set those entries for the
child process automatically.

### Model appears but requests 404

The selected slug is not in the current generated catalog. Regenerate after
editing `~/.codex-shim/models.json` or the file passed with `--settings`:

```bash
codex-shim generate
codex-model list
codex-model <slug>
```

### Upstream returns 401/403

The API key in your model settings file is wrong, expired, or missing a
provider-specific header. For ChatGPT passthrough, refresh Codex login so
`~/.codex/auth.json` contains a valid `tokens.access_token`.

### Tool calls turn into text

Use the ChatGPT passthrough path first to confirm Codex itself is sending tools.
If passthrough works but a BYOK route does not, the upstream probably lacks
native tool-call support or emits malformed streamed arguments. Check
`.codex-shim/shim.log` for the requested model and tool count.

### Images fail on a text-only model

Set `"noImageSupport": true` for that model in the settings file and regenerate
the catalog.

### Streaming hangs

Check whether the upstream streams correctly outside Codex. Then restart the
local daemon:

```bash
codex-shim restart
tail -f .codex-shim/shim.log
```

The server uses a long read timeout because real coding-agent turns can stream
for a while; a silent hang is usually upstream/network/provider behavior.

### macOS app crashes after patching

You repacked `app.asar` but did not update `ElectronAsarIntegrity` and re-sign,
or the patch hit the wrong JavaScript bundle. Restore and retry:

```bash
codex-shim restore-app
codex-shim patch-app
```

### Reset generated shim state

```bash
codex-shim stop
# Remove .codex-shim manually if you want a completely fresh generated state.
codex-shim generate
codex-shim start
```

---

## File layout

```text
codex_shim/             python source (server + cli + translation)
bin/codex-shim          main entrypoint
bin/codex-app           shortcut wrapping `codex-shim app`
bin/codex-model         shortcut wrapping `codex-shim model …`
.codex-shim/            generated catalog, config, logs, pid (gitignored)
tests/                  pytest suite
```

Config behavior:

- `codex-shim generate`, `start`, `stop`, `restart`, `list`, `status`, and
  `codex-shim codex -- ...` do not persistently modify `~/.codex/config.toml`.
- `codex-shim enable`, `codex-shim app`, and `codex-shim model use <slug>` write
  managed blocks to `~/.codex/config.toml`. If existing top-level Codex model
  keys are displaced, the managed block records them so disable can restore
  those keys without reverting unrelated config edits.
- `codex-shim disable` removes the managed blocks, restores displaced top-level
  model keys when present, and stops the daemon.

---

## Development checks

```bash
python3 -m pytest tests/
python3 -m compileall codex_shim/ -q
```

The tests cover settings/catalog generation, request translation, server
routing, and CLI settings-file UX. Add regression tests when changing
translation behavior; tool-call shape bugs are easy to miss by eyeballing
streams.

---

## Contributing

Good contributions include:

- new provider translation tests;
- captured stream fixtures for tricky tool-call/reasoning cases;
- compatibility notes for new Codex Desktop builds;
- safer picker patch detection for changed ASAR bundles;
- docs for known-good provider configs.

Before opening a PR, run the development checks above and include the Codex
Desktop/CLI version you tested.

---

## License

MIT — see `LICENSE`.

Codex Desktop is a trademark of OpenAI. This project is unaffiliated.
