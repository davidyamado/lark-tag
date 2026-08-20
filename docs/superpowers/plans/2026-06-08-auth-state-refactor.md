# 授权状态重构实施计划

> **给执行 Agent：** 必须使用子技能：推荐 `superpowers:subagent-driven-development`，或使用 `superpowers:executing-plans` 按任务逐项执行。本计划使用复选框（`- [ ]`）跟踪步骤状态。

**目标：** 让 Lark 和 Meegle 授权流程具备可恢复、单消费者、与真实 CLI 凭证一致的语义，同时阻止 Claude 在权限错误时吊销 Meegle token。

**架构：** 引入由 DB 持久化的 `auth_resume_jobs` 表，作为“授权完成后应该恢复哪条原始用户请求”的唯一可靠来源。所有 Lark/Meegle poller 以及 inline 授权完成路径都调用统一完成函数：先验证真实凭证，再原子 claim resume job，最后只 stream 一次 Claude。Meegle 权限错误与 OAuth 错误分开处理，并且不再指导 Claude 运行 `meegle auth logout`。

**技术栈：** Python、PostgreSQL、psycopg2、pytest，以及现有的 `UserStore`、`AuthManager` 和 `main.py` poller 基础设施。

---

## 问题摘要

当前生产证据显示有三个相关但不同的问题：

- 当另一个 pod 已经把授权标记为 `authorized` 时，Lark recovery poller 可能直接退出，导致原始 pending 消息永远不恢复。
- Inline 授权完成路径可能恢复错误的当前消息，因为原始消息只存在于某个 pod 的内存参数中。
- Meegle 授权会进入循环，因为 prompt 在宽泛权限错误下指导 Claude 运行 `meegle auth logout`。日志显示确实执行过 `meegle auth logout`，但实际 create 失败是 `user has not enabled this MCP feature`，这是 Meegle MCP/写权限问题，而不是 OAuth 问题。

共同的架构问题是：授权被建模为分散的 DB 字段加内存回调参数。真正应该持久化的单元是 auth resume job。

## 凭证生命周期发现

本地非破坏性检查显示两个 CLI 有明显差异：

- `lark-cli auth status --verify` 返回 `expiresAt`、`refreshExpiresAt`、`tokenStatus=needs_refresh`、`verified=true`，并且 scope 中包含 `offline_access`。这解释了为什么 Lark 感觉稳定：access token 可以过期，但 refresh 路径能让用户实际保持已授权。
- `meegle auth status` 返回 `authenticated` 和 `host`；本地授权后也可能返回 `expires_in_minutes`（本地测试显示刚授权后约 119 分钟）。`meegle auth --help` 暴露 `login`、`logout` 和 `status`，但没有显式 refresh 命令。生产证据还显示 `.meegle/credentials.enc` 存在时，`meegle auth status` 仍可能返回 `authenticated=false`。
- Meegle GitHub 文档描述 auth probe 会返回 `reason`。`no_local_token` 表示没有本地凭证文件，`token_rejected_by_server` 表示过期/被吊销/refresh 耗尽，`server_unreachable_or_error` 不应触发重新登录。退出码 `1` 表示本地凭证不存在或被拒绝；退出码 `2` 表示 auth probe 无法远程验证，调用方应稍后重试而不是重新授权。
- Meegle 文档也支持 env-token 模式（`MEEGLE_USER_ACCESS_TOKEN`，可选 `MEEGLE_ACCESS_TOKEN_HEADER`），但明确说明 env token 会绕过本地 keychain 存储和 refresh flow。因此 bot 应优先使用 per-user 本地凭证以获得更长期稳定的授权，而不是注入 env token。

因此设计上应把 Meegle 当成短生命周期或可能被外部失效的凭证，同时避免任何人为吊销。系统绝不能自动调用 `meegle auth logout`，必须在发布之间保留 `.machine-key` 和 `credentials.enc`，并且必须使用真实的 `meegle auth status` 检查来判断是否真的需要 OAuth。

## 文件结构

- 修改：`src/user_store.py`
  - 新增 `auth_resume_jobs` schema 和幂等 migration。
  - 新增 create、claim、consume、expire、card-id update、list 等辅助方法。
  - 新增 Meegle 凭证状态 reconcile 辅助方法，并复用现有 `none/pending/authorized` 状态以保证回滚安全。

- 修改：`src/auth.py`
  - 在 Meegle auth init/poll/status 周围增加日志。
  - 让 Meegle 完成路径只有在 `meegle auth status` 确认 `authenticated=true` 后才返回成功，并在 device-code poll 成功后增加短重试窗口。
  - 解析 Meegle `reason`/退出码，避免网络/服务端探测失败触发重新授权。
  - CLI 凭证检查失败时，不把 DB 标记为 `meegle_auth_status=authorized`。

- 修改：`src/main.py`
  - 每当用户请求触发 Lark 或 Meegle 授权流程时，写入 resume job。
  - 将授权后的直接 `_stream_claude(... original_text ...)` 替换为 `_complete_auth_and_resume(...)`。
  - 确保 inline pending completion、recovery poller、initial poller 和 Meegle poller 都使用同一套 claim-and-resume 函数。
  - 第一阶段只在 Meegle 相关请求进入 Claude 前对齐 DB 状态与真实 CLI 凭证状态；`/reset` 只重置对话，不修改授权状态。Meegle 授权重置统一使用 `/meegle-reauth`。

- 修改：`src/agent.py`
  - 删除指导 Claude 运行 `meegle auth logout` 的指令。
  - 增加明确错误分类：`authenticated=false` 可以触发 `/meegle-auth`；`user has not enabled this MCP feature` 不能触发授权。查询/读取类请求遇到该错误时，只能说明当前无法通过 Meegle CLI 读取目标信息，不能要求用户开启写权限，也不能声称已经查到状态。

- 测试：
  - 修改：`tests/test_user_store.py`
  - 修改：`tests/test_auth.py`
  - 修改：`tests/test_main.py`
  - 修改：`tests/test_multipod_coordination.py`
  - 新增：`tests/test_meegle_auth_flow.py`

