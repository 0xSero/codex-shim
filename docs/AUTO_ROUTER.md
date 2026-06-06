# Auto Router — pick the right model for every task, automatically

The Auto Router adds one extra entry to the Codex picker: **`Auto (smart
routing)`** (slug `codex-auto`). Choose it and the shim decides, *per task*,
which of your configured models to use — sending trivial turns to a cheap model
and hard turns to your strongest one. The goal is to keep frontier-level quality
while cutting cost, running entirely on the models *you* already configure.

It is **off unless you configure it**, fully local (standard library only on top
of the shim's existing `aiohttp` dependency), and degrades safely: if anything
goes wrong it falls back to a sensible default and never breaks a request.

---

## How it works (30 seconds)

1. You pick **`Auto (smart routing)`** in the Codex picker (or
   `codex-shim model use codex-auto`).
2. On each new task, the shim sends a *tiny* scoring request to a cheap
   **classifier** model you nominate. The classifier reads the task plus a short
   **capability card** for each candidate and returns a score `0.0–1.0` per
   candidate — the probability that model nails the task on the first try.
3. The shim picks the **cheapest candidate whose score clears `threshold`**
   (default `0.7`). If none clear it, it takes the highest-scoring one.
4. The real request is routed to that backend. The decision is **cached for the
   rest of that task** (its tool-call round-trips reuse it), so you pay the
   classifier cost once per task, not once per request.

```
                         ┌──────────────────────────────┐
  you pick "Auto"  ──▶   │  classifier (cheap model)     │
                         │  scores each candidate 0–1    │
                         └───────────────┬──────────────┘
                                         │ scores
                                         ▼
              cheapest candidate with score ≥ threshold (else best)
                                         │
                                         ▼
        real /v1/responses ──▶  that backend (OpenAI chat, Anthropic, …)
```

The classifier **never sees price**. It scores on capability only; cost is
applied afterward by the "cheapest among those good enough" rule. That keeps the
classifier from being biased toward expensive models.

---

## Prove it works (offline, no keys)

A self-contained demo spins up a mock multi-backend server, starts the **real**
codex-shim server with the router enabled, and runs real `/v1/responses` tasks
of increasing difficulty through it — showing which backend each one hit:

```bash
python3 examples/auto_router_demo.py
```

```text
#  Task                                         Classifier scores                Routed to      Cost
1  add a docstring to the foo() helper          cheap=0.90 mid=0.92 strong=0.95  cheap          $0.3
2  write a CRUD REST endpoint with tests        cheap=0.50 mid=0.85 strong=0.95  mid            $1.0
3  refactor the auth module across 8 files ...  cheap=0.40 mid=0.55 strong=0.95  strong         $5.0
4  what does this screenshot show?              cheap=0.90 mid=0.92 strong=0.95  strong         $5.0  <- only image-capable
5  (repeat task #1)                             served from cache                cheap          $0.3  <- classifier not re-called
RESULT: PASS
```

Row 4: even though the cheap model *scored* 0.90, the task has an image, so the
shim hard-zeroes the models that can't see and the only vision-capable candidate
wins. Row 5 shows the per-task cache: the repeat is served without re-calling the
classifier.

The behavior is locked down by 48 offline tests:

```bash
python3 -m pytest tests/test_router.py tests/test_router_integration.py -q
```

They cover config loading, task-signal extraction, score parsing (prose, code
fences, partial scores), cheapest-among-viable selection, image hard-rejects,
the per-task cache across an agent tool-loop, OpenAI **and** Anthropic
classifiers, streaming, compaction, the chat-completions endpoint, the exact
HTTP payload/headers the shim sends to the classifier, threshold tuning,
graceful fallback on garbage classifier output, candidate availability gating,
concurrency, and the picker switch.

---

## Quick start

Add a `router` block to `~/.codex-shim/models.json` (the same file that holds
your models):

```jsonc
{
  "models": [
    { "slug": "minimax-m3", "model": "MiniMax-M3", "provider": "openai",
      "base_url": "https://api.example.com/v1", "api_key": "${MINIMAX_KEY}",
      "display_name": "MiniMax M3" },
    { "slug": "opus", "model": "claude-opus-4-7", "provider": "anthropic",
      "base_url": "https://api.anthropic.com/v1", "api_key": "${ANTHROPIC_KEY}",
      "display_name": "Claude Opus 4.7" }
  ],
  "router": {
    "enabled": true,
    "slug": "codex-auto",
    "display_name": "Auto (smart routing)",
    "classifier": "minimax-m3",
    "threshold": 0.7,
    "default": "minimax-m3",
    "cache": true,
    "candidates": [
      { "slug": "minimax-m3", "cost": 0.3, "supports_images": false,
        "card": "Very cheap, fast. Strong on single-file edits, codegen from a clear spec, simple refactors, data wrangling. Weak on big multi-file refactors, subtle debugging, niche domains." },
      { "slug": "opus", "cost": 5.0, "supports_images": true,
        "card": "Frontier reasoning + agentic coding. Best for the hardest work: large multi-file refactors, subtle debugging, architecture, long autonomous workflows, and image tasks." }
    ]
  }
}
```

Then:

1. `codex-shim generate` — the catalog now includes `codex-auto`.
2. `codex-shim start` (or `enable` / `app`), pick **`Auto (smart routing)`**.

That's it. Any candidate whose backend isn't usable right now (no API key, or a
passthrough that isn't logged in) is **skipped automatically**, so the router
keeps working with whatever subset remains.

---

## Configuration reference

The `router` block in your settings file:

| Field | Meaning |
|-------|---------|
| `enabled` | Turn the router on/off. Also overridable at runtime with `CODEX_SHIM_DISABLE_ROUTER=1`. |
| `slug` | The picker slug for the Auto entry. Defaults to `codex-auto`. |
| `display_name` | Picker label. Defaults to `Auto (smart routing)`. |
| `classifier` | A model **slug** (one of your configured BYOK models) used as the scorer. Use your cheapest, fastest model. Must be an `openai`/`generic-chat-completion-api` or `anthropic` model with a key. If it's missing/unavailable, the router falls back to the cheapest candidate **without scoring**. |
| `threshold` | `0–1`. The bar a candidate's score must clear. **Lower = more aggressive savings** (cheap models win more often); **higher = escalate to strong models sooner**. `0.7` is a good start. |
| `default` | Candidate slug used when classification can't run at all. Defaults to the cheapest candidate. |
| `cache` | Reuse one classification across a task's follow-up tool calls. Recommended. |
| `timeout` | Seconds to wait for the classifier before falling back. Default `12`. Overridable with `CODEX_SHIM_ROUTER_TIMEOUT`. |
| `max_tokens` | Max tokens for the classifier reply (it only emits small JSON). Default `600`. Overridable with `CODEX_SHIM_ROUTER_MAX_TOKENS`. |
| `candidates[].slug` | Must match a model slug, ChatGPT passthrough slug, or Cursor passthrough slug. Unusable ones are silently skipped. |
| `candidates[].cost` | A **relative** weight; units don't matter, only the ordering. Lowest cost among the "good enough" candidates wins. |
| `candidates[].supports_images` | If `false`, the candidate is hard-scored `0` whenever the task includes images. |
| `candidates[].card` | The capability description the classifier reads. **This is the single most important field** — be honest about strengths and weaknesses; that's what makes routing smart. |

### The capability card is where the intelligence lives

The classifier only knows about a model what the card tells it. A good card lists
concrete strengths *and* weaknesses:

> *"Cheap, fast generalist. Good at standard servers/CRUD, data processing,
> conventional multi-file edits, and tool/test loops. Less reliable on hard
> algorithms, exotic build systems, or long autonomous debugging."*

Vague cards ("a good model") produce vague routing. Specific cards produce sharp
routing.

---

## Tuning & knobs

| Env var | Default | What it does |
|---------|---------|--------------|
| `CODEX_SHIM_DISABLE_ROUTER` | unset | Set `1` to disable the router even if `enabled` in config. |
| `CODEX_SHIM_ROUTER_TIMEOUT` | `12` | Seconds to wait for the classifier before falling back. |
| `CODEX_SHIM_ROUTER_MAX_TOKENS` | `600` | Max tokens for the classifier's reply. |
| `CODEX_SHIM_ROUTER_LOG` | unset | Set `1` to log every routing decision + the raw scores. |

**Watch it decide:** run with `CODEX_SHIM_ROUTER_LOG=1` and tail the shim log
(`.codex-shim/shim.log`). Each task prints a line like:

```
[router] -> opus (score=0.93; score>=0.70, cheapest) scores={"minimax-m3": 0.55, "opus": 0.93}
[router] codex-auto -> opus
```

That log is the honest way to tell whether routing is helping you — watch which
tasks escalate and tune `threshold` / cards accordingly.

---

## Failure behavior (it never breaks your request)

| Situation | What happens |
|-----------|--------------|
| Classifier times out / errors | Falls back to `default` (or cheapest candidate). |
| Classifier slug not configured/usable | Deterministic: routes to the cheapest candidate, no scoring. |
| A candidate's backend isn't usable | That candidate is skipped; routing continues with the rest. |
| Only one candidate available | Routes straight to it (no classifier call). |
| Task has images, candidate can't | That candidate is scored `0` (can't win). |
| Router disabled but `codex-auto` somehow requested | The slug isn't advertised; an explicit request falls through to the cheapest candidate. |

---

## Notes

- The Auto Router targets Codex's primary path, `POST /v1/responses` (and
  `/v1/responses/compact`). It also applies to `/v1/chat/completions`.
- Candidates can be BYOK models **or** passthrough slugs (`gpt-5.5`,
  `composer-2-5`) when those are available — the router routes to whatever is
  usable.
- Per-task caching means you pay the classifier tax once per task, not once per
  tool-call round-trip. Every decision is logged with `CODEX_SHIM_ROUTER_LOG=1`,
  so you can measure whether routing is actually helping before trusting it.
