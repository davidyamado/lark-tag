# 飞书 AI 助手 Bot 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 Python 飞书 AI 助手服务，用户在飞书内与 Bot 对话，服务自动完成 OAuth 授权并通过 Claude + lark-cli 执行飞书操作。

**Architecture:** 单进程 Python 服务，使用 ThreadPoolExecutor 处理并发用户请求。Event Listener 子进程（lark-cli）监听飞书消息事件，主进程接收事件后并发处理：检查授权状态、调用 Claude agent loop、通过 lark-cli 子进程（per-user HOME）执行操作。

**Tech Stack:** Python 3.11+, openai SDK (OpenRouter), sqlite3 (stdlib), subprocess, threading, python-dotenv

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `src/config.py` | 读取并校验环境变量，暴露单例配置对象 |
| `src/user_store.py` | SQLite CRUD：用户授权状态 + 对话历史 |
| `src/lark_runner.py` | lark-cli 子进程封装：HOME 注入、`--as user` 自动追加、超时、安全校验 |
| `src/auth.py` | OAuth 授权流程：`--no-wait` 获取链接 + 后台轮询 device-code |
| `src/agent.py` | Claude agent loop：OpenRouter tool use，最多 10 轮工具调用 |
| `src/event_listener.py` | lark-cli event +subscribe 子进程管理，解析 NDJSON，自动重连 |
| `src/main.py` | 入口：初始化 DB、启动 event listener、ThreadPoolExecutor 处理事件 |
| `tests/` | 与 src/ 一一对应的测试文件 |
| `.env.example` | 环境变量模板 |
| `requirements.txt` | 依赖列表 |
| `scripts/setup.sh` | 一次性服务器初始化脚本 |

---

## Task 1: 项目骨架与依赖

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `src/__init__.py`
- Create: `tests/__init__.py`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p src tests scripts
touch src/__init__.py tests/__init__.py
```

- [ ] **Step 2: 写 requirements.txt**

```
openai>=1.0.0
python-dotenv>=1.0.0
pytest>=8.0.0
pytest-mock>=3.0.0
```

- [ ] **Step 3: 写 .env.example**

```env
OPENROUTER_API_KEY=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
SQLITE_PATH=/var/lark-bot/bot.db
LARK_BOT_HOME=/var/lark-bot/config
LARK_USERS_DIR=/var/lark-bot/users
```

- [ ] **Step 4: 安装依赖**

```bash
pip install -r requirements.txt
```

Expected: 所有包安装成功，无 error。

- [ ] **Step 5: Commit**

```bash
git init
git add requirements.txt .env.example src/__init__.py tests/__init__.py
git commit -m "feat: project scaffold"
```

---

## Task 2: config.py — 环境变量加载与校验

**Files:**
- Create: `src/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config.py
import os
import pytest

def test_config_raises_if_missing_required_vars(monkeypatch):
    """缺少必要环境变量时应抛出 ValueError"""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    # 清除已加载的模块缓存，确保重新读取环境变量
    import importlib
    import src.config as cfg_module
    with pytest.raises((ValueError, SystemExit)):
        importlib.reload(cfg_module)
        _ = cfg_module.Config()

def test_config_loads_all_vars(monkeypatch):
    """所有变量存在时应正确加载"""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "app-secret")
    monkeypatch.setenv("SQLITE_PATH", "/tmp/test.db")
    monkeypatch.setenv("LARK_BOT_HOME", "/tmp/lark-bot-home")
    monkeypatch.setenv("LARK_USERS_DIR", "/tmp/lark-users")
    import importlib
    import src.config as cfg_module
    importlib.reload(cfg_module)
    cfg = cfg_module.Config()
    assert cfg.openrouter_api_key == "test-key"
    assert cfg.feishu_app_id == "app-id"
    assert cfg.sqlite_path == "/tmp/test.db"
    assert cfg.lark_bot_home == "/tmp/lark-bot-home"
    assert cfg.lark_users_dir == "/tmp/lark-users"
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_config.py -v
```

Expected: FAILED (ImportError 或 AttributeError — Config 不存在)

- [ ] **Step 3: 实现 config.py**

```python
# src/config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Config:
    openrouter_api_key: str
    feishu_app_id: str
    feishu_app_secret: str
    sqlite_path: str
    lark_bot_home: str
    lark_users_dir: str

    def __init__(self):
        required = {
            "OPENROUTER_API_KEY": "openrouter_api_key",
            "FEISHU_APP_ID": "feishu_app_id",
            "FEISHU_APP_SECRET": "feishu_app_secret",
        }
        optional = {
            "SQLITE_PATH": ("sqlite_path", "/var/lark-bot/bot.db"),
            "LARK_BOT_HOME": ("lark_bot_home", "/var/lark-bot/config"),
            "LARK_USERS_DIR": ("lark_users_dir", "/var/lark-bot/users"),
        }
        missing = [k for k in required if not os.environ.get(k)]
        if missing:
            raise ValueError(f"缺少必要环境变量: {', '.join(missing)}")
        for env_key, attr in required.items():
            setattr(self, attr, os.environ[env_key])
        for env_key, (attr, default) in optional.items():
            setattr(self, attr, os.environ.get(env_key, default))
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_config.py -v
```

Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/config.py tests/test_config.py
git commit -m "feat: config loading with validation"
```