---

## 当前修改进度（2026-06-08）

第一阶段已完成并在本地验证，提交范围为 Meegle 止血与状态一致性，不包含第二阶段 `auth_resume_jobs` 状态机改造。

- [x] 任务 2：已移除 prompt 中主动吊销 Meegle token 的指令，并增加回归测试，确保系统提示不再包含 `meegle auth logout`。
- [x] 任务 3：已新增 `AuthManager.meegle_auth_status()`，解析 `reason`、退出码、`retryable` 和 `expires_in_minutes`；device-code poll 成功后会短重试 `meegle auth status`，只有确认 `authenticated=true` 才写入 DB authorized。
- [x] 任务 7：已在 Meegle 相关请求进入 Claude 前 reconcile DB 状态和真实 CLI 凭证状态。`DB authorized + CLI authenticated=false + 非 retryable` 会清 DB Meegle 状态；`server_unreachable_or_error` / 退出码 `2` 保留 DB 状态，避免网络或服务端临时异常触发重新授权。
- [x] 任务 8：实施范围已修正。`/reset` 只重置对话/session，不修改 Lark 或 Meegle 授权状态；Meegle 授权重置入口为 `/meegle-reauth`，该命令才会受控 revoke Meegle token 并清理 DB 状态。
- [x] 任务 9：已补充 Meegle auth 状态日志和部署验证命令。
- [x] 额外修正：`user has not enabled this MCP feature` 已经确认为 Meegle 后端问题。当前处理策略是：不重新授权、不吊销 token；读请求只说明 Meegle CLI 后端拒绝读取，不能把它包装成“开启写权限后继续查”；写请求才可提示 Meegle MCP 能力或空间权限可能不满足。

本地验证结果：

```bash
.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_auth.py tests/test_main.py tests/test_multipod_coordination.py -q
# 39 passed

.venv\Scripts\python.exe -m pytest -q
# 110 passed, 23 skipped, 1 warning
```

### 任务 1：新增持久化授权恢复任务

**文件：**
- 修改：`src/user_store.py`
- 测试：`tests/test_user_store.py`

- [ ] **步骤 1：编写失败的 store 测试**

把以下测试加入 `tests/test_user_store.py`：

```python
def test_create_and_claim_auth_resume_job_once(store):
    job_id = store.create_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        client_id="client1",
        resume_text="create a requirement",
        reply_id="om_reply",
        thread_key="thread_1",
        root_id="root_1",
        chat_id="oc_chat",
        chat_type="p2p",
        existing_msg_id="om_auth_card",
    )

    first = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        owner="pod-a",
    )
    second = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        owner="pod-b",
    )

    assert first is not None
    assert first["id"] == job_id
    assert first["resume_text"] == "create a requirement"
    assert first["existing_msg_id"] == "om_auth_card"
    assert second is None


def test_claim_auth_resume_job_rejects_wrong_device_code(store):
    store.create_auth_resume_job(
        context_id="ou_u1",
        provider="lark",
        device_code="dev1",
        client_id="",
        resume_text="original request",
        reply_id="",
        thread_key="",
        root_id="",
        chat_id="",
        chat_type="p2p",
        existing_msg_id="",
    )

    claimed = store.claim_auth_resume_job(
        context_id="ou_u1",
        provider="lark",
        device_code="other-dev",
        owner="pod-a",
    )

    assert claimed is None
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
pytest tests/test_user_store.py::test_create_and_claim_auth_resume_job_once tests/test_user_store.py::test_claim_auth_resume_job_rejects_wrong_device_code -v
```

预期：两个测试都失败，因为 `create_auth_resume_job` 和 `claim_auth_resume_job` 尚不存在。

- [ ] **步骤 3：新增 schema**

在 `src/user_store.py` 中，把以下 schema 加到现有 `CREATE_*` 常量附近：

```python
CREATE_AUTH_RESUME_JOBS = """
CREATE TABLE IF NOT EXISTS auth_resume_jobs (
    id              TEXT PRIMARY KEY,
    context_id      TEXT NOT NULL,
    provider        TEXT NOT NULL,
    device_code     TEXT NOT NULL,
    client_id       TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    resume_text     TEXT NOT NULL DEFAULT '',
    reply_id        TEXT NOT NULL DEFAULT '',
    thread_key      TEXT NOT NULL DEFAULT '',
    root_id         TEXT NOT NULL DEFAULT '',
    chat_id         TEXT NOT NULL DEFAULT '',
    chat_type       TEXT NOT NULL DEFAULT 'p2p',
    existing_msg_id TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL,
    claimed_at      TIMESTAMPTZ,
    claimed_by      TEXT NOT NULL DEFAULT '',
    consumed_at     TIMESTAMPTZ,
    error           TEXT NOT NULL DEFAULT ''
)
"""

CREATE_AUTH_RESUME_JOBS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_auth_resume_pending
ON auth_resume_jobs (context_id, provider, status, created_at DESC)
"""
```

在 `_init_schema` 中执行这两个语句：

```python
cur.execute(CREATE_AUTH_RESUME_JOBS)
cur.execute(CREATE_AUTH_RESUME_JOBS_INDEX)
```

- [ ] **步骤 4：新增 store 方法**

在 `src/user_store.py` 顶部新增 import：

```python
import uuid
```

把以下方法加入 `UserStore`：

