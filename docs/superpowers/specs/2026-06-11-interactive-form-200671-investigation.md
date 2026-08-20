# 交互表单 200671 问题排查报告

日期：2026-06-11  
分支：`feature/interactive-form-card-actions`  
表单实验提交：`1b30233 实现交互表单长连接实验版本`  
线上回滚版本：`master` 已回到 `0bba628d`

## 背景

本轮目标是实现机器人自动回复可交互表单，并评估升级 `lark-cli` 的影响。表单采用飞书交互卡片，用户在卡片中补充需求、偏好或实现决策，提交后机器人继续处理原任务。

表单触发权交给模型判断，工程侧只负责渲染表单、接收回调、保存答案并把结构化答案交回后续任务处理。

模型侧提示约束为：

```text
Use this tool when you need to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.
```

## 当前问题

飞书交互卡片在点击“提交本题 / 上一题”时，客户端频繁出现 `200671`。

飞书文档对 `200671` 的说明是：请求的卡片回调服务返回了非 HTTP 200 的状态码，导致无法进行正常的卡片交互。

但本地日志与这个表面含义不一致。多次测试中，服务端日志显示：

- 卡片动作已经进入 SDK 长连接入口。
- 后端已经返回 `code=200`。
- 异步更新或同步返回卡片也成功执行。

因此当前问题不像是“服务没有收到回调”或“服务端真的返回非 200”，更像是飞书平台或客户端对 SDK 长连接卡片动作响应的解释、卡片结构、回传参数类型、或配置状态存在兼容问题。

## 当前实现架构

### 总体架构

当前表单实验版本采用“双长连接 + 本地表单状态”的架构：

- 普通消息入口：`lark-cli event +subscribe`
- 卡片动作入口：`lark_oapi.ws.Client` SDK 长连接
- 表单状态：本地 `FormStore`
- 表单创建：内部 HTTP API `/interactive-form/create`

### 普通消息入口

普通用户消息仍走原有链路：

1. `src/event_listener.py` 启动 `lark-cli event +subscribe`。
2. 接收 `im.message.receive_v1`。
3. 进入 `src/main.py` 的机器人处理流程。
4. 模型判断是否需要表单。
5. 需要表单时，通过内部 API 创建表单卡片。

### 表单创建流程

表单创建入口在 `src/internal_api.py`：

```text
POST /interactive-form/create
```

主要流程：

1. 模型生成表单 schema，包括标题、问题列表、题目类型、选项、自定义输入框标签。
2. `src/interactive_form_service.py` 校验 schema。
3. `src/form_store.py` 创建 session，保存 schema、当前题号、答案、操作者、卡片消息 ID。
4. `src/card_forms.py` 渲染飞书交互卡片。
5. `src/feishu_api.py` 调飞书消息 API 发送 interactive card。
6. 发送成功后将 `message_id/card_id` 写回 session。

### 卡片形态

当前实验版是一题一卡片视图：

- 每次只展示一道题。
- 单选和多选都用 `checker` 平铺展示。
- 底部有输入框。
- 单选时，如果输入框有内容，以输入框为准。
- 多选时，输入框内容追加到选择结果中。
- 第一题没有“上一题”。
- 后续题有“上一题”。
- “提交本题”在 `form` 内，带 `form_action_type=submit`。
- “上一题”在 `form` 外，只做 callback，不提交表单。

题目不会根据前面答案动态变化，因此返回上一题不会清除后续答案。

### 卡片动作入口

卡片点击事件不走 `lark-cli event +subscribe`，而是走 SDK 长连接：

```text
src/card_action_listener.py
```

注册的核心回调是：

```text
p2_card_action_trigger
```

卡片动作会被解析成内部结构：

```text
CardActionEvent(
  session_id,
  action,
  question_index,
  operator_open_id,
  form_value,
  callback_token,
  message_id,
  event_id
)
```

随后交给：

```text
InteractiveFormHandler.handle_action()
```

### 状态推进

`InteractiveFormHandler` 负责：

- 校验 session 是否存在。
- 校验点击人是否是表单拥有者。
- 校验点击题号是否是当前题。
- 单选题如果勾选多个选项，返回 toast 错误。
- `submit`：保存当前题答案，推进到下一题。
- `previous`：回到上一题。
- 最后一题提交后，标记 session 完成并触发后续 agent 任务。

### 卡片更新模式

实验中保留了两种更新模式：

1. `deferred` 默认模式  
   卡片动作回调只轻量 ACK，不同步返回新卡片。随后使用 callback token 调 `/interactive/v1/card/update` 异步更新当前卡片。

2. `sync` 实验模式  
   通过 `BOT_FORM_CARD_UPDATE_MODE=sync` 开启。卡片动作回调直接在 SDK response 中返回下一题卡片，不走 callback token 延时更新。

两种模式都已经验证过，仍然会出现 `200671`。

### 完成后的后续处理

表单全部提交后：

1. 渲染完成态卡片。
2. `src/main.py` 的 form completion runner 将结构化答案拼成后续 prompt。
3. 继续调用原有 `Agent` 处理用户最初任务。
4. 后续回复仍走原来的机器人消息链路。

## 已做尝试

### 1. 排查双长连接抢消息

曾发现 SDK 长连接错误注册了 `im.message.receive_v1`，导致普通消息被 SDK 消费，`lark-cli event +subscribe` 收不到消息，机器人不回复。

处理：

