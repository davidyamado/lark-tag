# Interactive Form SDK Long Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现由模型主动触发的飞书交互卡片表单，并通过 Python SDK 长连接接收卡片动作回调。

**Architecture:** Claude Code 通过内部 HTTP API 调用 `request_interactive_form` 语义工具，工程侧校验 schema、创建 session、发送第一题卡片并结束本轮。卡片动作由 SDK 长连接入口进入表单状态机，完成最后一题后把结构化答案作为 follow-up 回传同一会话的 Claude。

**Tech Stack:** Python 3、PostgreSQL/psycopg2、Feishu HTTP API、`lark-oapi` SDK、pytest。

---

### Task 1: 纯表单模型与卡片渲染

**Files:**
- Create: `src/card_forms.py`
- Create: `tests/test_card_forms.py`

- [ ] **Step 1: Write the failing tests**

覆盖 schema 校验、单选自定义覆盖、复选自定义追加、第一题不显示上一题、后续题显示上一题、回到已答题预填。

- [ ] **Step 2: Run tests to verify red**

Run: `python -m pytest tests/test_card_forms.py -q`
Expected: FAIL because `src.card_forms` does not exist.

- [ ] **Step 3: Implement minimal model**

实现 `validate_form_schema()`、`normalize_answer()`、`render_question_card()`、`build_followup_prompt()`。

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_card_forms.py -q`
Expected: PASS.

### Task 2: 表单持久化状态

**Files:**
- Create: `src/form_store.py`
- Create: `tests/test_form_store.py`

- [ ] **Step 1: Write the failing tests**

覆盖 session 创建、幂等事件、card sequence 递增、previous 不清答案、submit 覆盖当前答案、最后一题 `active -> returning`。

- [ ] **Step 2: Run tests to verify red**

Run: `python -m pytest tests/test_form_store.py -q`
Expected: FAIL because `src.form_store` does not exist.

- [ ] **Step 3: Implement store**

按仓库 inline migration 风格创建 `form_sessions` 与 `form_action_events`，提供 `create_session()`、`get_session()`、`record_event()`、`apply_previous()`、`apply_submit()`、`mark_completed()`、`mark_failed()`。

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_form_store.py -q`
Expected: PASS or SKIP when `POSTGRES_TEST_URL` is not set.

### Task 3: 内部 API 表单创建工具

**Files:**
- Modify: `src/internal_api.py`
- Modify: `src/agent.py`
- Create: `tests/test_internal_api_forms.py`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Write failing tests**

验证 Agent system prompt 包含 `request_interactive_form` 说明与 `/interactive-form/create` curl 示例；验证内部 API 路由调用表单服务并校验 bearer token。

- [ ] **Step 2: Run tests to verify red**

Run: `python -m pytest tests/test_agent.py tests/test_internal_api_forms.py -q`
Expected: FAIL because prompt/route missing.

- [ ] **Step 3: Implement API contract**

新增 `/interactive-form/create`，请求体包含 `open_id`、`title`、`questions`、`context`；内部 API 不直接依赖 Feishu 细节，通过注入的 form service 创建卡片。

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_agent.py tests/test_internal_api_forms.py -q`
Expected: PASS.

### Task 4: 卡片动作状态机与 SDK 长连接入口

**Files:**
- Create: `src/card_action_listener.py`
- Create: `tests/test_card_action_listener.py`

- [ ] **Step 1: Write failing tests**

覆盖 SDK 原始事件解析、非原请求人点击不推进、`previous`/`submit` 分发、重复事件 ACK、最后一题触发 follow-up executor。

- [ ] **Step 2: Run tests to verify red**

Run: `python -m pytest tests/test_card_action_listener.py -q`
Expected: FAIL because module does not exist.

- [ ] **Step 3: Implement listener and handler**

实现 SDK import fail-open、事件规范化、`InteractiveFormHandler.handle_action()`；长连接启动由 `start_card_action_listener()` 包装，便于单测 mock。

- [ ] **Step 4: Run tests to verify green**

Run: `python -m pytest tests/test_card_action_listener.py -q`
Expected: PASS.

### Task 5: Feishu API 与主程序接入

**Files:**
- Modify: `src/feishu_api.py`
- Modify: `src/main.py`
- Modify: `requirements.txt`
- Modify: `CLAUDE.md`
- Modify: relevant tests

- [ ] **Step 1: Write failing tests**

验证 `feishu_api` CardKit 请求体，验证 main 启动内部 API 时注入 form service，验证表单完成后调用 `_stream_claude` 传递结构化 follow-up。

- [ ] **Step 2: Run tests to verify red**

Run: `python -m pytest tests/test_main.py tests/test_feishu_api.py -q`
Expected: FAIL for missing integration.

- [ ] **Step 3: Implement integration**

新增 `send_interactive_card()`、`reply_interactive_card_in_thread()`、`update_interactive_card()`；main 初始化 `FormStore` 和 form service，启动 SDK listener，并在线程池中运行 completed follow-up。

- [ ] **Step 4: Full verification**

Run: `python -m pytest`
Expected: PASS, with Postgres integration tests skipped unless `POSTGRES_TEST_URL` is set.

### Self Review

- Spec coverage: 包含模型驱动触发、单题线性 UX、上一题保留后续答案、SDK 长连接入口、多 pod DB 幂等、最终答案回传 Claude；不包含 `lark-cli latest` 升级。
- Placeholder scan: 本计划所有任务都有具体文件、行为与命令。
- Type consistency: 表单 schema 使用 `title/questions/options/custom_input_label`；状态值使用 `active/returning/completed/failed`；动作值使用 `submit/previous`。