```python
    def create_auth_resume_job(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        client_id: str,
        resume_text: str,
        reply_id: str,
        thread_key: str,
        root_id: str,
        chat_id: str,
        chat_type: str,
        existing_msg_id: str = "",
    ) -> str:
        if provider not in ("lark", "meegle"):
            raise ValueError(f"invalid auth provider: {provider}")
        now = datetime.datetime.now(datetime.timezone.utc)
        job_id = str(uuid.uuid4())
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO auth_resume_jobs (
                        id, context_id, provider, device_code, client_id,
                        status, resume_text, reply_id, thread_key, root_id,
                        chat_id, chat_type, existing_msg_id, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id, context_id, provider, device_code, client_id or "",
                        resume_text or "", reply_id or "", thread_key or "", root_id or "",
                        chat_id or "", chat_type or "p2p", existing_msg_id or "", now,
                    ),
                )
            conn.commit()
            return job_id
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def claim_auth_resume_job(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        owner: str,
    ) -> Optional[dict]:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH picked AS (
                        SELECT id
                        FROM auth_resume_jobs
                        WHERE context_id = %s
                          AND provider = %s
                          AND device_code = %s
                          AND status = 'pending'
                        ORDER BY created_at DESC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE auth_resume_jobs j
                    SET status = 'claimed',
                        claimed_at = %s,
                        claimed_by = %s
                    FROM picked
                    WHERE j.id = picked.id
                    RETURNING j.*
                    """,
                    (context_id, provider, device_code, now, owner or ""),
                )
                row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def consume_auth_resume_job(self, job_id: str) -> None:
        now = datetime.datetime.now(datetime.timezone.utc)
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auth_resume_jobs SET status = 'consumed', consumed_at = %s WHERE id = %s",
                    (now, job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def fail_auth_resume_job(self, job_id: str, error: str) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE auth_resume_jobs SET status = 'failed', error = %s WHERE id = %s",
                    ((error or "")[:1000], job_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def set_auth_resume_existing_msg_id(
        self,
        context_id: str,
        provider: str,
        device_code: str,
        existing_msg_id: str,
    ) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE auth_resume_jobs
                    SET existing_msg_id = %s
                    WHERE context_id = %s
                      AND provider = %s
                      AND device_code = %s
                      AND status = 'pending'
                    """,
                    (existing_msg_id or "", context_id, provider, device_code),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)
```

- [ ] **步骤 5：运行 store 测试**

运行：

```bash
pytest tests/test_user_store.py::test_create_and_claim_auth_resume_job_once tests/test_user_store.py::test_claim_auth_resume_job_rejects_wrong_device_code -v
```

预期：两个测试通过。

- [ ] **步骤 6：提交**

```bash
git add src/user_store.py tests/test_user_store.py
git commit -m "feat: add durable auth resume jobs"
```

---

### 任务 2：阻止 Claude 吊销 Meegle 凭证

**文件：**
- 修改：`src/agent.py`
- 测试：`tests/test_agent.py`

- [ ] **步骤 1：编写 prompt 回归测试**

把以下测试加入 `tests/test_agent.py`：

```python
def test_meegle_prompt_does_not_instruct_logout(tmp_path):
    from src.agent import Agent

    agent = Agent(users_dir=str(tmp_path / "users"), bot_home=str(tmp_path / "bot-home"))
    cmd = agent._build_cmd(
        "create a requirement",
        open_id="ou_u1",
        session_id="sess",
        is_resume=False,
        chat_id="oc_chat",
        chat_type="p2p",
        user_prefs="",
        image_paths=None,
        home_key="ou_u1",
        max_turns=1,
        is_scheduled_task=False,
    )
    prompt = cmd[-1]

    assert "meegle auth logout" not in prompt
    assert "user has not enabled this MCP feature" in prompt
    assert "不要重新授权" in prompt
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
pytest tests/test_agent.py::test_meegle_prompt_does_not_instruct_logout -v
```

预期：失败，因为当前 prompt 包含 `meegle auth logout`。

- [ ] **步骤 3：替换 Meegle prompt 段落**

在 `src/agent.py` 中，将现有提到 `meegle auth logout` 的 Meegle 授权说明替换为以下策略文本：

```python
            "若 `meegle auth status` 返回 authenticated=false，才发起 Meegle OAuth：\n"
            f'  curl -s -X POST http://127.0.0.1:$INTERNAL_API_PORT/meegle-auth \\\n'
            f'    -H "Authorization: Bearer $INTERNAL_API_TOKEN" \\\n'
            f'    -H "Content-Type: application/json" \\\n'
            f'    -d \'{{"open_id":"{_meegle_ctx}"}}\'\n'
            "该命令输出 JSON，其中 url 字段是用户需要点击的授权链接。\n"
            "把授权链接发给用户后立即结束本轮回复；不要运行 meegle auth wait、不要轮询、不要 sleep。\n"
            "严禁主动运行 `meegle auth logout`。只有用户明确发送「重新授权 meegle」时，机器人后端才会受控重置 Meegle 授权。\n"
            "错误分类规则：\n"
            "- authenticated=false/token missing/token expired：可以发起 /meegle-auth。\n"
            "- user has not enabled this MCP feature：这是 Meegle CLI 后端拒绝当前账号使用 MCP 功能，不是 OAuth 过期；不要重新授权，不要吊销 token。\n"
            "  如果用户只是查询/读取工作项、状态、列表或项目资料：请明确说明本次无法通过 Meegle CLI 读取目标信息，"
            "不要要求用户开启写权限，不要承诺开启后继续查询，也不要声称已经查到状态。\n"
            "  如果用户是在创建、更新、流转等写操作中遇到该错误：可以说明 Meegle MCP 功能或对应空间写权限可能未满足，"
            "但仍然不要重新授权。\n"
            "- no permission/403/forbidden 但 auth status 仍 authenticated=true：优先解释为项目权限或 MCP 能力限制，不要重新授权。\n"
            "- 只有 auth status 明确为 authenticated=false，才进入 OAuth 流程。\n"
```

- [ ] **步骤 4：运行测试**

运行：

```bash
pytest tests/test_agent.py::test_meegle_prompt_does_not_instruct_logout -v
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add src/agent.py tests/test_agent.py
git commit -m "fix: prevent Claude from revoking Meegle tokens"
```

---

### 任务 3：写入授权成功状态前验证 Meegle 真实凭证

**文件：**
- 修改：`src/auth.py`
- 测试：`tests/test_auth.py`

- [ ] **步骤 1：编写失败测试**

把以下测试加入 `tests/test_auth.py`：

