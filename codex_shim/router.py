"""Auto Router — pick the right configured model for each task, automatically.

The Auto Router adds one virtual model (default slug ``codex-auto``) to the
shim. When Codex sends a request for that slug, the shim asks a small, cheap
*classifier* model — one of your own configured BYOK models — to score every
candidate model on how likely it is to complete *this* task correctly on the
first try (0.0–1.0). The shim then routes the real request to the **cheapest**
candidate whose score clears a quality bar (default ``0.7``), so trivial turns
go to a cheap model and hard turns escalate to your strongest one.

Design choices worth calling out:

* The classifier scores on **capability only** — it never sees price. Cost is
  applied afterward by the "cheapest among those good enough" tie-break, so the
  classifier can't be biased toward expensive models.
* Each decision is **cached per task** (keyed by the latest user message), so a
  task's follow-up tool-call round-trips reuse one classification instead of
  paying the classifier tax on every request.
* It **degrades safely** at every step: unknown candidate slugs are dropped, a
  missing/unavailable classifier falls back to the cheapest candidate
  deterministically, and any error falls back to the configured default. The
  request never breaks.

This module is intentionally network-free and pure: the actual classifier HTTP
call is injected as a callable by ``server.py`` so the scoring/selection logic
is fully unit-testable offline.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

DEFAULT_ROUTER_SLUG = "codex-auto"
DEFAULT_ROUTER_DISPLAY_NAME = "Auto (smart routing)"
DEFAULT_THRESHOLD = 0.7
DEFAULT_TIMEOUT = 12.0
DEFAULT_MAX_TOKENS = 600

_CACHE_MAX = 256


class RouterCache:
    """Bounded LRU cache mapping a task signal to a chosen candidate slug.

    Replaces the previous module-level ``dict`` whose only eviction was a bulk
    ``clear()`` at capacity (flip-flopping between full and empty, and causing a
    thundering herd of classifier calls). Each ``ShimServer`` owns its own
    instance so separate servers/sessions don't share routing state, and the
    cache is invalidated on model-config changes (e.g. picker model switch).

    Note: keys use Python's built-in ``hash()`` of the task text, which is not
    stable across process restarts (PYTHONHASHSEED); the cache is therefore only
    valid for a single process lifetime.
    """

    def __init__(self, maxsize: int = _CACHE_MAX) -> None:
        self.maxsize = maxsize
        self._store: "OrderedDict[str, str]" = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        if key not in self._store:
            return None
        self._store.move_to_end(key)
        return self._store[key]

    def put(self, key: str, value: str) -> None:
        self._store[key] = value
        self._store.move_to_end(key)
        while len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        return key in self._store


# Process-wide default cache, used when a caller does not supply its own.
# Kept for backwards compatibility with callers/tests that invoke resolve_auto
# without an explicit cache. New call sites (ShimServer) pass a per-instance
# cache so configuration changes can invalidate routing decisions.
_cache = RouterCache()

# An async callable that takes (system_prompt, user_content) and returns the
# classifier model's raw reply text.
ClassifyFn = Callable[[str, str], Awaitable[str]]


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------
def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def router_disabled_via_env() -> bool:
    """``CODEX_SHIM_DISABLE_ROUTER=1`` turns the router off even if enabled."""
    return _env_flag("CODEX_SHIM_DISABLE_ROUTER")


def router_log_enabled() -> bool:
    """``CODEX_SHIM_ROUTER_LOG=1`` logs every routing decision + raw scores."""
    return _env_flag("CODEX_SHIM_ROUTER_LOG")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RouterCandidate:
    slug: str
    cost: float = 1.0
    card: str = ""
    supports_images: bool = False


@dataclass(frozen=True)
class RouterConfig:
    enabled: bool
    slug: str
    display_name: str
    classifier: Optional[str]
    threshold: float
    default: Optional[str]
    cache: bool
    candidates: tuple[RouterCandidate, ...]
    timeout: float
    max_tokens: int

    @property
    def effective_enabled(self) -> bool:
        return self.enabled and not router_disabled_via_env()


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_router_config(settings_path: Path | str) -> Optional[RouterConfig]:
    """Read the optional top-level ``router`` block from the settings JSON.

    Returns ``None`` when the file is missing/invalid or has no ``router`` key,
    so the rest of the shim treats "no router configured" as the default.
    """
    path = Path(settings_path).expanduser()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("router")
    if not isinstance(raw, dict):
        return None

    candidates: list[RouterCandidate] = []
    for entry in raw.get("candidates") or []:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or entry.get("id") or "").strip()
        if not slug:
            continue
        candidates.append(
            RouterCandidate(
                slug=slug,
                cost=_as_float(entry.get("cost"), 1.0),
                card=str(entry.get("card") or "").strip(),
                supports_images=bool(entry.get("supports_images", False)),
            )
        )

    timeout = _as_float(raw.get("timeout"), DEFAULT_TIMEOUT)
    env_timeout = os.environ.get("CODEX_SHIM_ROUTER_TIMEOUT")
    if env_timeout:
        timeout = _as_float(env_timeout, timeout)

    max_tokens = int(_as_float(raw.get("max_tokens"), DEFAULT_MAX_TOKENS))
    env_max = os.environ.get("CODEX_SHIM_ROUTER_MAX_TOKENS")
    if env_max:
        max_tokens = int(_as_float(env_max, max_tokens))

    return RouterConfig(
        enabled=bool(raw.get("enabled", False)),
        slug=str(raw.get("slug") or raw.get("id") or DEFAULT_ROUTER_SLUG).strip() or DEFAULT_ROUTER_SLUG,
        display_name=str(raw.get("display_name") or DEFAULT_ROUTER_DISPLAY_NAME),
        classifier=(str(raw["classifier"]).strip() or None) if raw.get("classifier") else None,
        threshold=_as_float(raw.get("threshold"), DEFAULT_THRESHOLD),
        default=(str(raw["default"]).strip() or None) if raw.get("default") else None,
        cache=bool(raw.get("cache", True)),
        candidates=tuple(candidates),
        timeout=timeout,
        max_tokens=max_tokens,
    )


def filter_available(config: RouterConfig, available_slugs) -> list[RouterCandidate]:
    """Candidates whose backend is actually usable right now (route among the
    models that exist), never including the router's own virtual slug."""
    avail = set(available_slugs)
    return [c for c in config.candidates if c.slug in avail and c.slug != config.slug]


