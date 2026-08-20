# 飞书 AI 助手 Bot 设计文档

**日期**：2026-04-09  
**状态**：已批准，待实现

---

## 背景与目标

公司希望为内部员工提供一个飞书 AI 助手：用户直接在飞书中和机器人对话，AI 自动理解意图并调用 lark-cli 完成操作（创建日程、任务、文档、发消息等），无需用户自己安装配置。

**与个人用法的关键区别**：不要求每个员工自行创建飞书应用，服务器预配置一个共享应用，用户只需完成一次 OAuth 授权即可。

---

## 技术选型

| 项目 | 选型 |
|------|------|
| 后端语言 | Python 3.11+ |
| AI 模型 | Claude（通过公司 OpenRouter 订阅） |
| 飞书事件接入 | lark-cli event +subscribe（WebSocket 长连接） |
| 飞书操作执行 | lark-cli（子进程，per-user HOME 隔离） |
| 会话存储 | SQLite |
| 进程管理 | PM2 或 supervisord |
| 部署 | Linux 云服务器（Ubuntu 22.04） |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    飞书平台                               │
│  用户 ──发消息──► 飞书 Bot (WebSocket 长连接)              │
└──────────────────────────┬──────────────────────────────┘
                           │ im.message.receive_v1 事件
                           ▼
┌─────────────────────────────────────────────────────────┐
│                  Bot 服务器 (Python)                      │
│                                                         │
│  ┌──────────────┐    ┌───────────────┐                  │
│  │ Event Listener│───►│  Auth Manager │                  │
│  │ (lark-cli    │    │  (首次授权流程) │                  │
│  │  event sub.) │    └───────┬───────┘                  │
│  └──────┬───────┘            │ 已授权                    │
│         │                    ▼                          │
│  ┌──────▼────────────────────────────────┐              │
│  │         Claude Agent Loop             │              │
│  │  ┌──────────┐   ┌──────────────────┐  │              │
│  │  │ 对话历史  │   │  run_lark_cli    │  │              │
│  │  │(SQLite)  │   │  Tool Handler    │  │              │
│  │  └──────────┘   └────────┬─────────┘  │              │
│  └───────────────────────────┼────────────┘              │
│                              ▼                           │
│  ┌────────────────────────────────────────┐              │
│  │         lark-cli Runner                │              │
│  │  HOME=/var/lark-bot/users/<open_id>/   │              │
│  │  lark-cli calendar +event-create ...   │              │
│  └────────────────────────────────────────┘              │
└─────────────────────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
      OpenRouter      SQLite           文件系统
     (Claude API)   (会话/auth)    (/var/lark-bot/)
```

---

## 用户授权流程

### 核心原则

- 服务器持有一个共享飞书应用（app_id + app_secret）
- 每个用户有独立目录 `/var/lark-bot/users/{open_id}/`，存储其 user_access_token
- bot 身份（事件监听）使用 `/var/lark-bot/config/` 下的主配置

### 首次对话授权

```
用户首次发消息
      │
      ▼
检查 Redis: users:{open_id}:auth_status
      │
  未授权
      │
      ├─► 创建用户目录，写入 app 共享配置
      │
      ├─► HOME=/var/lark-bot/users/{open_id} lark-cli auth login --recommend --no-wait
      │   → 返回 {url, code}
      │
      ├─► Bot 发送 OAuth 链接给用户
      │
      └─► 启动后台轮询（每 5 秒）：
              lark-cli auth login --device-code {code}
              成功 → Redis 标记 auth=true，通知用户继续
              10 分钟超时 → 告知用户重发消息重试
```

### Token 存储

- Linux 无桌面环境：lark-cli 回退到文件系统存储（无 D-Bus keychain）
- 目录权限：`chmod 700 /var/lark-bot/users/{open_id}/`

### Token 过期处理

飞书 user_access_token 有效期有限，需在运行时检测并重新授权：

- **检测**：`lark_runner.py` 检查子进程输出是否含过期/无效关键词：
  `token_expired`、`token invalid`、`unauthorized`、`please login`、`401`
- **信号**：检测到上述关键词时抛出 `TokenExpiredError`（定义于 `lark_runner.py`）
- **响应**：`main.py` 捕获 `TokenExpiredError` 后：
  1. 调用 `user_store.reset_auth(open_id)` 将用户状态重置为 `pending`
  2. 重新发起 OAuth 授权流程（同首次授权）
  3. 通知用户："您的授权已过期，请重新完成授权"

---

## Claude Agent Loop

### OpenRouter 配置

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)
MODEL = "anthropic/claude-sonnet-4-6"
```

### Tool 定义