```python
def test_poll_meegle_once_does_not_authorize_when_status_false(auth, store):
    store.upsert_user(
        "ou_u1",
        meegle_auth_status="pending",
        meegle_pending_code="dev1",
        meegle_pending_client_id="client1",
    )

    def fake_run_meegle(args, home, timeout=30):
        if args[:3] == ["auth", "login", "--device-code"]:
            return MagicMock(stdout='{"ok": true}', stderr="", returncode=0)
        if args == ["auth", "status"]:
            return MagicMock(stdout='{"authenticated": false, "host": "project.feishu.cn"}', stderr="", returncode=1)
        raise AssertionError(args)

    auth._run_meegle = fake_run_meegle

    assert auth.poll_meegle_once("ou_u1", "client1", "dev1") is False
    assert store.get_user("ou_u1")["meegle_auth_status"] == "pending"


def test_poll_meegle_once_authorizes_when_status_true(auth, store):
    store.upsert_user(
        "ou_u1",
        meegle_auth_status="pending",
        meegle_pending_code="dev1",
        meegle_pending_client_id="client1",
    )

    def fake_run_meegle(args, home, timeout=30):
        if args[:3] == ["auth", "login", "--device-code"]:
            return MagicMock(stdout='{"ok": true}', stderr="", returncode=0)
        if args == ["auth", "status"]:
            return MagicMock(stdout='{"authenticated": true, "host": "project.feishu.cn", "expires_in_minutes": 119}', stderr="", returncode=0)
        raise AssertionError(args)

    auth._run_meegle = fake_run_meegle

    assert auth.poll_meegle_once("ou_u1", "client1", "dev1") is True
    assert store.get_user("ou_u1")["meegle_auth_status"] == "authorized"
```

- [ ] **步骤 2：运行测试**

运行：

```bash
pytest tests/test_auth.py::test_poll_meegle_once_does_not_authorize_when_status_false tests/test_auth.py::test_poll_meegle_once_authorizes_when_status_true -v
```

预期：如果当前代码过于宽泛地标记 authorized，第一个测试会失败；第二个测试应通过，或用于指导最小调整。

- [ ] **步骤 3：新增信息更完整的 Meegle status probe**

在 `src/auth.py` 中新增以下 helper，并保留 `is_meegle_authenticated` 作为 bool 包装：

```python
    def meegle_auth_status(self, open_id: str) -> dict:
        """Return Meegle auth status with reason/exit-code semantics.

        Expected result:
        {
            "authenticated": bool,
            "host": str,
            "reason": str,
            "exit_code": int,
            "retryable": bool,
        }
        """
        user_home = os.path.join(self.users_dir, open_id)
        try:
            result = self._run_meegle(["auth", "status"], home=user_home)
            data = json.loads(result.stdout or "{}")
            ok = bool(data.get("authenticated"))
            reason = data.get("reason", "")
            exit_code = getattr(result, "returncode", 0)
            retryable = (exit_code == 2 or reason == "server_unreachable_or_error")
            logger.info(
                "[auth] meegle status: %s authenticated=%s host=%s reason=%s exit=%s",
                self._name(open_id), ok, data.get("host", ""), reason, exit_code,
            )
            return {
                "authenticated": ok,
                "host": data.get("host", ""),
                "reason": reason,
                "exit_code": exit_code,
                "retryable": retryable,
            }
        except (json.JSONDecodeError, TypeError, subprocess.SubprocessError) as e:
            logger.warning("[auth] meegle status failed for %s: %s", self._name(open_id), e)
            return {
                "authenticated": False,
                "host": "",
                "reason": "status_probe_error",
                "exit_code": 2,
                "retryable": True,
            }

    def is_meegle_authenticated(self, open_id: str) -> bool:
        return bool(self.meegle_auth_status(open_id).get("authenticated"))
```

然后为 device-code 成功路径新增短重试验证 helper：

```python
    def wait_for_meegle_authenticated(self, open_id: str, attempts: int = 3, delay_seconds: float = 0.5) -> bool:
        for attempt in range(attempts):
            status = self.meegle_auth_status(open_id)
            if status.get("authenticated"):
                return True
            if status.get("retryable"):
                time.sleep(delay_seconds)
                continue
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
        return False
```

如果 `src/auth.py` 顶部还没有 `import time`，则新增。

保持 `poll_meegle_once` 只在以下分支中标记 authorized：

```python
        if self.wait_for_meegle_authenticated(open_id):
            self.store.mark_meegle_authorized(open_id)
            return True
```

不要新增任何仅基于 poll 命令退出码或 stdout 就标记 authorized 的路径。

- [ ] **步骤 4：运行测试**

运行：

```bash
pytest tests/test_auth.py -v
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add src/auth.py tests/test_auth.py
git commit -m "fix: verify Meegle credentials before authorizing"
```

---

### 任务 4：创建统一授权完成函数

**文件：**
- 修改：`src/main.py`
- 测试：`tests/test_main.py`

- [ ] **步骤 1：为单消费者 resume 编写失败测试**

把以下测试加入 `tests/test_main.py`：

```python
def test_complete_auth_and_resume_claims_original_text_once(store, auth, agent):
    from src.main import _complete_auth_and_resume

    store.create_auth_resume_job(
        context_id="ou_u1",
        provider="meegle",
        device_code="dev1",
        client_id="client1",
        resume_text="original request",
        reply_id="reply1",
        thread_key="thread1",
        root_id="root1",
        chat_id="oc_chat",
        chat_type="p2p",
        existing_msg_id="om_auth",
    )

    with patch("src.main._stream_claude", return_value="om_auth") as stream:
        first = _complete_auth_and_resume(
            open_id="ou_u1",
            context_id="ou_u1",
            provider="meegle",
            device_code="dev1",
            auth=auth,
            store=store,
            agent=agent,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            owner="pod-a",
        )
        second = _complete_auth_and_resume(
            open_id="ou_u1",
            context_id="ou_u1",
            provider="meegle",
            device_code="dev1",
            auth=auth,
            store=store,
            agent=agent,
            app_id=APP_ID,
            app_secret=APP_SECRET,
            owner="pod-b",
        )

    assert first is True
    assert second is False
    stream.assert_called_once()
    assert stream.call_args.args[1] == "original request"
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
pytest tests/test_main.py::test_complete_auth_and_resume_claims_original_text_once -v
```

