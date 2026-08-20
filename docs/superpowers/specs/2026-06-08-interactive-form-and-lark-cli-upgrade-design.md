# 机器人交互表单与 lark-cli 升级设计

**日期**：2026-06-08  
**状态**：已批准，待实施计划  

---

## 背景与目标

当前机器人主要通过文本消息驱动 Claude，再由 Claude 调用 `lark-cli` 完成飞书操作。对于缺少必要字段的请求，纯文本追问容易让用户在聊天中来回补信息，也不利于后端结构化恢复。

本设计的目标是先实现机器人自动回复可交互表单，让用户在飞书卡片内按题补齐信息；同时评估并规划 `lark-cli` 从当前镜像锁定版本升级到 `latest` 的影响。两者不在同一阶段交付，避免同时改变消息入口和表单状态机。

## 决策摘要

1. 第一阶段先做 **Python SDK 长连接卡片动作入口** 和固定线性单题表单。
2. 现有 `lark-cli event +subscribe` 消息监听第一阶段保持不变。
3. 每个 pod 都建立 SDK 长连接，采用 active-active；事件随机落到任意 pod 后由 Postgres 行锁和幂等保证一致性。
4. `lark-cli latest` 升级作为独立阶段处理，重点适配 `event +subscribe` 到 `event consume` 的契约变化。
5. 授权恢复第二阶段 `auth_resume_jobs` 可以在表单阶段后继续推进，表单答案回传给模型后的恢复执行应复用其“可恢复、单消费者”的方向。
6. 表单是否出现由模型判断：工程侧只提供“向用户提问”的工具、渲染卡片、接收提交并把答案回传给模型。

## 范围

### 本轮方案包含

- 新增卡片动作长连接监听入口。
- 新增交互表单状态持久化模型。
- 新增模型可调用的表单提问工具契约和 prompt 约束。
- 新增固定线性单题表单渲染、提交、上一题、答案回传流程。
- 多 pod 幂等和轻量卡片动作回调设计。
- `lark-cli latest` 风险评估和分阶段发布策略。

### 本轮方案不包含

- 第一阶段不升级 `lark-cli` 到 latest。
- 第一阶段不替换现有消息事件监听。
- 第一阶段不做动态分支表单。
- 第一阶段不做最终汇总确认页。
- 第一阶段不由工程侧硬编码判断哪些用户消息要触发表单。
- 第一阶段不把表单状态保存在 pod 内存。

## 用户体验设计

表单采用固定线性单题流程，每张卡片当前只展示一道题。

### 卡片规则

- 标题区域说明当前机器人正在补齐信息。
- 进度区域显示 `问题 N / M`。
- 题目必须标明是单选还是复选。
- 选项使用 `checker` 复选框平铺展开展示，不使用下拉菜单。
- `checker` 选项不绑定回调，只作为本地表单控件；用户勾选/取消勾选时不触发服务端交互。
- 底部始终提供自定义输入框。
- 第一题只显示右侧 `提交本题`。
- 后续题左侧显示 `上一题`，右侧显示 `提交本题`。
- 不显示取消按钮。
- 不在卡片顶部保留已提交答案。
- 最后一题提交后把答案回传给模型，不展示汇总确认。

### 答案规则

- 单选题：如果自定义输入框非空，以用户填写内容为准；否则使用选中的选项。若用户勾选多个选项，提交时提示“当前问题为单选，请选择一个合适的答案”，不保存答案。
- 复选题：提交勾选项，并把自定义输入框内容追加为一个额外答案。
- 点击 `上一题`：仅把当前题号回退一位，不清除后续答案。
- 用户重新提交某一题：只覆盖该题答案，后续已答题目保留。
- 回到已答题目时，卡片应预填之前的选项和自定义输入内容。

## 表单触发与模型工具

工程侧不判断“什么情况下需要提问”。判断权交给模型，由系统 prompt 约束模型在需要收集信息或做选择时调用表单工具。

模型可调用工具暂定为 `request_interactive_form`，语义是“向用户发起一个或多个固定顺序问题，并等待用户在卡片中提交”。工具说明必须包含以下约束：

```text
Use this tool when you need to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.
```

模型调用该工具后，本轮回复结束，不继续执行用户请求。工程侧创建 `form_session`、发送第一题卡片，并等待飞书卡片回调。用户答完最后一题后，工程侧把所有答案作为结构化 follow-up 回传给模型，由模型继续推理、调用 `lark-cli` 或给出最终答复。

工具输入由模型生成，但必须是受约束的结构化 schema：