```python
tools = [{
    "type": "function",
    "function": {
        "name": "run_lark_cli",
        "description": "执行 lark-cli 命令来操作飞书。支持日历、任务、文档、IM、邮件等所有功能。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整的 lark-cli 命令，例如：lark-cli calendar +agenda 或 lark-cli task +create --title '需求评审'"
                }
            },
            "required": ["command"]
        }
    }
}]
```

### System Prompt

```
你是公司内部的飞书 AI 助手。你可以通过 run_lark_cli 工具直接操作飞书，
帮助用户完成日历、任务、文档、消息等各类工作。

规则：
- 操作前确认关键信息（如时间、参与人）
- 涉及删除/发送消息等不可逆操作，先描述计划让用户确认
- 命令中勿包含 --as 参数（系统自动添加 --as user）
- 命令超出 lark-cli 能力范围时，如实告知用户
```

### 执行循环

```
收到用户消息
      │
      ▼
加载对话历史（SQLite SELECT 最近 20 条 WHERE open_id=?）
      │
      ▼
调用 OpenRouter Claude API
      │
    tool_use → 执行 lark-cli 子进程 → 返回 tool_result → 继续（最多 10 次）
      │
    end_turn → 发送最终回复给用户 → 保存历史
```

### lark-cli 子进程安全封装

```python
import subprocess, os, shlex

def run_lark_cli(command: str, open_id: str, timeout: int = 30) -> str:
    if not command.strip().startswith("lark-cli "):
        return "错误：只允许执行 lark-cli 命令"
    # 自动注入 --as user（Claude 生成的命令不含此参数）
    final_command = command.rstrip() + " --as user"
    user_home = os.path.join(os.environ["LARK_USERS_DIR"], open_id)
    env = {**os.environ, "HOME": user_home}
    result = subprocess.run(
        shlex.split(final_command),
        capture_output=True, text=True,
        timeout=timeout, env=env
    )
    return result.stdout or result.stderr
```

---

## 数据存储

### SQLite 表结构

```sql
-- 用户授权状态
CREATE TABLE users (
    open_id       TEXT PRIMARY KEY,
    auth_status   TEXT NOT NULL DEFAULT 'pending',  -- 'authorized' | 'pending'
    pending_code  TEXT,
    pending_url   TEXT,
    pending_at    DATETIME,                          -- 用于判断 10 分钟超时
    authorized_at DATETIME
);

-- 对话历史（每条消息一行）
CREATE TABLE history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    open_id    TEXT NOT NULL,
    role       TEXT NOT NULL,  -- 'user' | 'assistant' | 'tool'
    content    TEXT NOT NULL,  -- JSON 字符串
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_history_open_id ON history(open_id, created_at);
```

**查询历史**：取最近 20 条，按 created_at ASC 排序。  
**清理旧历史**：插入新消息时，若该用户记录超过 20 条则删除最旧的。  
**授权超时检查**：`pending_at` 超过 10 分钟视为过期，重新发起授权流程。

### 文件系统

```
/var/lark-bot/
  config/
    .lark-cli/config.json      ← 共享 app 配置（bot 身份）
  users/
    {open_id}/
      .lark-cli/config.json    ← 共享 app + 用户 token
```

---

## 项目代码结构

```
src/
  main.py             ← 入口：启动事件监听和异步任务
  event_listener.py   ← lark-cli event +subscribe 子进程管理与重连（使用 LARK_BOT_HOME，bot 身份）
  agent.py            ← Claude agent loop
  auth.py             ← 用户授权流程（OAuth + device-code 轮询）
  lark_runner.py      ← lark-cli 子进程封装（HOME 注入、超时、安全检查）
  user_store.py       ← SQLite 操作封装（用户授权状态 + 对话历史）
  config.py           ← 环境变量读取与校验

requirements.txt
.env.example
```

---

## 环境变量

```env
OPENROUTER_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
SQLITE_PATH=/var/lark-bot/bot.db
LARK_BOT_HOME=/var/lark-bot/config
LARK_USERS_DIR=/var/lark-bot/users
```

---

## 依赖

```
openai>=1.0.0        # OpenRouter 兼容接口
python-dotenv>=1.0.0 # 环境变量
# SQLite 使用 Python 标准库 sqlite3，无需额外依赖
```

---

## 验证方式

1. **事件监听**：服务启动后，在飞书向 bot 发送任意消息，服务器日志应出现事件
2. **授权流程**：首次发消息应收到 OAuth 链接，点击完成后再次发消息应正常响应
3. **日历功能**：发送"查看我今天的日程"，bot 应调用 `lark-cli calendar +agenda` 并返回结果
4. **任务创建**：发送"创建一个任务：明天提交周报"，bot 应创建任务并确认
5. **多用户隔离**：两个不同用户同时对话，各自的操作应互不干扰