预期：失败，因为 `_complete_auth_and_resume` 尚不存在。

- [ ] **步骤 3：新增 `_complete_auth_and_resume`**

把以下函数加到 `src/main.py` 的 auth poller helper 附近：

```python
def _complete_auth_and_resume(
        open_id: str,
        context_id: str,
        provider: str,
        device_code: str,
        auth: AuthManager,
        store: UserStore,
        agent: Agent,
        app_id: str,
        app_secret: str,
        owner: str = "") -> bool:
    """Claim and resume the original request after auth completes.

    Returns True if this pod claimed and resumed the pending request.
    Returns False if another pod already claimed it or no durable resume job exists.
    """
    _ctx = context_id or open_id
    _owner = owner or f"{os.environ.get('HOSTNAME', '')}:{os.getpid()}"
    job = store.claim_auth_resume_job(_ctx, provider, device_code, _owner)
    if not job:
        logger.info("[auth-flow] no resumable %s auth job claimed for %s", provider, _ctx)
        return False

    try:
        if not job.get("resume_text"):
            logger.info("[auth-flow] claimed %s auth job has empty resume_text: %s", provider, job["id"])
            store.consume_auth_resume_job(job["id"])
            return True

        _stream_claude(
            open_id,
            job["resume_text"],
            agent,
            store,
            app_id,
            app_secret,
            existing_msg_id=job.get("existing_msg_id") or None,
            context_id=_ctx,
            reply_msg_id=(job.get("reply_id") or None) if not job.get("existing_msg_id") else None,
            thread_session_key=job.get("thread_key") or None,
            root_id=job.get("root_id") or "",
            chat_id=job.get("chat_id") or "",
            chat_type=job.get("chat_type") or "p2p",
        )
        store.consume_auth_resume_job(job["id"])
        return True
    except Exception as e:
        store.fail_auth_resume_job(job["id"], str(e))
        raise
```

调用方必须显式处理 `False`。对于没有持久化 job 的旧 pending flow，poller 应更新授权卡片，或发送一条简短消息提示用户授权后重新发送请求。授权成功后不能静默丢弃用户请求。

- [ ] **步骤 4：运行测试**

运行：

```bash
pytest tests/test_main.py::test_complete_auth_and_resume_claims_original_text_once -v
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add src/main.py tests/test_main.py
git commit -m "feat: add unified auth completion resume"
```

---

### 任务 5：启动授权时持久化恢复任务

**文件：**
- 修改：`src/main.py`
- 测试：`tests/test_main.py`

- [ ] **步骤 1：为新用户 Lark 授权编写失败测试**

把以下测试加入 `tests/test_main.py`：

```python
def test_new_user_auth_persists_resume_job(store, auth, agent):
    event = _event("ou_new_resume", text="original lark request", message_id="mid_resume")
    mock_auth_executor = MagicMock()

    with patch.object(auth, "is_authenticated", return_value=False), \
         patch.object(auth, "start_auth", return_value=_FAKE_AUTH_DATA):
        handle_message(
            event, store, auth, agent,
            APP_ID, APP_SECRET, MagicMock(),
            auth_executor=mock_auth_executor,
        )

    claimed = store.claim_auth_resume_job(
        context_id="ou_new_resume",
        provider="lark",
        device_code=_FAKE_AUTH_DATA["device_code"],
        owner="test",
    )
    assert claimed is not None
    assert claimed["resume_text"] == "original lark request"
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
pytest tests/test_main.py::test_new_user_auth_persists_resume_job -v
```

预期：失败，因为当前还不会创建 auth resume job。

- [ ] **步骤 3：提交 `_start_auth_and_poll` 前写入 resume job**

在 `handle_message` 中，`lark_data = auth.start_auth(context_id)` 成功之后、`auth_executor.submit(_start_auth_and_poll, ...)` 之前，加入：

```python
                    store.create_auth_resume_job(
                        context_id=context_id,
                        provider="lark",
                        device_code=lark_data["device_code"],
                        client_id="",
                        resume_text=text,
                        reply_id=reply_msg_id or "",
                        thread_key=thread_session_key or "",
                        root_id=root_id,
                        chat_id=chat_id,
                        chat_type=chat_type,
                    )
```

Claude 返回后，如果检测到 Meegle pending 状态，在提交 `_poll_meegle_and_resume` 前加入：

```python
            store.create_auth_resume_job(
                context_id=context_id,
                provider="meegle",
                device_code=m_code,
                client_id=m_client,
                resume_text=text,
                reply_id=reply_msg_id or "",
                thread_key=thread_session_key or "",
                root_id=root_id,
                chat_id=chat_id,
                chat_type=chat_type,
                existing_msg_id=_claude_card_id or "",
            )
```

Claude 返回后如果触发 Lark reauth，也加入相同调用，其中 `provider="lark"`、`device_code=l_code`、`client_id=""`，并设置 `existing_msg_id=_claude_card_id or ""`。

对于 poller 函数内部创建的授权卡片，发出卡片后立即把 card id 回写到 job：

```python
    if hasattr(store, "set_auth_resume_existing_msg_id"):
        store.set_auth_resume_existing_msg_id(_ctx, "lark", device_code, card_id)
```

Meegle poller 使用 `provider="meegle"`。这样可以保留当前 UX：恢复后的 Claude 回复复用原授权卡片，而不是额外发送一张不相关的新卡片。

- [ ] **步骤 4：运行测试**

运行：

```bash
pytest tests/test_main.py::test_new_user_auth_persists_resume_job -v
```

预期：通过。

- [ ] **步骤 5：提交**

```bash
git add src/main.py tests/test_main.py
git commit -m "feat: persist auth resume jobs when starting auth"
```

---

### 任务 6：让所有授权成功路径都走“认领并恢复”流程

**文件：**
- 修改：`src/main.py`
- 测试：`tests/test_main.py`
- 测试：`tests/test_multipod_coordination.py`