def router_is_active(config: Optional[RouterConfig], available_slugs) -> bool:
    return bool(config) and config.effective_enabled and len(filter_available(config, available_slugs)) >= 1


# ---------------------------------------------------------------------------
# Catalog / discovery entries for the virtual model
# ---------------------------------------------------------------------------
def router_models_entry(config: RouterConfig, created: int) -> dict[str, Any]:
    return {"id": config.slug, "object": "model", "created": created, "owned_by": "codex-shim-auto"}


def router_catalog_entry(config: RouterConfig) -> dict[str, Any]:
    return {
        "slug": config.slug,
        "display_name": config.display_name,
        "description": "Automatically routes each task to the cheapest configured model that can handle it.",
        "context_window": 400_000,
        "max_context_window": 400_000,
        "auto_compact_token_limit": 320_000,
        "truncation_policy": {"mode": "tokens", "limit": 64_000},
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Faster, lighter reasoning"},
            {"effort": "medium", "description": "Balanced speed and reasoning"},
            {"effort": "high", "description": "Deeper reasoning"},
            {"effort": "xhigh", "description": "Maximum reasoning where supported"},
        ],
        "default_reasoning_summary": "none",
        "reasoning_summary_format": "none",
        "supports_reasoning_summaries": False,
        "default_verbosity": "low",
        "support_verbosity": False,
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "supports_search_tool": False,
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
        "input_modalities": ["text", "image"],
        "supports_image_detail_original": True,
        "shell_type": "shell_command",
        "visibility": "list",
        "minimal_client_version": "0.0.1",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "priority": 12000,
        "prefer_websockets": False,
        "available_in_plans": ["free", "plus", "pro", "team", "business", "enterprise"],
        "base_instructions": "You are Codex, a coding agent. The active model is chosen automatically per task.",
        "model_messages": {
            "instructions_template": "You are Codex, a coding agent. The active model is chosen automatically per task.",
            "instructions_variables": {"model_name": config.display_name},
        },
    }


# ---------------------------------------------------------------------------
# Task signal extraction (works on Responses bodies and chat-completions bodies)
# ---------------------------------------------------------------------------
def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


def _latest_from_input(inp: Any) -> str:
    if isinstance(inp, str):
        return inp.strip()
    if not isinstance(inp, list):
        return ""
    for item in reversed(inp):
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user":
            text = _content_text(item.get("content"))
            if text.strip():
                return text.strip()
        elif item.get("type") in {"input_text", "text"}:
            text = _content_text(item)
            if text.strip():
                return text.strip()
    return ""


def _latest_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            text = _content_text(message.get("content"))
            if text.strip():
                return text.strip()
    return ""


def latest_user_text(body: dict[str, Any]) -> str:
    """The latest real user instruction — the task to classify.

    Skips tool round-trip items (``function_call_output`` etc.) so a task's
    follow-up turns keep the same cache key as the original ask.
    """
    text = _latest_from_input(body.get("input"))
    if text:
        return text
    return _latest_from_messages(body.get("messages"))