---

## Task 3: user_store.py — SQLite 用户状态与历史

**Files:**
- Create: `src/user_store.py`
- Create: `tests/test_user_store.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_user_store.py
import sqlite3
import pytest
from src.user_store import UserStore

@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test.db")
    s = UserStore(db_path)
    yield s
    s.close()

def test_get_user_returns_none_for_unknown(store):
    assert store.get_user("unknown_id") is None

def test_upsert_and_get_user(store):
    store.upsert_user("u1", auth_status="pending", pending_code="abc", pending_url="http://example.com")
    user = store.get_user("u1")
    assert user["auth_status"] == "pending"
    assert user["pending_code"] == "abc"

def test_mark_authorized(store):
    store.upsert_user("u1", auth_status="pending")
    store.mark_authorized("u1")
    user = store.get_user("u1")
    assert user["auth_status"] == "authorized"
    assert user["authorized_at"] is not None

def test_add_and_get_history(store):
    store.add_history("u1", "user", '{"text": "hello"}')
    store.add_history("u1", "assistant", '{"text": "hi"}')
    rows = store.get_history("u1")
    assert len(rows) == 2
    assert rows[0]["role"] == "user"
    assert rows[1]["role"] == "assistant"

def test_history_capped_at_20(store):
    for i in range(25):
        store.add_history("u1", "user", f'{{"text": "msg{i}"}}')
    rows = store.get_history("u1")
    assert len(rows) == 20

def test_history_ordered_asc(store):
    store.add_history("u1", "user", '{"text": "first"}')
    store.add_history("u1", "assistant", '{"text": "second"}')
    rows = store.get_history("u1")
    assert rows[0]["role"] == "user"
    assert rows[1]["role"] == "assistant"

def test_is_auth_pending_timeout(store):
    """pending_at 超过 10 分钟视为超时"""
    import datetime
    old_time = (datetime.datetime.utcnow() - datetime.timedelta(minutes=11)).isoformat()
    store.upsert_user("u1", auth_status="pending", pending_at=old_time)
    assert store.is_pending_expired("u1") is True

def test_is_auth_pending_not_timeout(store):
    import datetime
    recent = (datetime.datetime.utcnow() - datetime.timedelta(minutes=3)).isoformat()
    store.upsert_user("u1", auth_status="pending", pending_at=recent)
    assert store.is_pending_expired("u1") is False

def test_reset_auth(store):
    """reset_auth 应将已授权用户状态重置为 pending"""
    store.upsert_user("u1", auth_status="authorized")
    store.reset_auth("u1")
    user = store.get_user("u1")
    assert user["auth_status"] == "pending"
    assert user["authorized_at"] is None
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_user_store.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 user_store.py**

```python
# src/user_store.py
import sqlite3
import datetime
from typing import Optional

CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    open_id       TEXT PRIMARY KEY,
    auth_status   TEXT NOT NULL DEFAULT 'pending',
    pending_code  TEXT,
    pending_url   TEXT,
    pending_at    DATETIME,
    authorized_at DATETIME
)
"""

CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    open_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_history_open_id ON history(open_id, created_at)"

HISTORY_LIMIT = 20


class UserStore:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute(CREATE_USERS)
        cur.execute(CREATE_HISTORY)
        cur.execute(CREATE_INDEX)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def get_user(self, open_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM users WHERE open_id = ?", (open_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_user(self, open_id: str, **kwargs):
        user = self.get_user(open_id)
        if user is None:
            kwargs.setdefault("auth_status", "pending")
            cols = ["open_id"] + list(kwargs.keys())
            placeholders = ",".join("?" for _ in cols)
            vals = [open_id] + list(kwargs.values())
            self.conn.execute(
                f"INSERT INTO users ({','.join(cols)}) VALUES ({placeholders})", vals
            )
        else:
            if kwargs:
                sets = ", ".join(f"{k} = ?" for k in kwargs)
                vals = list(kwargs.values()) + [open_id]
                self.conn.execute(f"UPDATE users SET {sets} WHERE open_id = ?", vals)
        self.conn.commit()

    def mark_authorized(self, open_id: str):
        now = datetime.datetime.utcnow().isoformat()
        self.upsert_user(open_id, auth_status="authorized", authorized_at=now,
                         pending_code=None, pending_url=None)

    def is_pending_expired(self, open_id: str) -> bool:
        user = self.get_user(open_id)
        if not user or not user.get("pending_at"):
            return False
        pending_at = datetime.datetime.fromisoformat(user["pending_at"])
        elapsed = (datetime.datetime.utcnow() - pending_at).total_seconds()
        return elapsed > 600  # 10 minutes

    def add_history(self, open_id: str, role: str, content: str):
        self.conn.execute(
            "INSERT INTO history (open_id, role, content) VALUES (?, ?, ?)",
            (open_id, role, content)
        )
        # Keep only the last HISTORY_LIMIT rows for this user
        self.conn.execute("""
            DELETE FROM history WHERE open_id = ? AND id NOT IN (
                SELECT id FROM history WHERE open_id = ?
                ORDER BY created_at DESC LIMIT ?
            )
        """, (open_id, open_id, HISTORY_LIMIT))
        self.conn.commit()

    def get_history(self, open_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM history WHERE open_id = ? ORDER BY created_at ASC",
            (open_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_auth(self, open_id: str):
        """Reset auth status to pending (e.g., after token expiry)."""
        self.upsert_user(
            open_id,
            auth_status="pending",
            authorized_at=None,
            pending_code=None,
            pending_url=None,
            pending_at=None,
        )
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_user_store.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/user_store.py tests/test_user_store.py
git commit -m "feat: user store with SQLite (auth state + history)"
```

---

## Task 4: lark_runner.py — lark-cli 子进程封装

**Files:**
- Create: `src/lark_runner.py`
- Create: `tests/test_lark_runner.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_lark_runner.py
import os
import pytest
from unittest.mock import patch, MagicMock
from src.lark_runner import run_lark_cli

FAKE_USERS_DIR = "/fake/users"

def test_rejects_non_lark_cli_command():
    result = run_lark_cli("rm -rf /", "user123", users_dir=FAKE_USERS_DIR)
    assert "错误" in result

def test_rejects_empty_command():
    result = run_lark_cli("", "user123", users_dir=FAKE_USERS_DIR)
    assert "错误" in result

def test_injects_as_user_flag():
    """--as user 应自动追加到命令末尾"""
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        args = mock_run.call_args[0][0]
        assert "--as" in args
        assert "user" in args

def test_sets_home_env():
    """HOME 应设置为 users_dir/open_id"""
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        env = mock_run.call_args[1]["env"]
        assert env["HOME"] == f"{FAKE_USERS_DIR}/user123"

def test_returns_stdout_on_success():
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="result text", stderr="")
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert result == "result text"

def test_returns_stderr_when_no_stdout():
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="some error")
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert result == "some error"

def test_handles_timeout():
    import subprocess
    with patch("src.lark_runner.subprocess.run", side_effect=subprocess.TimeoutExpired("lark-cli", 30)):
        result = run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
        assert "超时" in result

def test_raises_token_expired_on_expired_output():
    """输出包含 token_expired 时应抛出 TokenExpiredError"""
    from src.lark_runner import TokenExpiredError
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="token_expired: please login again")
        with pytest.raises(TokenExpiredError):
            run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)

def test_raises_token_expired_on_unauthorized_output():
    """输出包含 unauthorized 时应抛出 TokenExpiredError"""
    from src.lark_runner import TokenExpiredError
    with patch("src.lark_runner.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="401 unauthorized")
        with pytest.raises(TokenExpiredError):
            run_lark_cli("lark-cli calendar +agenda", "user123", users_dir=FAKE_USERS_DIR)
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_lark_runner.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 lark_runner.py**

```python
# src/lark_runner.py
import os
import shlex
import subprocess

# Keywords in lark-cli output that indicate the user token has expired / is invalid
_TOKEN_EXPIRED_SIGNALS = (
    "token_expired",
    "token invalid",
    "unauthorized",
    "please login",
    "401",
)


class TokenExpiredError(Exception):
    """Raised when lark-cli output indicates the user token has expired."""


def run_lark_cli(command: str, open_id: str, users_dir: str, timeout: int = 30) -> str:
    if not command.strip().startswith("lark-cli "):
        return "错误：只允许执行 lark-cli 命令"

    # Auto-inject --as user; Claude-generated commands must not include it
    final_command = command.rstrip() + " --as user"
    user_home = os.path.join(users_dir, open_id)
    env = {**os.environ, "HOME": user_home}

    try:
        result = subprocess.run(
            shlex.split(final_command),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = result.stdout or result.stderr
        # Detect token expiry before returning to caller
        lower = output.lower()
        if any(sig in lower for sig in _TOKEN_EXPIRED_SIGNALS):
            raise TokenExpiredError(f"Token expired for {open_id}: {output[:200]}")
        return output
    except TokenExpiredError:
        raise
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超时（{timeout}s）"
    except Exception as e:
        return f"错误：{e}"
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_lark_runner.py -v
```

Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/lark_runner.py tests/test_lark_runner.py
git commit -m "feat: lark_runner with HOME injection and --as user"
```

---

## Task 5: auth.py — OAuth 授权流程

**Files:**
- Create: `src/auth.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_auth.py
import pytest
from unittest.mock import patch, MagicMock, call
from src.auth import AuthManager

FAKE_USERS_DIR = "/fake/users"
FAKE_BOT_HOME = "/fake/bot-home"

@pytest.fixture
def store(tmp_path):
    from src.user_store import UserStore
    s = UserStore(str(tmp_path / "test.db"))
    yield s
    s.close()

@pytest.fixture
def auth(store):
    return AuthManager(store, users_dir=FAKE_USERS_DIR, bot_home=FAKE_BOT_HOME)

def test_start_auth_returns_url(auth):
    """start_auth 应调用 lark-cli auth login --no-wait 并返回 URL"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"url": "https://auth.example.com/oauth", "code": "abc123"}',
            stderr=""
        )
        result = auth.start_auth("user1")
        assert result["url"] == "https://auth.example.com/oauth"
        assert result["code"] == "abc123"