- [ ] **步骤 1：编写 inline poll 回归测试**

把以下测试加入 `tests/test_multipod_coordination.py`：

```python
def test_inline_pending_poll_resumes_original_job_not_current_message():
    store = LockingStore()
    store.users["ou_test"] = {
        "auth_status": "pending",
        "pending_code": "dev1",
        "pending_url": "http://auth",
        "pending_at": "2026-06-08T00:00:00+00:00",
        "session_id": None,
        "meegle_auth_status": "none",
    }
    store.auth_jobs = []

    def create_auth_resume_job(**kwargs):
        store.auth_jobs.append({"id": "job1", "status": "pending", **kwargs})
        return "job1"

    def claim_auth_resume_job(context_id, provider, device_code, owner):
        for job in store.auth_jobs:
            if job["status"] == "pending" and job["context_id"] == context_id and job["provider"] == provider and job["device_code"] == device_code:
                job["status"] = "claimed"
                return job
        return None

    store.create_auth_resume_job = create_auth_resume_job
    store.claim_auth_resume_job = claim_auth_resume_job
    store.consume_auth_resume_job = lambda job_id: None
    store.fail_auth_resume_job = lambda job_id, error: None
    store.auth_jobs.append({
        "id": "job1",
        "context_id": "ou_test",
        "provider": "lark",
        "device_code": "dev1",
        "resume_text": "original message",
        "reply_id": "",
        "thread_key": "",
        "root_id": "",
        "chat_id": "oc_p2p",
        "chat_type": "p2p",
        "existing_msg_id": "",
        "status": "pending",
    })

    auth = MagicMock()
    auth.poll_once.return_value = True
    agent = MagicMock()

    with patch("src.main._stream_claude_inner", return_value="card_1") as stream:
        handle_message(_event("mid_current") | {"text": "[图片]"}, store, auth, agent, "app", "secret")

    stream.assert_called_once()
    assert stream.call_args.args[1] == "original message"
```

- [ ] **步骤 2：运行测试并确认失败**

运行：

```bash
pytest tests/test_multipod_coordination.py::test_inline_pending_poll_resumes_original_job_not_current_message -v
```

预期：失败，因为当前 inline pending 会 fall through 并 stream 当前消息。

- [ ] **步骤 3：替换授权成功后的直接 stream**

在以下成功分支中，将直接调用 `_stream_claude(open_id, original_text, ...)` 替换为 `_complete_auth_and_resume(...)`：

- `_recover_pending_auth_and_resume`
- `_poll_lark_reauth_and_resume`
- `_poll_meegle_and_resume`
- `_start_auth_and_poll`
- `_start_meegle_auth_and_poll`
- `_start_combined_auth_and_poll` `_try_finish`
- `handle_message` inline pending branch after `auth.poll_once(...)` succeeds

对于 `handle_message` 中的 inline pending 分支，把：

```python
            if code and auth.poll_once(context_id, code):
                logger.info(f"[auth-flow] {_dname} poll_once succeeded inline, continuing")
                pass
```

替换为：

```python
            if code and auth.poll_once(context_id, code):
                logger.info(f"[auth-flow] {_dname} poll_once succeeded inline, completing durable resume")
                _complete_auth_and_resume(
                    open_id=open_id,
                    context_id=context_id,
                    provider="lark",
                    device_code=code,
                    auth=auth,
                    store=store,
                    agent=agent,
                    app_id=app_id,
                    app_secret=app_secret,
                )
                return
```

对于 poller，调用 `_complete_auth_and_resume(...)` 并在运行后 return。非成功路径仍保留卡片 timeout 处理。

- [ ] **步骤 4：运行协调相关测试**

运行：

```bash
pytest tests/test_multipod_coordination.py tests/test_main.py -v
```

预期：为新 store 方法更新 helper fake 后，测试通过。

- [ ] **步骤 5：提交**

```bash
git add src/main.py tests/test_main.py tests/test_multipod_coordination.py
git commit -m "fix: route auth success through durable resume claim"
```

---

### 任务 7：将 Meegle DB 状态与真实 CLI 凭证对齐

**文件：**
- 修改：`src/user_store.py`
- 修改：`src/main.py`
- 测试：`tests/test_meegle_auth_flow.py`

- [ ] **步骤 1：为陈旧 authorized DB 状态添加失败测试**

创建 `tests/test_meegle_auth_flow.py`：

```python
from unittest.mock import MagicMock, patch

from src.main import handle_message


def test_meegle_authorized_db_but_cli_false_triggers_auth_not_create(store, auth, agent):
    store.upsert_user("ou_u1", auth_status="authorized", meegle_auth_status="authorized")
    auth.is_authenticated.return_value = True
    auth.is_meegle_authenticated.return_value = False

    event = {
        "open_id": "ou_u1",
        "text": "create a requirement in feishu project",
        "message_id": "mid1",
        "chat_id": "oc_p2p",
        "chat_type": "p2p",
    }

    with patch("src.main._stream_claude", return_value="card") as stream:
        handle_message(event, store, auth, agent, "app", "secret", MagicMock())

    stream.assert_called_once()
    assert store.get_user("ou_u1")["meegle_auth_status"] == "none"
```

- [ ] **步骤 2：运行测试**

运行：

```bash
pytest tests/test_meegle_auth_flow.py::test_meegle_authorized_db_but_cli_false_triggers_auth_not_create -v
```

预期：在 reconcile 实现前失败。

- [ ] **步骤 3：对陈旧凭证复用现有 `none` 状态**

第一版不要引入新的 `meegle_auth_status="invalid"` 状态。虽然该列是 text，技术上可行，但会削弱与当前代码的回滚兼容性。使用 `store.reset_meegle_auth(context_id)` 表达状态回到未授权，用结构化日志记录 invalid/stale 原因。

- [ ] **步骤 4：在 Meegle 敏感 stream 前做 reconcile**

在 `handle_message` 中，加载 `user` 之后、调用 Claude 之前，仅当消息可能与 Meegle 相关时加入轻量 reconcile：