- 移除 SDK 侧 `im.message.receive_v1` 注册。
- 在普通消息 listener 增加 unhandled event 日志。
- 在 SDK listener 增加 ignored event 日志。

结论：

- 普通消息被抢的问题已修复。
- 后续 `200671` 测试中，没有看到卡片动作被普通消息长连接误收。

### 2. 调整卡片结构

曾怀疑按钮和表单结构导致回传异常。

处理：

- 只有 `submit_btn` 保留 `form_action_type=submit`。
- `previous_btn` 移到 `form` 外。
- `form` 内只保留题目控件、输入框、提交按钮。

结论：

- 结构更清晰，但 `200671` 仍然出现。

### 3. 轻量 ACK + callback token 异步更新

曾怀疑回调响应体过大或处理太慢。

处理：

- 卡片动作回调立即返回轻量 ACK。
- 不同步返回 card。
- 通过 callback token 调 `/interactive/v1/card/update` 异步更新卡片。
- 无 toast、无 card 时，返回 `None`，使 SDK 写回为 `data_present=False`。

关键日志：

```text
Card action response built: response_type=NoneType has_toast=False has_card=False
Lark SDK websocket write completed: code=200 data_present=False payload_bytes=13
Interactive card updated by callback token
```

结论：

- 后端 ACK 是 200。
- 异步更新成功。
- `200671` 仍然出现。

### 4. 同步返回新卡片

曾怀疑 callback token 延时更新与客户端状态冲突。

处理：

- 增加 `BOT_FORM_CARD_UPDATE_MODE=sync`。
- 卡片动作回调直接返回下一题 card。
- 不走 callback token 异步更新。

关键日志：

```text
Returning form card in callback response
Card action response built: response_type=P2CardActionTriggerResponse has_toast=False has_card=True
Lark SDK websocket write completed: code=200 data_present=True
```

结论：

- 同步返回 card 也成功。
- `200671` 仍然出现。
- 因此问题不是单纯由异步更新路径造成。

### 5. 回滚隔离

处理：

- `master` 已还原到线上版本 `0bba628d`。
- 表单相关代码保存到 `feature/interactive-form-card-actions`。
- 表单实验提交为 `1b30233`。

结论：

- 线上分支恢复干净。
- 后续排查应只在表单实验分支进行。

## 当前结论

目前已基本排除：

- 后端没有收到卡片动作。
- 后端真的返回非 200。
- 普通消息长连接抢走卡片动作。
- callback token 异步更新是唯一原因。
- 空 callback response object 是唯一原因。

当前更可能是以下类别的问题：

1. SDK 长连接对卡片动作回调的响应格式与飞书客户端预期不完全兼容。
2. 当前卡片 JSON 中某些元素组合不兼容，例如 `form + checker + button behaviors + form_action_type`。
3. 卡片回传参数虽然是对象，但某些字段类型、位置或结构仍不符合飞书实际要求。
4. 飞书开放平台配置存在状态不一致或不可见配置问题。
5. 飞书客户端或平台内部误报，服务端已经 200，但客户端仍显示 `200671`。
6. 双长连接架构有隐性风险，但当前 `200671` 没有日志证据指向它。

## 仍需验证的问题可能性

### A. 最小卡片是否也报错

需要构造最小卡片：

- 一个普通按钮。
- 只带 callback。
- 不带 `form`。
- 不带 `checker`。
- 不带 input。
- 后端只返回简单 ACK 或 toast。

如果最小卡片也报 `200671`，问题更可能在 SDK 长连接卡片动作能力、开放平台配置或平台侧。

如果最小卡片正常，再逐步加回：

1. `form`
2. `input`
3. `checker`
4. `form_action_type=submit`
5. 同步返回 card
6. callback token 异步更新

以定位具体卡片结构。

### B. HTTP callback 是否正常

如果同样的最小卡片通过 HTTP callback 正常，而 SDK 长连接报错，则可以明确问题在 SDK 长连接路径。

但运维认为 HTTP callback 安全性较差，当前优先级低于长连接排查。

### C. 卡片参数类型是否完全对象化

飞书文档提示 SDK 仅支持对象类型卡片回传参数，不兼容字符串类型。

当前按钮 `value` 是对象，但仍需确认：

- 所有 `behaviors.value` 都是对象。
- checker 的 `value` 是否可能被平台视为字符串回传参数。
- form_value 中 checker 字段是否和 SDK 期望一致。

### D. 多长连接集群语义

飞书长连接同一应用多客户端是集群模式，不广播，只会随机一个客户端收到消息。

当前本地双长连接包括：

- `lark-cli event +subscribe`
- SDK card action listener

已知它曾导致普通消息被 SDK 抢走，但移除 SDK 的普通消息注册后，当前 `200671` 没有表现为卡片动作被另一个长连接抢走。

如果线上多 pod 部署，每个 pod 都开 SDK 长连接，则仍需评估：

- 卡片动作会随机进入某个 pod。
- 表单 session 如果只在本地内存，会有跨 pod 状态丢失风险。
- 需要共享存储或单实例消费策略。

## 建议下一步

优先在 `feature/interactive-form-card-actions` 上做最小卡片实验：

1. 新增一个内部接口发送最小 callback 卡片。
2. SDK 回调只识别最小按钮动作并返回 toast。
3. 观察是否出现 `200671`。
4. 如果仍报错，转向 SDK 长连接/开放平台配置排查。
5. 如果不报错，逐步加回当前表单元素，定位具体不兼容结构。

在没有完成最小卡片验证前，不建议继续在现有复杂表单上叠加补丁。