```json
{
  "title": "创建需求前，请补充信息",
  "questions": [
    {
      "id": "priority",
      "title": "这条需求的优先级是什么？",
      "type": "single",
      "options": [
        {"label": "P0 紧急", "description": "线上阻断或高优故障"},
        {"label": "P1 高", "description": "本周应完成的重要需求"},
        {"label": "P2 普通", "description": "排期处理即可"}
      ],
      "custom_input_label": "其他答案"
    }
  ]
}
```

工程侧只做 schema 校验、渲染、状态持久化、回调接收和答案回传。工程侧不根据业务类型生成问题，也不在用户提交表单后直接替模型执行。

## 技术方案对比

### 方案 A：飞书 HTTP 回调入口

服务暴露 HTTPS callback URL，在飞书开放平台配置事件回调。优点是通用、LB 天然支持多 pod；缺点是需要新增公网入口、URL 校验、验签/解密、Ingress 配置和 3 秒快速响应约束。

### 方案 B：SDK 长连接入口（采用）

每个 pod 使用 Python SDK 建立长连接，并注册新版卡片动作回调 `card.action.trigger`。优点是不需要新增公网 HTTP 回调地址，和参考项目的长连接 cardAction 方向一致；缺点是需要引入 SDK 依赖，并保证每个 pod 都运行同一套 handler。

### 方案 C：等待 lark-cli 支持卡片动作

继续只依赖 `lark-cli event`。优点是部署入口最少；缺点是当前 `lark-cli@latest` 的 event list 未暴露卡片动作事件，无法可靠实现表单提交。

## 推荐架构

```
飞书平台
  ├─ im.message.receive_v1 ──► 现有 lark-cli EventListener ──► handle_message ──► Claude
  │
  └─ card.action.trigger ───► 新 SDK CardActionListener ─────► form session 状态机
                                                           │
                                                           ├─ 轻量 ACK，后台刷新当前卡片
                                                           └─ 最后一题提交后回传答案给 Claude
```

第一阶段只新增第二条链路。消息入口、授权入口、scheduler、Claude streaming 卡片更新保持原有路径。

## 新增组件

### `src/card_forms.py`

负责纯业务表单模型与渲染。

- 定义 `FormQuestion`、`FormAnswer`、`FormSession` 的内部结构。
- 根据 session 当前状态渲染 CardKit JSON。
- 解析 form_value，产出规范化答案。
- 判断是否还有下一题。
- 生成给模型的结构化 follow-up payload。

### `src/card_action_listener.py`

负责 SDK 长连接生命周期。

- 初始化 Python SDK client。
- 注册 `card.action.trigger` handler。
- 把 SDK 事件规范化为内部 `CardActionEvent`。
- 快速调用表单状态机并返回 ACK。
- 出错时记录日志并让 SDK 按官方策略重试。

### `src/form_store.py`

负责 Postgres 持久化与并发控制。

- 创建表单 session。
- 获取 session 并加行锁。
- 保存当前答案。
- 移动 current_index。
- 原子递增 card sequence。
- 记录已处理事件，防止重复提交。

### `src/feishu_api.py`

扩展现有 API 封装。

- 新增创建 CardKit card 的方法。
- 新增发送 card_id 引用消息的方法。
- 新增按 card_id 更新卡片的方法。
- 保留现有 `send_text_card`、`update_card_text`，供 Claude streaming 使用。

## 数据模型

### `form_sessions`

```sql
CREATE TABLE IF NOT EXISTS form_sessions (
    id                 TEXT PRIMARY KEY,
    context_id         TEXT NOT NULL,
    operator_open_id   TEXT NOT NULL,
    chat_id            TEXT NOT NULL DEFAULT '',
    chat_type          TEXT NOT NULL DEFAULT 'p2p',
    reply_msg_id       TEXT NOT NULL DEFAULT '',
    root_id            TEXT NOT NULL DEFAULT '',
    thread_session_key TEXT NOT NULL DEFAULT '',
    message_id         TEXT NOT NULL DEFAULT '',
    card_id            TEXT NOT NULL DEFAULT '',
    card_sequence      INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL DEFAULT 'active',
    current_index      INTEGER NOT NULL DEFAULT 0,
    questions_json     JSONB NOT NULL,
    answers_json       JSONB NOT NULL DEFAULT '{}'::jsonb,
    original_text      TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL,
    updated_at         TIMESTAMPTZ NOT NULL,
    completed_at       TIMESTAMPTZ
);
```

`status` 取值：

- `active`：正在补题。
- `returning`：最后一题已提交，正在把答案回传给模型。
- `completed`：答案已成功回传给模型。
- `failed`：答案回传或卡片更新失败。