```python
        _mentions_meegle = any(s in text.lower() for s in ("meegle", "飞书项目", "需求", "工作项", "project"))
        if _mentions_meegle and user and user.get("meegle_auth_status") == "authorized":
            try:
                status = auth.meegle_auth_status(context_id) if hasattr(auth, "meegle_auth_status") else {"authenticated": auth.is_meegle_authenticated(context_id), "retryable": False, "reason": ""}
                if status.get("retryable"):
                    logger.warning("[meegle-auth] status probe retryable for %s; keeping DB state reason=%s", context_id, status.get("reason", ""))
                elif not status.get("authenticated"):
                    logger.warning("[meegle-auth] DB authorized but CLI status false for %s; resetting DB state reason=%s", context_id, status.get("reason", ""))
                    store.reset_meegle_auth(context_id)
                    user = store.get_user(context_id) or user
            except Exception as e:
                logger.warning("[meegle-auth] credential reconciliation failed for %s: %s", context_id, e)
```

这里不直接启动 OAuth。它的作用是防止 DB 向后续逻辑提供错误状态，并给 Claude 一个干净的 `authenticated=false` 路径。

- [ ] **步骤 4.5：保持有效 Meegle 凭证稳定**

当 `auth.is_meegle_authenticated(context_id)` 返回 true 时，不 reset、不 revoke Meegle。reconcile 规则刻意保持单向：

```text
DB authorized + CLI authenticated=true  -> keep state
DB authorized + CLI authenticated=false + non-retryable reason -> reset DB state only, do not logout
DB authorized + CLI status retryable/network failure -> keep DB state, ask user to retry later, do not reauth
DB pending + expired pending_at         -> reset DB pending state only, do not logout
permission/MCP feature error            -> keep auth state, do not logout, do not reauth
```

这样可以在 Meegle session 有效时尽量长期保留，同时在凭证陈旧时允许用户恢复。

- [ ] **步骤 5：运行测试**

运行：

```bash
pytest tests/test_meegle_auth_flow.py tests/test_main.py tests/test_auth.py -v
```

预期：通过。

- [ ] **步骤 6：提交**

```bash
git add src/user_store.py src/main.py tests/test_meegle_auth_flow.py
git commit -m "fix: reconcile Meegle DB state with CLI credentials"
```

---

### 任务 8：明确 `/reset` 与 `/meegle-reauth` 的边界

**文件：**
- 修改：`src/main.py`
- 修改：`tests/test_main.py`

- [x] **步骤 1：新增 reset 不修改授权状态的回归测试**

把以下测试加入 `tests/test_main.py`：

```python
def test_reset_does_not_change_meegle_auth_state(store, auth, agent):
    store.upsert_user("ou_meegle_reset", auth_status="authorized",
                      meegle_auth_status="authorized", session_id="old-session")

    with patch("src.main.send_feishu_message"):
        handle_message(
            _event("ou_meegle_reset", text="/reset", message_id="mid_reset"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    user = store.get_user("ou_meegle_reset")
    assert user["session_id"] is None
    assert user["meegle_auth_status"] == "authorized"
```

- [x] **步骤 2：新增 `/meegle-reauth` 才重置 Meegle 授权状态的回归测试**

```python
def test_meegle_reauth_resets_meegle_auth_state(store, auth, agent):
    store.upsert_user("ou_meegle_reauth", auth_status="authorized",
                      meegle_auth_status="authorized", session_id="keep-session",
                      meegle_pending_code="old-code")

    with patch.object(auth, "revoke_meegle_token") as revoke, \
         patch("src.main.send_feishu_message"):
        handle_message(
            _event("ou_meegle_reauth", text="/meegle-reauth", message_id="mid_meegle_reauth"),
            store, auth, agent, APP_ID, APP_SECRET, MagicMock(),
        )

    revoke.assert_called_once_with("ou_meegle_reauth")
    user = store.get_user("ou_meegle_reauth")
    assert user["session_id"] == "keep-session"
    assert user["meegle_auth_status"] == "none"
```

- [x] **步骤 3：实现边界**

实现原则：

- `/reset` 只清 Claude session / thread session。
- `/reset` 不调用 `auth.meegle_auth_status()`，不调用 `store.reset_meegle_auth()`，不吊销任何 token。
- `/meegle-reauth` 是 Meegle 授权重置入口，允许受控调用 `auth.revoke_meegle_token(context_id)` 并清理 DB Meegle 状态。
- 使用 Meegle 前的 DB/CLI reconcile 保留在普通消息处理路径中，不挂在 `/reset` 上。

- [x] **步骤 4：运行 reset 和 auth 测试**

运行：

```bash
pytest tests/test_main.py tests/test_auth.py -v
```

预期：通过。

- [x] **步骤 5：提交**

```bash
git add src/main.py tests/test_main.py
git commit -m "fix: keep reset separate from Meegle reauth"
```

---

### 任务 9：提升授权状态机可观测性

**文件：**
- 修改：`src/auth.py`
- 修改：`src/main.py`
- 测试：不需要新增单元测试；在开发容器中通过日志验证。

- [ ] **步骤 1：新增结构化日志点**

在以下位置增加日志：

```python
logger.info("[auth-resume] created provider=%s ctx=%s code=%s job=%s", provider, context_id, device_code[:8], job_id)
logger.info("[auth-resume] claimed provider=%s ctx=%s job=%s owner=%s", provider, context_id, job["id"], owner)
logger.info("[auth-resume] skipped provider=%s ctx=%s code=%s reason=no_claim", provider, context_id, device_code[:8])
logger.info("[meegle-auth] status ctx=%s authenticated=%s host=%s", context_id, authenticated, host)
logger.warning("[meegle-auth] permission_error ctx=%s error=%s action=no_reauth", context_id, error_text[:300])
```

使用 `src/main.py` 和 `src/auth.py` 中已有的 logger 实例。

- [ ] **步骤 2：把部署验证命令加入文档**

在 `docs/deployment.md` 中加入一个简短章节：