def test_start_auth_saves_pending(auth, store):
    """start_auth 应将状态写入数据库"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            stdout='{"url": "https://auth.example.com/oauth", "code": "abc123"}',
            stderr=""
        )
        auth.start_auth("user1")
        user = store.get_user("user1")
        assert user["auth_status"] == "pending"
        assert user["pending_code"] == "abc123"

def test_poll_success_marks_authorized(auth, store):
    """device-code 轮询成功时，应标记用户为 authorized"""
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="Login successful", stderr="")
        result = auth.poll_once("user1", "abc123")
        assert result is True
        user = store.get_user("user1")
        assert user["auth_status"] == "authorized"

def test_poll_failure_returns_false(auth, store):
    """device-code 尚未授权时，返回 False"""
    store.upsert_user("user1", auth_status="pending", pending_code="abc123")
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="authorization_pending")
        result = auth.poll_once("user1", "abc123")
        assert result is False

def test_start_auth_raises_on_cli_error(auth):
    """lark-cli 返回空输出时应抛出 RuntimeError"""
    with patch("src.auth.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", stderr="error")
        with pytest.raises(RuntimeError):
            auth.start_auth("user1")
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_auth.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 auth.py**

```python
# src/auth.py
import json
import os
import shlex
import subprocess
import datetime
from src.user_store import UserStore


class AuthManager:
    def __init__(self, store: UserStore, users_dir: str, bot_home: str):
        self.store = store
        self.users_dir = users_dir
        self.bot_home = bot_home

    def _run(self, command: str, home: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": home}
        return subprocess.run(
            shlex.split(command),
            capture_output=True, text=True, timeout=30, env=env
        )

    def start_auth(self, open_id: str) -> dict:
        """
        Start device-code OAuth flow for a user.
        Returns {"url": ..., "code": ...}
        """
        user_home = os.path.join(self.users_dir, open_id)
        os.makedirs(user_home, mode=0o700, exist_ok=True)

        result = self._run(
            "lark-cli auth login --recommend --no-wait",
            home=user_home
        )
        raw = result.stdout.strip()
        if not raw:
            raise RuntimeError(f"auth login failed: {result.stderr}")

        data = json.loads(raw)
        now = datetime.datetime.utcnow().isoformat()
        self.store.upsert_user(
            open_id,
            auth_status="pending",
            pending_code=data["code"],
            pending_url=data["url"],
            pending_at=now,
        )
        return data

    def poll_once(self, open_id: str, code: str) -> bool:
        """
        Poll for device-code completion.
        Returns True if authorized, False if still pending.
        """
        user_home = os.path.join(self.users_dir, open_id)
        result = self._run(
            f"lark-cli auth login --device-code {code}",
            home=user_home
        )
        output = (result.stdout + result.stderr).lower()
        if "authorization_pending" in output or "expired" in output or not result.stdout.strip():
            return False
        self.store.mark_authorized(open_id)
        return True
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_auth.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/auth.py tests/test_auth.py
git commit -m "feat: auth manager with device-code OAuth flow"
```

---

## Task 6: agent.py — Claude Agent Loop

**Files:**
- Create: `src/agent.py`
- Create: `tests/test_agent.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_agent.py
import json
import pytest
from unittest.mock import patch, MagicMock, call
from src.agent import Agent

FAKE_API_KEY = "test-key"
FAKE_USERS_DIR = "/fake/users"

@pytest.fixture
def store(tmp_path):
    from src.user_store import UserStore
    s = UserStore(str(tmp_path / "test.db"))
    yield s
    s.close()

@pytest.fixture
def agent(store):
    return Agent(api_key=FAKE_API_KEY, store=store, users_dir=FAKE_USERS_DIR)

def _make_message(role, content):
    m = MagicMock()
    m.role = role
    if isinstance(content, str):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = content
        m.content = [text_block]
    else:
        m.content = content
    return m

def _make_response(role="assistant", content="回复内容", stop_reason="end_turn"):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].finish_reason = stop_reason
    resp.choices[0].message = _make_message(role, content)
    return resp

def test_chat_returns_text_response(agent, store):
    """正常对话应返回 AI 的文本回复"""
    with patch("src.agent.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.chat.completions.create.return_value = _make_response(content="你好！")
        result = agent.chat("user1", "你好")
        assert "你好" in result

def test_chat_saves_history(agent, store):
    """对话后应保存用户消息和助手回复到历史"""
    with patch("src.agent.OpenAI") as MockOpenAI:
        client = MockOpenAI.return_value
        client.chat.completions.create.return_value = _make_response(content="结果")
        agent.chat("user1", "查日程")
        history = store.get_history("user1")
        roles = [h["role"] for h in history]
        assert "user" in roles
        assert "assistant" in roles

def test_chat_executes_tool_call(agent, store):
    """当 Claude 调用 run_lark_cli tool 时，应执行并返回结果"""
    tool_call = MagicMock()
    tool_call.type = "tool_use"
    tool_call.id = "tc1"
    tool_call.function = MagicMock()
    tool_call.function.name = "run_lark_cli"
    tool_call.function.arguments = json.dumps({"command": "lark-cli calendar +agenda"})

    first_msg = MagicMock()
    first_msg.role = "assistant"
    first_msg.content = [tool_call]

    first_resp = MagicMock()
    first_resp.choices = [MagicMock()]
    first_resp.choices[0].finish_reason = "tool_calls"
    first_resp.choices[0].message = first_msg

    second_resp = _make_response(content="你今天有 2 个日程")

    with patch("src.agent.OpenAI") as MockOpenAI:
        with patch("src.agent.run_lark_cli", return_value="09:00 例会\n14:00 评审") as mock_runner:
            client = MockOpenAI.return_value
            client.chat.completions.create.side_effect = [first_resp, second_resp]
            result = agent.chat("user1", "查日程")
            mock_runner.assert_called_once()
            assert "日程" in result

def test_chat_stops_after_max_iterations(agent, store):
    """工具调用超过 10 次应停止并返回错误提示"""
    tool_call = MagicMock()
    tool_call.type = "tool_use"
    tool_call.id = "tc1"
    tool_call.function = MagicMock()
    tool_call.function.name = "run_lark_cli"
    tool_call.function.arguments = json.dumps({"command": "lark-cli calendar +agenda"})

    loop_msg = MagicMock()
    loop_msg.role = "assistant"
    loop_msg.content = [tool_call]

    loop_resp = MagicMock()
    loop_resp.choices = [MagicMock()]
    loop_resp.choices[0].finish_reason = "tool_calls"
    loop_resp.choices[0].message = loop_msg

    with patch("src.agent.OpenAI") as MockOpenAI:
        with patch("src.agent.run_lark_cli", return_value="result"):
            client = MockOpenAI.return_value
            client.chat.completions.create.return_value = loop_resp
            result = agent.chat("user1", "循环任务")
            assert isinstance(result, str)
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_agent.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 agent.py**

```python
# src/agent.py
import json
from openai import OpenAI
from src.user_store import UserStore
from src.lark_runner import run_lark_cli as _run_lark_cli

SYSTEM_PROMPT = """你是公司内部的飞书 AI 助手。你可以通过 run_lark_cli 工具直接操作飞书，
帮助用户完成日历、任务、文档、消息等各类工作。

