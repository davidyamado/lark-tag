# 去掉公共 OpenRouter key fallback

## 当前逻辑（已上线）

优先级：个人 key > 公共 key（env `ANTHROPIC_AUTH_TOKEN`） > 阻断提示

- 有个人 key → 用个人 key 对话
- 无个人 key 但有公共 key → fallback 到公共 key
- 无个人 key 且无公共 key → 显示申请提示，不进入 Claude

## 下周操作

**只需一步：删除 env 里的 `ANTHROPIC_AUTH_TOKEN`（或将其设为空）。**

无需改代码。阻断逻辑已内置在 `_stream_claude` 中（搜索 `not personal_key and not _shared_key`）。

## 验证

1. **有个人 key 的用户**：正常对话，日志 "Using personal key"
2. **无个人 key 的用户**：收到 "您还未申请AI使用key..." 卡片，不进入 Claude
3. **OA API 异常**：收到 "key获取异常，请联系开发者。"

## 完成后

删除本文件。
