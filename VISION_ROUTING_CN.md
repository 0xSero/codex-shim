# codex-shim 智能视觉路由

本 fork 为 codex-shim 添加了智能视觉路由功能，允许在同一对话中自动在支持视觉的模型和纯文本模型之间切换。

## 新功能

### 智能视觉路由

根据视觉内容自动路由请求：
- **图像请求** → 路由到支持视觉的模型（如 Kimi、Claude with vision）
- **纯文本请求** → 路由到默认文本模型（如 DeepSeek）
- **历史过滤** → 切换到纯文本模型时从历史中移除图像

这使你能够：
- 在大多数对话中使用更便宜/更快的文本模型
- 仅在需要时自动切换到视觉模型
- 保留助手的图像描述以保持上下文连续性

## 安装

与上游 codex-shim 相同：

```bash
git clone https://github.com/YOUR_USERNAME/codex-shim.git
cd codex-shim
python3 -m pip install --user -e .
```

## 配置

### 1. 配置模型

创建 `~/.codex-shim/models.json`，同时包含视觉和纯文本模型：

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

**关键字段：** `no_image_support`
- `true` → 纯文本模型（DeepSeek、大多数本地模型）
- `false` 或省略 → 支持视觉的模型（Kimi、Claude、GPT-4V）

### 2. 启用视觉路由

设置环境变量：

```bash
export CODEX_SHIM_VISION_ROUTING=true
```

或添加到你的 shell 配置文件（`~/.bashrc`、`~/.zshrc`）：

```bash
echo 'export CODEX_SHIM_VISION_ROUTING=true' >> ~/.zshrc
```

### 3. 启动 Shim

```bash
codex-shim generate
codex-shim start
codex-shim app
```

## 工作原理

### 对话流程示例

```
第 1 轮：用户发送图像 + "这是什么？"
→ 检测到当前轮有图像
→ 路由到 Kimi（视觉模型）
→ Kimi 回复："这是一只橙色的猫坐在沙发上"

第 2 轮：用户发送"它是什么品种？"
→ 当前轮没有图像
→ 路由到 DeepSeek（文本模型）
→ 历史被修改：
  - 图像替换为："[上一轮的图像 - 请参考上面助手的描述了解视觉细节]"
  - 助手的描述被保留："这是一只橙色的猫..."
→ DeepSeek 基于描述回复："根据描述，这可能是一只橘猫..."
```

### 路由逻辑

1. **检查当前轮**是否有图像（不是整个历史）
2. **如果有图像** → 使用第一个支持视觉的模型
3. **如果没有图像** → 使用默认模型（请求的模型或列表中的第一个）
4. **如果默认模型是纯文本** → 从历史中移除图像，保留文本

### 什么会被移除

路由到纯文本模型时，这些内容会被替换为文本占位符：
- `input_image` 项
- 消息内容中的图像
- 计算机使用截图（`computer_call_output`）
- 视觉函数调用输出

**什么会被保留：**
- 所有文本内容
- 助手回复（包括图像描述）
- 工具调用结果（文本部分）

## 配置选项

### 环境变量

| 变量 | 默认值 | 描述 |
|------|--------|------|
| `CODEX_SHIM_VISION_ROUTING` | `false` | 启用智能视觉路由 |
| `CODEX_SHIM_VISION_RECENT_TURNS` | `3` | 检查最近几轮对话中的图像 |
| `CODEX_SHIM_DEFAULT_TEXT_MODEL` | (第一个模型) | 纯文本请求的首选模型 |

### 自定义设置示例

```bash
export CODEX_SHIM_VISION_ROUTING=true
export CODEX_SHIM_DEFAULT_TEXT_MODEL=deepseek-chat
export CODEX_SHIM_VISION_RECENT_TURNS=5

codex-shim start
```

## 禁用视觉路由

要使用原始 codex-shim 行为：

```bash
unset CODEX_SHIM_VISION_ROUTING
# 或
export CODEX_SHIM_VISION_ROUTING=false
```

然后重启 shim：

```bash
codex-shim restart
```

## 故障排除

### 图像仍然导致错误

**问题：** 纯文本模型收到图像并返回错误。

**解决方案：** 确保视觉路由已启用：
```bash
echo $CODEX_SHIM_VISION_ROUTING  # 应该输出：true
```

如果未设置，启用它并重启：
```bash
export CODEX_SHIM_VISION_ROUTING=true
codex-shim restart
```

### 总是使用视觉模型

**问题：** 即使纯文本请求也使用视觉模型。

**可能原因：**
1. 没有配置纯文本模型 → 添加一个带有 `"no_image_support": true` 的模型
2. 最近历史中有图像 → 这是为了保持上下文连续性的预期行为

### 助手描述未保留

**问题：** 文本模型没有关于之前图像的上下文。

**检查：** 验证助手回复在历史中。路由会保留所有助手消息，包括图像描述。

## 与上游同步

本 fork 定期与 [0xSero/codex-shim](https://github.com/0xSero/codex-shim) 同步。

更新方法：

```bash
cd ~/codex-shim
git checkout main
git fetch upstream
git merge upstream/main
git push origin main

git checkout feature/smart-vision-routing
git merge main
# 如有冲突则解决
git push origin feature/smart-vision-routing
```

## 贡献回上游

如果你觉得这个功能有用，考虑将其贡献回上游项目！

## 与上游的差异

本 fork 添加了：
- `codex_shim/vision_router.py` - 智能路由逻辑
- 修改了 `codex_shim/server.py` - 与路由集成（最小化修改）
- `tests/test_vision_router.py` - 路由测试套件
- 环境变量控制

所有更改都设计为：
- **可选** - 默认禁用，没有环境变量不会改变行为
- **最小化** - 对现有文件的小修改
- **模块化** - 核心逻辑在单独文件中

## 许可证

MIT - 与上游 codex-shim 相同