def _value_has_image(value: Any) -> bool:
    if isinstance(value, dict):
        vtype = str(value.get("type") or "")
        if vtype in {"input_image", "image_url"} or "image_url" in value:
            return True
        return any(_value_has_image(v) for v in value.values())
    if isinstance(value, list):
        return any(_value_has_image(v) for v in value)
    return False


def has_images(body: dict[str, Any]) -> bool:
    return _value_has_image(body.get("input")) or _value_has_image(body.get("messages"))


def task_signal(body: dict[str, Any]) -> dict[str, Any]:
    inp = body.get("input")
    if isinstance(inp, list):
        input_items = len(inp)
    elif inp:
        input_items = 1
    else:
        input_items = len(body.get("messages") or []) if isinstance(body.get("messages"), list) else 0
    tools = body.get("tools") or []
    return {
        "task": latest_user_text(body),
        "has_images": has_images(body),
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "input_items": input_items,
    }


# ---------------------------------------------------------------------------
# Classifier prompt + score parsing + selection
# ---------------------------------------------------------------------------
def build_system_prompt(candidates: list[RouterCandidate]) -> str:
    lines = [
        "You are a task-routing classifier for an AI coding agent.",
        "You are given a <session> describing the user's current task and a list",
        "of candidate models. For EACH candidate, output a score from 0.0 to 1.0:",
        "the probability that the model completes THIS task correctly on its first",
        "attempt, without errors or rework.",
        "",
        "You are NOT choosing a winner. A downstream system combines your scores",
        "with cost data you do not see to make the final pick. Be an accurate,",
        "well-calibrated, independent probability estimator for each model.",
        "",
        "Scoring guide:",
        "  0.0       cannot attempt (e.g. images required but unsupported) -- exact 0.0",
        "  0.1-0.3   will almost certainly fail; lacks the capability",
        "  0.4-0.6   real chance of failure; touches a known weakness or is uncertain",
        "  0.7-0.8   likely success; handles this category well",
        "  0.9-1.0   near-certain success; well within demonstrated ability",
        "Use the full range. A short prompt is NOT necessarily an easy task -- hidden",
        "complexity (multi-file edits, debugging, niche domains, strict correctness)",
        "should pull scores down for weaker models. Default to ~0.5-0.6 when unsure.",
        "",
        "Candidate models:",
    ]
    for c in candidates:
        card = c.card or "General-purpose model. No capability card provided."
        lines.append("- slug: %s" % c.slug)
        lines.append("  images: %s" % ("yes" if c.supports_images else "no"))
        lines.append("  capability: %s" % card)
    schema = {"scores": {c.slug: 0.0 for c in candidates}, "reasoning": "one short sentence"}
    lines += [
        "",
        "Respond with ONE JSON object, no prose, no code fence, exactly this shape:",
        json.dumps(schema, ensure_ascii=False),
        'Every candidate slug above MUST appear in "scores". Each value in [0.0, 1.0].',
    ]
    return "\n".join(lines)


def build_user_content(signal: dict[str, Any], candidates: list[RouterCandidate]) -> str:
    task = signal["task"] or "(no explicit instruction; infer from context)"
    if len(task) > 6000:  # head+tail, keep the classifier call cheap
        task = task[:3000] + "\n...\n" + task[-3000:]
    return "\n".join(
        [
            "<session>",
            "  images_present: %s" % ("yes" if signal["has_images"] else "no"),
            "  tools_available: %d" % signal["tool_count"],
            "  input_items: %d" % signal["input_items"],
            "  current_task: |",
            "\n".join("    " + ln for ln in task.splitlines()) or "    (empty)",
            "</session>",
            "",
            "Score these candidate slugs: %s" % ", ".join(c.slug for c in candidates),
        ]
    )


def _clamp01(value: Any) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if value < 0 else (1.0 if value > 1 else value)


def parse_scores(text: str, candidate_slugs: list[str]) -> dict[str, float]:
    """Pull the ``{"scores": {...}}`` object out of the classifier's reply."""
    if not text:
        return {}
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    for stop in (end, text.find("}", start)):  # greedy object, then first object
        if stop == -1 or stop <= start:
            continue
        try:
            obj = json.loads(text[start : stop + 1])
        except Exception:
            continue
        scores = obj.get("scores") if isinstance(obj, dict) else None
        if isinstance(scores, dict):
            return {slug: _clamp01(scores.get(slug, 0)) for slug in candidate_slugs}
    return {}


