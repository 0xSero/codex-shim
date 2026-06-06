# Auto Router 配置与操作手册

本文记录本机 `codex-shim` 的 Auto Router 配置、启用方式、日志验证、Ark coding plan 配额耗尽时的行为，以及常用回滚/调参操作。

> 注意：本文只记录 slug、策略和命令；不要把 API key 写进文档。真实配置文件在 `~/.codex-shim/models.json`。

---

## 1. 当前配置目标

Auto Router 在 Codex Desktop 的 picker 中显示为：

- display name：`Auto (smart routing)`
- slug：`codex-auto`

选择它后，shim 会先让一个便宜模型作为 **classifier** 给候选模型打分，然后把真实请求路由到合适的候选模型。

本机当前策略：

1. **分类器走官方 DeepSeek**：`deepseek-v4-flash`
   - 目的：分类决策不依赖 Ark coding plan 的剩余额度。
2. **正常情况下优先用 Ark**：Ark 候选成本更低，分类器认为能力足够时会优先选 Ark。
3. **Ark 全断时兜底走官方 DeepSeek Flash**：`default = deepseek-v4-flash`。
4. **含图任务走 Kimi**：`kimi-k2-6` 是当前唯一 `supports_images=true` 的候选。

---

## 2. 当前候选模型

| slug | 通道 | cost | supports_images | 用途 |
|---|---:|---:|---:|---|
| `deepseek-v4-flash-ark` | Ark coding plan | `0.3` | false | 简单、便宜、快速的文本任务 |
| `deepseek-v4-pro-ark` | Ark coding plan | `3.0` | false | 复杂文本任务、长上下文、多文件重构 |
| `kimi-k2-6` | Ark coding plan | `2.0` | true | 截图、UI、图像输入任务 |
| `deepseek-v4-flash` | DeepSeek 官方 API | `0.5` | false | Ark 不可用时的默认兜底 |
| `deepseek-v4-pro` | DeepSeek 官方 API | `4.0` | false | 官方通道的复杂文本兜底候选 |

`cost` 是相对成本，不要求等于真实价格。Router 的规则是：

1. 分类器只看候选能力卡片，不看 cost。
2. 分数达到 `threshold` 的候选里选 cost 最低的。
3. 如果没有候选达到阈值，就选分数最高的。
4. 如果分类器失败或没有可用分数，就选 `default`。

当前阈值：`threshold = 0.7`。

---

## 3. 推荐配置片段

位置：`~/.codex-shim/models.json`

把下面的 `router` 块放在 `models` 同级。`models` 里的真实模型条目继续保留原样。

```jsonc
{
  "models": [
    // ... 已有模型配置，不要在文档里记录真实 API key ...
  ],
  "router": {
    "enabled": true,
    "slug": "codex-auto",
    "display_name": "Auto (smart routing)",
    "classifier": "deepseek-v4-flash",
    "threshold": 0.7,
    "default": "deepseek-v4-flash",
    "cache": true,
    "candidates": [
      {
        "slug": "deepseek-v4-flash-ark",
        "cost": 0.3,
        "supports_images": false,
        "card": "Cheap, fast (128k ctx) via Ark coding plan. Strong on single-file edits, code gen from clear spec, simple refactors, glue scripts, shell one-liners. Weak on large multi-file refactors, subtle bugs, architecture decisions."
      },
      {
        "slug": "deepseek-v4-pro-ark",
        "cost": 3.0,
        "supports_images": false,
        "card": "Strong reasoning (1M ctx) via Ark coding plan. Best for hard text work: multi-file refactors, bug hunts, architecture, autonomous loops, dense code review. Cannot see images."
      },
      {
        "slug": "kimi-k2-6",
        "cost": 2.0,
        "supports_images": true,
        "card": "Vision-capable + reasoning (256k ctx). Required for any task with screenshots, diagrams, UI mockups. Good general coder on text too but less strong than DeepSeek pro on pure-code hard work."
      },
      {
        "slug": "deepseek-v4-flash",
        "cost": 0.5,
        "supports_images": false,
        "card": "Cheap, fast (128k ctx) via DeepSeek official API. Same capability as deepseek-v4-flash-ark but different access channel with separate quota."
      },
      {
        "slug": "deepseek-v4-pro",
        "cost": 4.0,
        "supports_images": false,
        "card": "Strong reasoning (1M ctx) via DeepSeek official API. Same capability as deepseek-v4-pro-ark but different access channel with separate quota."
      }
    ]
  }
}
```

