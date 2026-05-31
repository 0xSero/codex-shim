# Smart Vision Routing for codex-shim

This fork adds intelligent vision routing capabilities to codex-shim, allowing automatic switching between vision-capable and text-only models within the same conversation.

## What's New

### Smart Vision Routing

Automatically routes requests based on visual content:
- **Image requests** → Routes to vision-capable models (e.g., Kimi, Claude with vision)
- **Text-only requests** → Routes to default text model (e.g., DeepSeek)
- **History filtering** → Strips images from history when switching to text-only models

This allows you to:
- Use cheaper/faster text models for most conversations
- Automatically switch to vision models only when needed
- Preserve assistant's image descriptions for context continuity

## Installation

Same as upstream codex-shim:

```bash
git clone https://github.com/YOUR_USERNAME/codex-shim.git
cd codex-shim
python3 -m pip install --user -e .
```

## Configuration

### 1. Configure Models

Create `~/.codex-shim/models.json` with both vision and text-only models:

```json
{
  "models": [
    {
      "model": "deepseek-chat",
      "provider": "openai",
      "base_url": "https://api.deepseek.com",
      "api_key": "sk-your-deepseek-key",
      "display_name": "DeepSeek V3",
      "max_context_limit": 64000,
      "no_image_support": true
    },
    {
      "model": "moonshot-v1-128k",
      "provider": "openai",
      "base_url": "https://api.moonshot.cn/v1",
      "api_key": "sk-your-kimi-key",
      "display_name": "Kimi K1.5",
      "max_context_limit": 128000,
      "no_image_support": false
    }
  ]
}
```

**Key field:** `no_image_support`
- `true` → Text-only model (DeepSeek, most local models)
- `false` or omitted → Vision-capable model (Kimi, Claude, GPT-4V)

### 2. Enable Vision Routing

Set the environment variable:

```bash
export CODEX_SHIM_VISION_ROUTING=true
```

Or add to your shell profile (`~/.bashrc`, `~/.zshrc`):

```bash
echo 'export CODEX_SHIM_VISION_ROUTING=true' >> ~/.zshrc
```

### 3. Start the Shim

```bash
codex-shim generate
codex-shim start
codex-shim app
```

## How It Works

### Example Conversation Flow

```
Turn 1: User sends image + "What is this?"
→ Detects image in current turn
→ Routes to Kimi (vision model)
→ Kimi responds: "This is an orange cat sitting on a sofa"

Turn 2: User sends "What breed is it?"
→ No image in current turn
→ Routes to DeepSeek (text model)
→ History is modified:
  - Image replaced with: "[Image from previous turn - see assistant's description above]"
  - Assistant's description preserved: "This is an orange cat..."
→ DeepSeek responds based on the description: "Based on the description, it's likely an orange tabby..."
```

### Routing Logic

1. **Check current turn** for images (not entire history)
2. **If image present** → Use first vision-capable model
3. **If no image** → Use default model (requested model or first in list)
4. **If default is text-only** → Strip images from history, preserve text

### What Gets Stripped

When routing to text-only models, these are replaced with text placeholders:
- `input_image` items
- Images in message content
- Computer use screenshots (`computer_call_output`)
- Visual function call outputs

**What's preserved:**
- All text content
- Assistant responses (including image descriptions)
- Tool call results (text portions)

## Configuration Options

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEX_SHIM_VISION_ROUTING` | `false` | Enable smart vision routing |
| `CODEX_SHIM_VISION_RECENT_TURNS` | `3` | Number of recent turns to check for images |
| `CODEX_SHIM_DEFAULT_TEXT_MODEL` | (first model) | Preferred model for text-only requests |

### Example with Custom Settings

```bash
export CODEX_SHIM_VISION_ROUTING=true
export CODEX_SHIM_DEFAULT_TEXT_MODEL=deepseek-chat
export CODEX_SHIM_VISION_RECENT_TURNS=5

codex-shim start
```

## Disabling Vision Routing

To use the original codex-shim behavior:

```bash
unset CODEX_SHIM_VISION_ROUTING
# or
export CODEX_SHIM_VISION_ROUTING=false
```

Then restart the shim:

```bash
codex-shim restart
```

## Troubleshooting

### Images still causing errors

**Problem:** Text-only model receives image and returns error.

**Solution:** Make sure vision routing is enabled:
```bash
echo $CODEX_SHIM_VISION_ROUTING  # Should output: true
```

If not set, enable it and restart:
```bash
export CODEX_SHIM_VISION_ROUTING=true
codex-shim restart
```

### Always using vision model

**Problem:** Even text-only requests use the vision model.

**Possible causes:**
1. No text-only models configured → Add a model with `"no_image_support": true`
2. Images in recent history → This is expected behavior for context continuity

### Assistant descriptions not preserved

**Problem:** Text model doesn't have context about previous images.

**Check:** Verify assistant responses are in the history. The routing preserves all assistant messages, including image descriptions.

## Syncing with Upstream

This fork is regularly synced with [0xSero/codex-shim](https://github.com/0xSero/codex-shim).

To update:

```bash
cd ~/codex-shim
git checkout main
git fetch upstream
git merge upstream/main
git push origin main

git checkout feature/smart-vision-routing
git merge main
# Resolve conflicts if any
git push origin feature/smart-vision-routing
```

## Contributing Back

If you find this feature useful, consider contributing it back to the upstream project!

## Differences from Upstream

This fork adds:
- `codex_shim/vision_router.py` - Smart routing logic
- Modified `codex_shim/server.py` - Integration with routing (minimal changes)
- `tests/test_vision_router.py` - Test suite for routing
- Environment variable controls

All changes are designed to be:
- **Opt-in** - Disabled by default, no behavior change without env var
- **Minimal** - Small modifications to existing files
- **Modular** - Core logic in separate file

## License

MIT - Same as upstream codex-shim