### `form_action_events`

```sql
CREATE TABLE IF NOT EXISTS form_action_events (
    event_id      TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    action        TEXT NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL
);
```

用于卡片动作回调幂等。若 SDK 事件缺少稳定 `event_id`，则使用 `session_id + message_id + operator_open_id + action + question_index + submitted_at` 的稳定哈希作为幂等键。

## 回调处理流程

```
SDK 收到 card.action.trigger
      │
      ▼
提取 action.value: session_id / action / question_index
      │
      ▼
校验 operator_open_id == session.operator_open_id
      │
  不匹配 ──► 忽略或更新临时提示
      │
      ▼
插入 form_action_events 幂等记录
      │
  已存在 ──► 直接 ACK
      │
      ▼
SELECT * FROM form_sessions WHERE id = :session_id FOR UPDATE
      │
      ├─ action=previous：current_index -= 1，不改 answers
      │
      └─ action=submit：保存当前题答案
              │
              ├─ 非最后一题：current_index += 1，轻量 ACK 后后台刷新为下一题卡片
              └─ 最后一题：status=returning，轻量 ACK 后后台刷新为已提交卡片，并异步把答案回传给模型
```

handler 内只做快速状态推进、卡片渲染和任务投递；SDK callback 返回空 callback payload 的 200 或 toast，不在 callback response 中携带整张 raw card，不在 SDK callback 中同步 PATCH 卡片，也不在 SDK callback 中同步运行 Claude。后台刷新任务应在 ACK 后短暂延迟再 PATCH，避免飞书尚未完成 action callback 判定时并行更新同一张卡片。

## 多 pod 设计

每个 pod 都建立 SDK 长连接。飞书长连接对多个 client 是集群模式，事件会分发给其中一个连接，而不是广播给所有 pod。因此任意 pod 都可能收到任意表单回调。

为支持 active-active：

- 表单状态全部存在 Postgres。
- 每个回调先写幂等事件表。
- 同一 session 用 `SELECT * FROM form_sessions WHERE id = :session_id FOR UPDATE` 串行化。
- 卡片动作回调只返回轻量 ACK；当前卡片刷新由后台任务调用 `update_interactive_card` 完成，避免 SDK 回包体过大或网络 PATCH 阻塞 action callback。后台任务需在 ACK 后短暂延迟再 PATCH，避免和飞书 action callback 处理并发。
- 后台 PATCH 表单卡片时使用 `form_sessions.card_sequence` 原子递增。
- 最后一题提交后把 `status` 从 `active` 改为 `returning`，只有成功改到 `returning` 的事务可以回传答案。
- 如果 pod 在 `returning` 后崩溃，后续可由恢复任务扫描超时 session 并标记失败或重试回传。

## 安全与权限

- 只有原始请求人可以提交表单。
- 群聊里其他用户点击时不推进状态。
- `action.value` 必须包含不可猜测的 `session_id`，并校验 session 归属。
- 可选增加 HMAC `callback_token`，绑定 `session_id`、`operator_open_id`、`action` 和过期时间。
- 自定义输入框内容按普通用户输入处理，进入 Claude 前保留为结构化文本，不作为命令直接执行。

## 与 Claude 执行的关系

第一版表单让 Claude 自己判断是否需要向用户提问，并由 Claude 生成受约束的表单 schema。工程侧不判断业务场景，也不根据操作类型生成问题。

表单完成后，工程侧把原始用户消息、问题列表和 answers 组合成结构化 follow-up，回传给同一会话中的 Claude。Claude 收到答案后继续推理：可以调用 `lark-cli`、发起授权、继续提问，或给出最终答复。

答案回传后的 Claude 输出复用现有 streaming 卡片能力。第一版可以在表单卡片上显示“已提交，正在继续处理”，再由现有 `_stream_claude` 发送或更新后续结果卡片。

## 与授权恢复的关系

表单提交后触发的 Claude 执行仍可能进入 Lark 或 Meegle 授权流程。因此表单执行入口应和普通消息入口一样传递：

- `context_id`
- `reply_msg_id`
- `thread_session_key`
- `root_id`
- `chat_id`
- `chat_type`
- 原始请求文本和补齐答案

当 `auth_resume_jobs` 第二阶段落地后，表单答案回传后的 Claude 执行也应创建 resume job，保证授权完成后恢复的是带有表单答案的模型 follow-up，而不是某一条中间卡片动作。

## lark-cli latest 升级评估