---

## 4. 启用流程

编辑 `~/.codex-shim/models.json` 后执行：

```bash
codex-shim generate
CODEX_SHIM_ROUTER_LOG=1 codex-shim restart
```

`generate` 成功时应该看到类似输出：

```text
Generated 7 model entries:
  auto router: codex-auto (Auto (smart routing))
  catalog: /Users/maerun/Documents/Third-Party/codex-shim/.codex-shim/custom_model_catalog.json
  config:  /Users/maerun/Documents/Third-Party/codex-shim/.codex-shim/config.toml
```

然后在 Codex Desktop 的 picker 里选择：

```text
Auto (smart routing)
```

如果 picker 里看不到这一项：

1. 先重开 Codex Desktop，让它重新读取 catalog。
2. 如果仍没有，检查 `patch-app` 是否仍有效。

---

## 5. 日志验证

日志位置：

```bash
/Users/maerun/Documents/Third-Party/codex-shim/.codex-shim/shim.log
```

实时查看：

```bash
tail -f /Users/maerun/Documents/Third-Party/codex-shim/.codex-shim/shim.log
```

选择 `Auto (smart routing)` 后，发起一次对话，日志应出现两类信息。

### 5.1 Router 决策日志

示例：

```text
[router] -> deepseek-v4-flash-ark (score=0.91; score>=0.70, cheapest) scores={"deepseek-v4-flash-ark": 0.91, "deepseek-v4-pro-ark": 0.94, "kimi-k2-6": 0.82, "deepseek-v4-flash": 0.91, "deepseek-v4-pro": 0.94}
[router] codex-auto -> deepseek-v4-flash-ark
```

含义：

- 第一行：分类器给各候选打分，并解释为什么选中某个 slug。
- 第二行：虚拟模型 `codex-auto` 被改写成真实模型。

### 5.2 实际请求路由日志

示例：

```text
[route] slug='deepseek-v4-flash-ark' provider='openai' upstream_model='deepseek-v4-flash' url=https://ark.cn-beijing.volces.com/api/coding/v3/chat/completions
```

这行说明真实请求最终发去了哪里。

---

## 6. Ark coding plan 配额耗尽时的行为

当前配置已经做了一个重要保护：

```jsonc
"classifier": "deepseek-v4-flash",
"default": "deepseek-v4-flash"
```

也就是说，分类器和默认兜底都走 DeepSeek 官方 API，不依赖 Ark。

### 6.1 能解决什么

如果分类器失败、没有可用分数、或路由器内部出错，Auto Router 会走：

```text
default -> deepseek-v4-flash
```

这会绕开 Ark，直接走官方 API。

### 6.2 不能解决什么

上游 Auto Router 当前是 **routes once, no retry**：

1. classifier 打分成功；
2. router 选中了 `deepseek-v4-pro-ark` 或 `deepseek-v4-flash-ark`；
3. 真实请求发到 Ark 后遇到 429 / quota exhausted；
4. shim 不会自动重试官方候选，而是把错误返回给 Codex Desktop。

因此，当前配置降低了 Ark 配额问题的影响，但不是完整熔断器。

### 6.3 如果 Ark 当天已用完

临时最稳做法：在 picker 里手动切到官方 DeepSeek 模型，或者临时禁用 Auto Router：

```bash
CODEX_SHIM_DISABLE_ROUTER=1 codex-shim restart
```

禁用后 `codex-auto` 不再出现在 picker 中，所有请求按你手动选择的模型走。

---

## 7. 常用操作

### 7.1 重启并保留 router 日志

```bash
CODEX_SHIM_ROUTER_LOG=1 codex-shim restart
```

确认进程环境里带了这个变量：

```bash
PID=$(cat /Users/maerun/Documents/Third-Party/codex-shim/.codex-shim/shim.pid)
ps eww "$PID" | tr ' ' '\n' | grep CODEX_SHIM_ROUTER_LOG
```