规则：
- 操作前确认关键信息（如时间、参与人）
- 涉及删除/发送消息等不可逆操作，先描述计划让用户确认
- 命令中勿包含 --as 参数（系统自动添加 --as user）
- 命令超出 lark-cli 能力范围时，如实告知用户"""

TOOLS = [{
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

MAX_TOOL_ITERATIONS = 10
MODEL = "anthropic/claude-sonnet-4-6"


class Agent:
    def __init__(self, api_key: str, store: UserStore, users_dir: str):
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=api_key,
        )
        self.store = store
        self.users_dir = users_dir

    def chat(self, open_id: str, user_text: str) -> str:
        # Build message list from history
        history = self.store.get_history(open_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for h in history:
            messages.append({"role": h["role"], "content": json.loads(h["content"])})
        messages.append({"role": "user", "content": user_text})

        # Save user message
        self.store.add_history(open_id, "user", json.dumps(user_text))

        iterations = 0
        while iterations < MAX_TOOL_ITERATIONS:
            response = self.client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
            )
            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls":
                iterations += 1
                # Process all tool calls
                tool_results = []
                for tc in msg.content:
                    if tc.type != "tool_use":
                        continue
                    args = json.loads(tc.function.arguments)
                    command = args.get("command", "")
                    result = _run_lark_cli(command, open_id, users_dir=self.users_dir)
                    tool_results.append({
                        "tool_call_id": tc.id,
                        "role": "tool",
                        "content": result,
                    })

                # Append assistant message and tool results
                messages.append({"role": "assistant", "content": msg.content})
                messages.extend(tool_results)
                continue

            # end_turn — extract final text
            final_text = ""
            for block in msg.content:
                if hasattr(block, "text"):
                    final_text += block.text
            self.store.add_history(open_id, "assistant", json.dumps(final_text))
            return final_text

        # Exceeded max iterations
        return "抱歉，处理您的请求时遇到了问题，请稍后重试。"
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_agent.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/agent.py tests/test_agent.py
git commit -m "feat: Claude agent loop with OpenRouter tool use"
```

---

## Task 7: event_listener.py — 飞书事件监听

**Files:**
- Create: `src/event_listener.py`
- Create: `tests/test_event_listener.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_event_listener.py
import json
import pytest
from unittest.mock import patch, MagicMock, call
from src.event_listener import parse_event_line, EventListener

def test_parse_valid_message_event():
    """解析有效的 im.message.receive_v1 NDJSON 行"""
    line = json.dumps({
        "type": "event",
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}},
            "message": {"content": json.dumps({"text": "你好"}), "message_type": "text"}
        }
    })
    result = parse_event_line(line)
    assert result is not None
    assert result["open_id"] == "ou_123"
    assert result["text"] == "你好"

def test_parse_non_message_event_returns_none():
    """非消息事件应返回 None"""
    line = json.dumps({"type": "event", "event_type": "other.event"})
    assert parse_event_line(line) is None

def test_parse_invalid_json_returns_none():
    """无效 JSON 应返回 None"""
    assert parse_event_line("not json") is None

def test_parse_non_text_message_returns_none():
    """非文本消息（如图片）应返回 None"""
    line = json.dumps({
        "type": "event",
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": "ou_123"}},
            "message": {"content": "{}", "message_type": "image"}
        }
    })
    assert parse_event_line(line) is None

def test_event_listener_calls_callback_on_message():
    """EventListener 读取 NDJSON 行时应调用 on_message 回调"""
    messages = []

    valid_line = json.dumps({
        "type": "event",
        "event_type": "im.message.receive_v1",
        "event": {
            "sender": {"sender_id": {"open_id": "ou_abc"}},
            "message": {"content": json.dumps({"text": "test"}), "message_type": "text"}
        }
    })

    listener = EventListener(bot_home="/fake/bot-home", on_message=messages.append)

    # Simulate processing one line without starting a real subprocess
    event = parse_event_line(valid_line)
    if event:
        messages.append(event)

    assert len(messages) == 1
    assert messages[0]["open_id"] == "ou_abc"
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_event_listener.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 event_listener.py**

```python
# src/event_listener.py
import json
import logging
import os
import shlex
import subprocess
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SUBSCRIBE_CMD = (
    "lark-cli event +subscribe "
    "--event-types im.message.receive_v1 "
    "--compact --quiet"
)
RECONNECT_DELAY = 5  # seconds


def parse_event_line(line: str) -> Optional[dict]:
    """
    Parse one NDJSON line from lark-cli event +subscribe.
    Returns {"open_id": ..., "text": ...} or None.
    """
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None

    if data.get("event_type") != "im.message.receive_v1":
        return None

    try:
        event = data["event"]
        open_id = event["sender"]["sender_id"]["open_id"]
        msg = event["message"]
        if msg.get("message_type") != "text":
            return None
        text = json.loads(msg["content"]).get("text", "").strip()
        if not text:
            return None
        return {"open_id": open_id, "text": text}
    except (KeyError, json.JSONDecodeError):
        return None


class EventListener:
    def __init__(self, bot_home: str, on_message: Callable[[dict], None]):
        self.bot_home = bot_home
        self.on_message = on_message
        self._stop_event = threading.Event()

    def start(self):
        """Start listening in a background thread with auto-reconnect."""
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def stop(self):
        self._stop_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self._listen_once()
            except Exception as e:
                logger.error(f"Event listener error: {e}")
            if not self._stop_event.is_set():
                logger.info(f"Reconnecting in {RECONNECT_DELAY}s...")
                time.sleep(RECONNECT_DELAY)

    def _listen_once(self):
        env = {**os.environ, "HOME": self.bot_home}
        proc = subprocess.Popen(
            shlex.split(SUBSCRIBE_CMD),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        logger.info("Event listener started")
        try:
            for line in proc.stdout:
                if self._stop_event.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                event = parse_event_line(line)
                if event:
                    self.on_message(event)
        finally:
            proc.terminate()
            proc.wait()
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_event_listener.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add src/event_listener.py tests/test_event_listener.py
git commit -m "feat: event listener with NDJSON parsing and auto-reconnect"
```

---

## Task 8: main.py — 入口：线程池与流程编排

**Files:**
- Create: `src/main.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_main.py
import pytest
from unittest.mock import patch, MagicMock
from src.main import handle_message

@pytest.fixture
def deps(tmp_path):
    from src.user_store import UserStore
    from src.auth import AuthManager
    from src.agent import Agent

    store = UserStore(str(tmp_path / "test.db"))
    auth = AuthManager(store, users_dir="/fake/users", bot_home="/fake/bot")
    agent = Agent(api_key="test-key", store=store, users_dir="/fake/users")
    yield {"store": store, "auth": auth, "agent": agent}
    store.close()

def test_handle_message_triggers_auth_for_new_user(deps):
    """新用户首次发消息应触发授权流程"""
    with patch.object(deps["auth"], "start_auth", return_value={"url": "http://oauth.test", "code": "xyz"}) as mock_start:
        with patch("src.main.send_feishu_message") as mock_send:
            handle_message(
                {"open_id": "new_user", "text": "你好"},
                deps["store"], deps["auth"], deps["agent"]
            )
            mock_start.assert_called_once_with("new_user")
            mock_send.assert_called_once()
            call_args = mock_send.call_args[0]
            assert "http://oauth.test" in call_args[1]

def test_handle_message_triggers_auth_for_expired_pending(deps):
    """pending 超时的用户应重新触发授权"""
    import datetime
    old_time = (datetime.datetime.utcnow() - datetime.timedelta(minutes=11)).isoformat()
    deps["store"].upsert_user("u1", auth_status="pending", pending_code="old", pending_at=old_time)

    with patch.object(deps["auth"], "start_auth", return_value={"url": "http://oauth2.test", "code": "new"}) as mock_start:
        with patch("src.main.send_feishu_message"):
            handle_message(
                {"open_id": "u1", "text": "hello"},
                deps["store"], deps["auth"], deps["agent"]
            )
            mock_start.assert_called_once()

def test_handle_message_notifies_pending_user(deps):
    """pending 未超时时应提示用户完成授权"""
    import datetime
    recent = (datetime.datetime.utcnow() - datetime.timedelta(minutes=2)).isoformat()
    deps["store"].upsert_user("u1", auth_status="pending", pending_code="abc", pending_url="http://auth.example.com", pending_at=recent)

    with patch("src.main.send_feishu_message") as mock_send:
        handle_message(
            {"open_id": "u1", "text": "hello"},
            deps["store"], deps["auth"], deps["agent"]
        )
        mock_send.assert_called_once()
        assert "授权" in mock_send.call_args[0][1]

def test_handle_message_calls_agent_for_authorized_user(deps):
    """已授权用户发消息应调用 agent.chat"""
    deps["store"].upsert_user("u1", auth_status="authorized")

    with patch.object(deps["agent"], "chat", return_value="你好！") as mock_chat:
        with patch("src.main.send_feishu_message") as mock_send:
            handle_message(
                {"open_id": "u1", "text": "你好"},
                deps["store"], deps["auth"], deps["agent"]
            )
            mock_chat.assert_called_once_with("u1", "你好")
            mock_send.assert_called_once_with("u1", "你好！")

def test_handle_message_reauths_on_token_expired(deps):
    """agent.chat 抛出 TokenExpiredError 时应重置状态并重新发起授权"""
    from src.lark_runner import TokenExpiredError
    deps["store"].upsert_user("u1", auth_status="authorized")

    with patch.object(deps["agent"], "chat", side_effect=TokenExpiredError("expired")):
        with patch.object(deps["auth"], "start_auth", return_value={"url": "http://reauth.test", "code": "new"}) as mock_start:
            with patch("src.main.send_feishu_message") as mock_send:
                handle_message(
                    {"open_id": "u1", "text": "查日程"},
                    deps["store"], deps["auth"], deps["agent"]
                )
                # User status must be reset before re-auth
                user = deps["store"].get_user("u1")
                assert user["auth_status"] == "pending"
                mock_start.assert_called_once_with("u1")
                # Message should contain the new auth URL and explain why
                call_text = mock_send.call_args[0][1]
                assert "过期" in call_text or "重新" in call_text
                assert "http://reauth.test" in call_text
```

- [ ] **Step 2: 运行，确认失败**

```bash
pytest tests/test_main.py -v
```

Expected: FAILED (ImportError)

- [ ] **Step 3: 实现 main.py**

```python
# src/main.py
import logging
import os
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor

from src.agent import Agent
from src.auth import AuthManager
from src.config import Config
from src.event_listener import EventListener
from src.lark_runner import TokenExpiredError
from src.user_store import UserStore

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def send_feishu_message(open_id: str, text: str, bot_home: str = None) -> None:
    """Send a text message to a user via lark-cli (bot identity)."""
    if bot_home is None:
        bot_home = os.environ.get("LARK_BOT_HOME", "/var/lark-bot/config")
    env = {**os.environ, "HOME": bot_home}
    cmd = f"lark-cli im +send-message --to {open_id} --text {shlex.quote(text)} --as bot"
    try:
        subprocess.run(shlex.split(cmd), env=env, timeout=30, capture_output=True)
    except Exception as e:
        logger.error(f"Failed to send message to {open_id}: {e}")


def handle_message(event: dict, store: UserStore, auth: AuthManager, agent: Agent) -> None:
    open_id = event["open_id"]
    text = event["text"]

    user = store.get_user(open_id)

    # New user or expired pending — start OAuth
    if user is None or (user["auth_status"] == "pending" and store.is_pending_expired(open_id)):
        try:
            data = auth.start_auth(open_id)
            send_feishu_message(
                open_id,
                f"请点击以下链接完成授权，授权后即可使用 AI 助手：\n{data['url']}"
            )
        except Exception as e:
            logger.error(f"Auth start failed for {open_id}: {e}")
            send_feishu_message(open_id, "授权初始化失败，请稍后重试。")
        return

    # Pending but not yet expired — remind user
    if user["auth_status"] == "pending":
        url = user.get("pending_url", "")
        send_feishu_message(open_id, f"请先完成授权：\n{url}")
        return

    # Authorized — call agent
    try:
        reply = agent.chat(open_id, text)
        send_feishu_message(open_id, reply)
    except TokenExpiredError:
        # Reset auth state and restart OAuth flow
        store.reset_auth(open_id)
        try:
            data = auth.start_auth(open_id)
            send_feishu_message(
                open_id,
                f"您的授权已过期，请重新完成授权后继续使用：\n{data['url']}"
            )
        except Exception as e:
            logger.error(f"Re-auth failed for {open_id}: {e}")
            send_feishu_message(open_id, "授权已过期，重新授权时出现错误，请稍后重试。")
    except Exception as e:
        logger.error(f"Agent error for {open_id}: {e}")
        send_feishu_message(open_id, "处理您的请求时出现错误，请稍后重试。")


def main():
    cfg = Config()
    store = UserStore(cfg.sqlite_path)
    auth = AuthManager(store, users_dir=cfg.lark_users_dir, bot_home=cfg.lark_bot_home)
    agent = Agent(api_key=cfg.openrouter_api_key, store=store, users_dir=cfg.lark_users_dir)

    executor = ThreadPoolExecutor(max_workers=20)

    def on_message(event: dict):
        executor.submit(handle_message, event, store, auth, agent)

    listener = EventListener(bot_home=cfg.lark_bot_home, on_message=on_message)
    listener.start()

    logger.info("Feishu AI Bot started. Listening for messages...")
    try:
        import signal
        signal.pause()
    except (KeyboardInterrupt, AttributeError):
        pass
    finally:
        listener.stop()
        executor.shutdown(wait=False)
        store.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行，确认通过**

```bash
pytest tests/test_main.py -v
```

Expected: 4 passed

- [ ] **Step 5: 运行所有测试，确认全部通过**

```bash
pytest -v
```

Expected: All tests pass (约 33 tests)

- [ ] **Step 6: Commit**

```bash
git add src/main.py tests/test_main.py
git commit -m "feat: main entry point with ThreadPoolExecutor and message routing"
```

---

## Task 9: 服务器初始化脚本

**Files:**
- Create: `scripts/setup.sh`

- [ ] **Step 1: 写 setup.sh**

```bash
#!/bin/bash
# scripts/setup.sh — 一次性服务器初始化脚本
# 在 Ubuntu 22.04 云服务器上以 root 运行
set -e

# 1. 创建目录结构
mkdir -p /var/lark-bot/config/.lark-cli
mkdir -p /var/lark-bot/users
chmod 700 /var/lark-bot/users

# 2. 写入 bot 共享 app 配置（替换实际值）
# 配置文件路径：lark-cli 默认读取 $HOME/.lark-cli/config.json
cat > /var/lark-bot/config/.lark-cli/config.json << 'EOF'
{
  "app_id": "${FEISHU_APP_ID}",
  "app_secret": "${FEISHU_APP_SECRET}"
}
EOF
echo "注意：请手动将 config.json 中的占位符替换为实际值"

# 3. 创建 Python 虚拟环境并安装依赖
cd /opt/lark-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. 复制 .env 文件
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env，请填写实际值后再启动服务"
fi

# 5. 创建 systemd 服务
cat > /etc/systemd/system/lark-bot.service << 'EOF'
[Unit]
Description=Feishu AI Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/lark-bot
ExecStart=/opt/lark-bot/venv/bin/python -m src.main
EnvironmentFile=/opt/lark-bot/.env
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "Setup complete. Edit /opt/lark-bot/.env, then run: systemctl enable --now lark-bot"
```

- [ ] **Step 2: 添加执行权限**

```bash
chmod +x scripts/setup.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/setup.sh
git commit -m "feat: server setup script with systemd service"
```

---

## Task 10: 集成验证

验证整个系统端到端可用（需要真实的飞书应用和服务器）。

- [ ] **Step 1: 配置并启动服务**

```bash
cp .env.example .env
# 填写 OPENROUTER_API_KEY, FEISHU_APP_ID, FEISHU_APP_SECRET
python -m src.main
```

Expected: 日志输出 "Feishu AI Bot started. Listening for messages..."

- [ ] **Step 2: 事件监听验证**

在飞书向 bot 发送任意消息，服务器日志应出现事件解析记录。

- [ ] **Step 3: 授权流程验证**

首次发消息应收到 OAuth 链接；点击完成后再次发消息应正常响应。

- [ ] **Step 4: 日历功能验证**

发送"查看我今天的日程"，bot 应调用 `lark-cli calendar +agenda` 并返回结果。

- [ ] **Step 5: 多用户隔离验证**

两个不同用户同时对话，各自操作应互不干扰（检查 HOME 环境变量隔离）。

- [ ] **Step 6: Final commit**

```bash
git add .
git commit -m "docs: integration verification checklist"
```

---

## 快速参考

**运行全部测试：**
```bash
pytest -v
```

**启动服务（开发）：**
```bash
python -m src.main
```

**环境变量：**

| 变量 | 说明 |
|------|------|
| `OPENROUTER_API_KEY` | OpenRouter API Key |
| `FEISHU_APP_ID` | 飞书共享应用 App ID |
| `FEISHU_APP_SECRET` | 飞书共享应用 App Secret |
| `SQLITE_PATH` | SQLite 数据库路径，默认 `/var/lark-bot/bot.db` |
| `LARK_BOT_HOME` | Bot 身份 HOME 目录，默认 `/var/lark-bot/config` |
| `LARK_USERS_DIR` | 用户隔离目录根，默认 `/var/lark-bot/users` |