def pick_candidate(
    scores: dict[str, float],
    candidates: list[RouterCandidate],
    threshold: float,
    has_image_task: bool,
) -> tuple[Optional[str], float, str]:
    """Cheapest candidate whose score clears the bar; image-incapable models are
    hard-zeroed when the task has images; if none clear the bar, take the best."""
    scored: list[tuple[RouterCandidate, float]] = []
    for c in candidates:
        score = scores.get(c.slug, 0.0)
        if has_image_task and not c.supports_images:
            score = 0.0
        scored.append((c, score))
    viable = [(c, s) for (c, s) in scored if s >= threshold]
    if viable:
        winner = min(viable, key=lambda cs: (float(cs[0].cost or 0), -cs[1]))
        return winner[0].slug, winner[1], "score>=%.2f, cheapest" % threshold
    best = max(scored, key=lambda cs: cs[1], default=None)
    if best and best[1] > 0:
        return best[0].slug, best[1], "below bar; highest score"
    return None, 0.0, "no usable score"


def fallback_slug(
    config: RouterConfig,
    candidates: list[RouterCandidate],
    has_image_task: bool = False,
) -> Optional[str]:
    """Deterministic, classifier-free choice.

    When ``has_image_task`` is False (default), pick the configured default
    if it is among the candidates, otherwise the cheapest candidate. This is
    the original behaviour and is preserved so existing call sites do not
    need to change semantics.

    When ``has_image_task`` is True, ignore candidates whose
    ``supports_images`` is False — sending an image task to a text-only
    upstream produces a confusing 400 (`unknown variant ``image_url`` `).
    Among the remaining vision-capable candidates, prefer the configured
    default if it qualifies, otherwise the cheapest. If no candidate
    supports images, return None so the caller can surface the failure
    explicitly rather than silently routing into an upstream that will
    reject the body.
    """
    pool = (
        [c for c in candidates if c.supports_images] if has_image_task else list(candidates)
    )
    if not pool:
        return None
    if config.default and any(c.slug == config.default for c in pool):
        return config.default
    return min(pool, key=lambda c: float(c.cost or 0)).slug


def _cache_key(signal: dict[str, Any]) -> str:
    return "%s|%s" % (signal["has_images"], hash(signal["task"]))


def reset_cache(cache: Optional[RouterCache] = None) -> None:
    """Clear a router cache (the process-wide default unless one is given)."""
    (cache if cache is not None else _cache).clear()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def resolve_auto(
    config: RouterConfig,
    candidates: list[RouterCandidate],
    body: dict[str, Any],
    classify: Optional[ClassifyFn],
    *,
    log: Optional[Callable[[str], None]] = None,
    cache: Optional[RouterCache] = None,
) -> tuple[Optional[str], dict[str, Any]]:
    """Return the concrete candidate slug the Auto Router selects for this
    request (or ``None`` if nothing is routable). Never raises.

    ``cache`` lets the caller supply a per-instance :class:`RouterCache`; when
    omitted the process-wide default is used."""

    cache = cache if cache is not None else _cache

    def _log(message: str) -> None:
        if log:
            log(message)

    try:
        if not candidates:
            return None, {"reason": "no candidates"}
        if len(candidates) == 1:
            return candidates[0].slug, {"reason": "single candidate", "scores": {}}

        signal = task_signal(body)
        key = _cache_key(signal)
        if config.cache:
            cached = cache.get(key)
            if cached and any(c.slug == cached for c in candidates):
                _log("[router] cache-hit -> %s" % cached)
                return cached, {"reason": "cache", "scores": {}}

        if classify is None:
            pick = fallback_slug(config, candidates, has_image_task=signal["has_images"])
            _log("[router] no classifier -> deterministic %s" % pick)
            return pick, {"reason": "no classifier", "scores": {}}

        system_prompt = build_system_prompt(candidates)
        user_content = build_user_content(signal, candidates)
        try:
            raw = await classify(system_prompt, user_content)
        except Exception as exc:  # noqa: BLE001 - any classifier failure is non-fatal
            pick = fallback_slug(config, candidates, has_image_task=signal["has_images"])
            _log("[router] classifier failed (%s); falling back to %s" % (exc, pick))
            return pick, {"reason": "classifier error", "scores": {}}

        scores = parse_scores(raw, [c.slug for c in candidates])
        pick, score, why = pick_candidate(scores, candidates, config.threshold, signal["has_images"])
        if not pick:
            pick = fallback_slug(config, candidates, has_image_task=signal["has_images"])
            why = "empty scores; fallback"
            score = 0.0
        if config.cache and pick:
            cache.put(key, pick)
        _log(
            "[router] -> %s (score=%.2f; %s) scores=%s"
            % (pick, score, why, json.dumps({c.slug: round(scores.get(c.slug, 0.0), 2) for c in candidates}))
        )
        return pick, {"reason": why, "score": score, "scores": scores}
    except Exception as exc:  # noqa: BLE001 - routing must never break a request
        _log("[router] unexpected error: %s" % exc)
        return None, {"reason": "error", "scores": {}}