### 7.2 重新生成 catalog

```bash
codex-shim generate
```

修改 `models.json` 后需要跑一次，确保 `codex-auto` 写进 catalog。

### 7.3 急停 Auto Router

```bash
CODEX_SHIM_DISABLE_ROUTER=1 codex-shim restart
```

恢复：

```bash
CODEX_SHIM_ROUTER_LOG=1 codex-shim restart
```

### 7.4 回滚到启用 Auto Router 前的配置

启用前已有备份：

```bash
~/.codex-shim/models.json.before-auto-router-20260606-162120
```

回滚：

```bash
cp ~/.codex-shim/models.json.before-auto-router-20260606-162120 ~/.codex-shim/models.json
codex-shim restart
```

### 7.5 检查当前配置摘要

```bash
python3 - <<'PY'
import json
from pathlib import Path
p = Path.home() / '.codex-shim' / 'models.json'
r = json.loads(p.read_text()).get('router', {})
print('enabled:', r.get('enabled'))
print('slug:', r.get('slug'))
print('classifier:', r.get('classifier'))
print('default:', r.get('default'))
print('threshold:', r.get('threshold'))
print('cache:', r.get('cache'))
print('candidates:')
for c in r.get('candidates', []):
    print(' ', c.get('slug'), 'cost=', c.get('cost'), 'images=', c.get('supports_images'))
PY
```

---

## 8. 调参建议

### 8.1 简单任务经常被路由到 Pro

把阈值调高，例如：

```jsonc
"threshold": 0.85
```

含义：只有分类器非常确定廉价模型也能完成时，才会选廉价模型；否则更容易升级到强模型。

> 反直觉提醒：阈值越高，不是越省钱，而是越保守，越容易上强模型。

### 8.2 太多任务都走 Flash，质量不够

把候选 card 里 Pro 的能力描述写得更明确，或者把 threshold 适当调低/调高要结合日志判断。

优先看日志里的 `scores={...}`：

- 如果 Pro 分数高但 Flash 也过线，且 Flash 更便宜，所以选 Flash：说明 cost 规则在起作用。
- 如果 Pro 分数并不高：说明 card 没让分类器理解 Pro 的适用场景。

### 8.3 想让 Auto 完全避开 Ark

把 candidates 里的 Ark 候选删除，只保留官方 DeepSeek 和 vision 候选：

```jsonc
"candidates": [
  { "slug": "deepseek-v4-flash", ... },
  { "slug": "deepseek-v4-pro", ... },
  { "slug": "kimi-k2-6", ... }
]
```

缺点：Auto Router 不再帮你利用 Ark coding plan 的低成本。

---

## 9. 当前已知限制

1. **真实请求失败后不自动二次重试**：选中 Ark 后如果真实请求返回 quota 错误，当前上游 Auto Router 不会自动改走官方候选。
2. **含图 fallback 仍依赖 Ark**：当前唯一 vision 候选 `kimi-k2-6` 在 Ark 通道上。如果 Ark 当天用完，含图任务没有官方 vision fallback。
3. **Router log 是运行时环境变量**：下次普通 `codex-shim restart` 不带 `CODEX_SHIM_ROUTER_LOG=1` 时，路由仍工作，但不打印 `[router]` 决策日志。
4. **`codex-auto` 出现在 picker 依赖 Desktop catalog 刷新**：如果刚生成 catalog 后 picker 不显示，先重开 Codex Desktop。

---

## 10. 一句话排障

| 现象 | 优先检查 |
|---|---|
| picker 没有 `Auto (smart routing)` | 是否跑过 `codex-shim generate`；是否需要重开 Desktop；patch-app 是否有效 |
| 有 Auto 但没有 `[router]` 日志 | shim 是否用 `CODEX_SHIM_ROUTER_LOG=1` 启动 |
| Auto 总是走 Flash | 看 `scores={...}`；确认 Pro 的 card 是否足够明确；确认 threshold/cost |
| Auto 选 Ark 后请求失败 | 很可能 Ark coding plan 配额耗尽；手动切官方模型或临时禁用 Auto |
| 含图任务失败 | 当前 vision 候选在 Ark；Ark 用完时没有独立 vision fallback |