当前 Dockerfile 锁定 `@larksuite/cli@1.0.9`。本地检查显示 npm latest 为 `1.0.54`，其中事件命令从旧的：

```bash
lark-cli event +subscribe --event-types a,b --compact --as bot --force
```

变成新的：

```bash
lark-cli event consume im.message.receive_v1 --as bot
lark-cli event consume im.chat.member.bot.added_v1 --as bot
```

主要风险：

- 新版一次只消费一个 EventKey，现有 listener 要管理多个 subprocess。
- 新版输出 schema 可能不再契约化提供 `mentions`、`root_id`、`parent_id`，会影响群聊 @、话题上下文和 quote reply。
- 新版 consumer 有 ready marker、stdin EOF 退出和 event bus daemon 语义，旧的 lock 清理与 `--force` 逻辑不适用。
- 需要重写事件进程生命周期测试。

因此 `lark-cli latest` 升级不放入表单第一阶段。

## 发布顺序

### 阶段 1：SDK 长连接表单

- 增加 `lark-oapi` 依赖。
- 新增 `card_action_listener.py`、`card_forms.py`、`form_store.py`。
- 扩展 `feishu_api.py` 的 CardKit card create/update。
- 启动时并行启动现有消息 listener 和新卡片动作 listener。
- 保持 Dockerfile 中 `@larksuite/cli@1.0.9` 不变。

### 阶段 2：授权恢复第二阶段

- 完成 `auth_resume_jobs` 持久化授权恢复状态机。
- 将表单答案回传后的模型执行纳入同一套授权后恢复流程。

### 阶段 3：lark-cli latest 升级

- 将 Dockerfile 明确 pin 到已验证 latest 版本 `@larksuite/cli@1.0.54`。
- 将 `event +subscribe` 适配为多个 `event consume` 子进程。
- 补齐新事件输出 parser 和兼容测试。
- 独立发布，支持快速回滚到上一镜像。

## 测试策略

### 单元测试

- `card_forms`：
  - 单选自定义覆盖选项。
  - 复选自定义追加答案。
  - 第一题不渲染上一题按钮。
  - 后续题渲染上一题和提交按钮。
  - 回到已答题目时预填已保存答案。

- `form_store`：
  - 创建 session。
  - 幂等事件重复提交只处理一次。
  - `FOR UPDATE` 路径下 current_index 串行更新。
  - card_sequence 原子递增。
  - 最后一题只有一个事务能从 `active` 改为 `returning`。

- `card_action_listener`：
  - 解析 SDK form_value。
  - 非原请求人点击不推进状态。
  - submit/previous action 分发正确。

### 集成测试

- P2P 表单从第 1 题到答案回传给模型。
- 群聊表单只允许原请求人提交。
- 两个模拟 pod 同时处理同一 session，只有一个更新成功。
- SDK 回调重复投递不重复回传给 Claude。

### 手动验证

- 飞书开发环境开启长连接订阅。
- 发送需要补字段的请求，确认收到单题表单。
- 单选题填写自定义答案，确认覆盖选项。
- 复选题填写自定义答案，确认追加。
- 回到上一题后再前进，后续答案仍被保留并预填。
- 最后一题提交后答案回传给模型，由模型继续处理。
- 多 pod 环境下连续点击，日志中只有一次答案回传。

## 运维要求

- 不需要新增公网 HTTP callback URL。
- 每个 pod 需要能出网访问飞书开放平台长连接服务。
- 飞书应用需要配置长连接订阅，并包含 `card.action.trigger`。
- 每个 pod 使用同一 App ID/App Secret 初始化 SDK。
- 不要让测试、灰度、生产共用同一个飞书 App；否则事件会随机分发到不同环境。
- pod 数必须低于飞书应用长连接数量限制，并保留运维余量。

## 回滚策略

阶段 1 回滚：

- 回滚镜像即可停止新 SDK 长连接入口。
- 已发送但未完成的表单卡片会失去交互能力；用户可重新发送原请求。
- 数据库新增表保留，不影响旧版本运行。

阶段 3 回滚：

- 使用上一版镜像恢复 `lark-cli@1.0.9` 和 `event +subscribe`。
- 阶段 3 不修改表单数据结构，回滚不影响已上线表单长连接入口。

## 自检

- 范围聚焦：表单长连接与 `lark-cli` 升级拆阶段，没有混合发布。
- 多 pod 明确：每个 pod 建长连接，靠 DB 幂等和行锁保证一致性。
- 用户体验明确：固定线性单题、无取消、无汇总确认、上一题保留后续答案。
- 升级风险明确：`event +subscribe` 到 `event consume` 是独立兼容工作。