```markdown
### 授权状态验证

部署后，选择一个受影响用户进行验证：

```bash
U='ou_...'
USER_HOME="${LARK_USERS_DIR:-/var/lark-bot/users}/$U"
gosu botuser env HOME="$USER_HOME" MEEGLE_HOST="${MEEGLE_HOST:-project.feishu.cn}" meegle auth status
find "$USER_HOME/.meegle" -maxdepth 5 -printf '%M %u %g %TY-%Tm-%Td %TH:%TM:%TS %p\n' | sort
grep -a -n "auth-resume\\|meegle-auth" /var/lark-bot/bot.log | tail -120
```
```

- [ ] **步骤 3：提交**

```bash
git add src/auth.py src/main.py docs/deployment.md
git commit -m "chore: add auth state observability"
```

---

### 任务 10：完整验证

**文件：**
- 无代码改动。

- [ ] **步骤 1：运行定向测试**

运行：

```bash
pytest tests/test_user_store.py tests/test_auth.py tests/test_main.py tests/test_multipod_coordination.py tests/test_meegle_auth_flow.py -v
```

预期：通过。

- [ ] **步骤 2：运行完整测试套件**

运行：

```bash
pytest -v
```

预期：通过。

- [ ] **步骤 3：手动进行类生产验证**

在 bot 容器中，使用测试用户/context 验证：

```bash
U='ou_test_user'
USER_HOME="${LARK_USERS_DIR:-/var/lark-bot/users}/$U"
gosu botuser env HOME="$USER_HOME" MEEGLE_HOST="${MEEGLE_HOST:-project.feishu.cn}" meegle auth status
```

授权前预期：`authenticated=false`。

触发一次 Meegle 请求，完成授权后再验证：

```bash
gosu botuser env HOME="$USER_HOME" MEEGLE_HOST="${MEEGLE_HOST:-project.feishu.cn}" meegle auth status
grep -a -n "auth-resume\\|meegle-auth" /var/lark-bot/bot.log | tail -120
```

授权后预期：

- `authenticated=true`
- 只出现一次 `auth-resume claimed`
- 没有 `meegle auth logout`
- 如果返回 `user has not enabled this MCP feature`，bot 不发送新的 OAuth 链接、不吊销 token。读请求报告“无法通过 Meegle CLI 读取”；写请求可提示 MCP 能力或空间权限可能不满足。

- [ ] **步骤 4：提交验证记录**

```bash
git status --short
git commit --allow-empty -m "test: verify auth state refactor"
```

---

## 上线计划

1. 第一阶段先部署任务 2、3、7、8、9。这是 Meegle 止血与状态一致性包：禁止自动 `meegle auth logout`，授权完成前校验真实凭证，使用 Meegle 前 reconcile DB 状态和真实 CLI 凭证状态，明确 `/reset` 只重置对话、`/meegle-reauth` 才重置 Meegle 授权，并补充必要日志。任务 7 必须保留 retryable/network failure 不清 DB 的保护，避免把临时服务异常误判成未授权。任务 10 中与这些改动相关的测试和手动验证必须随本阶段执行。
2. 第二阶段再一起部署任务 1、4、5、6。它们引入 `auth_resume_jobs` 并改变 Lark/Meegle 授权后的恢复状态机，应作为一个整体发布，避免出现只写 job 但不 claim、或只 claim 但未写 job 的半成品状态。
3. 任务 10 不是单独上线内容，而是每个阶段都要执行的验证清单。完整回归应在第二阶段完成后再跑一次。
4. 上线后观察以下日志：
   - `meegle auth logout`：只应在显式 `/meegle-reauth` 流程中作为受控吊销出现。
   - `auth-resume claimed`：每次授权完成应只有一个。
   - `no_claim`：当输掉竞争的 pod 在另一个 pod claim job 后检测到授权完成时，这是可接受的。
   - `DB authorized but CLI status false`：受影响用户重新授权一次或凭证完成 reconcile 后，这类日志应下降。

## 向后兼容性

- 已有用户如果 `.meegle/credentials.enc` 和 `.machine-key` 有效，不需要重新授权。
- 已有用户如果 DB 显示 `meegle_auth_status=authorized`，但在实际 Meegle 请求前 CLI status 因非重试原因显示 `authenticated=false`，则会被重置为 `meegle_auth_status=none`，并在后续 Meegle OAuth 流程中要求重新授权一次。
- `/reset` 只重置 Claude 对话/session，不修改 Lark 或 Meegle 授权状态。Meegle 授权重置必须使用 `/meegle-reauth`，该命令会受控吊销 Meegle token 并清理 DB Meegle 状态。
- 只要真实 CLI 凭证有效，Meegle 应尽量保持稳定。部署不得删除或轮换 `.meegle/.machine-key`、`.meegle/credentials.enc` 或 user home 路径。应用不能承诺 Meegle 会像 Lark 的 refresh-token flow 一样表现，因为 Meegle CLI 没有暴露完全可比的 expiry/refresh 元数据。
- `user has not enabled this MCP feature` 已确认为 Meegle 后端问题时，bot 不应触发重新授权，也不应吊销 token。查询/读取类请求只报告“无法通过 Meegle CLI 读取”，写操作才可提示 MCP 能力或空间权限可能不满足。
- 部署前已经启动的 pending auth flow 可能没有 `auth_resume_jobs` 行。对于这些 flow，代码应记录 `no_claim` 并要求用户授权后重新发送。这在 rollout 期间可接受，并且能避免重复回复。

## 自检

- 需求覆盖：本计划覆盖原始 Lark recovery staleness bug、inline 恢复错误消息竞态、Meegle logout 循环、DB/credential 不一致、群聊/P2P context 感知、测试、可观测性和上线策略。
- 占位符扫描：没有实现步骤依赖未定义的方法名；新方法和签名都在使用前定义。
- 类型一致性：`provider`、`device_code`、`client_id`、`resume_text`、`reply_id`、`thread_key`、`root_id`、`chat_id`、`chat_type` 和 `existing_msg_id` 在 store、main 和 tests 中保持一致。
