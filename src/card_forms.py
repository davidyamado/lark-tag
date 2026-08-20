import json
from copy import deepcopy
from typing import Any


class FormSchemaError(ValueError):
    """Raised when Claude provides an invalid interactive form schema."""


def _non_empty_text(value: Any, field: str, max_len: int = 200) -> str:
    if not isinstance(value, str):
        raise FormSchemaError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise FormSchemaError(f"{field} must not be empty")
    if len(text) > max_len:
        raise FormSchemaError(f"{field} is too long")
    return text


def validate_form_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the model-provided form schema."""
    if not isinstance(schema, dict):
        raise FormSchemaError("schema must be an object")

    title = _non_empty_text(schema.get("title"), "title")
    questions = schema.get("questions")
    if not isinstance(questions, list) or not questions:
        raise FormSchemaError("questions must be a non-empty list")
    if len(questions) > 10:
        raise FormSchemaError("questions must contain at most 10 items")

    normalized_questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise FormSchemaError(f"questions[{i}] must be an object")
        qid = _non_empty_text(q.get("id"), f"questions[{i}].id", max_len=64)
        if qid in seen_ids:
            raise FormSchemaError(f"duplicate question id: {qid}")
        seen_ids.add(qid)
        qtype = q.get("type")
        if qtype not in ("single", "multi"):
            raise FormSchemaError(f"questions[{i}].type must be single or multi")
        qtitle = _non_empty_text(q.get("title"), f"questions[{i}].title")
        options = q.get("options")
        if not isinstance(options, list) or not options:
            raise FormSchemaError(f"questions[{i}].options must be a non-empty list")
        if len(options) > 12:
            raise FormSchemaError(f"questions[{i}].options must contain at most 12 items")

        normalized_options: list[dict[str, str]] = []
        option_labels: set[str] = set()
        for j, opt in enumerate(options):
            if not isinstance(opt, dict):
                raise FormSchemaError(f"questions[{i}].options[{j}] must be an object")
            label = _non_empty_text(opt.get("label"), f"questions[{i}].options[{j}].label", 80)
            if label in option_labels:
                raise FormSchemaError(f"duplicate option label: {label}")
            option_labels.add(label)
            desc = opt.get("description", "")
            if desc is None:
                desc = ""
            if not isinstance(desc, str):
                raise FormSchemaError(f"questions[{i}].options[{j}].description must be a string")
            normalized_options.append({"label": label, "description": desc.strip()[:200]})

        custom_label = q.get("custom_input_label") or "其他答案"
        custom_label = _non_empty_text(custom_label, f"questions[{i}].custom_input_label", 80)
        normalized_questions.append({
            "id": qid,
            "title": qtitle,
            "type": qtype,
            "options": normalized_options,
            "custom_input_label": custom_label,
        })

    return {"title": title, "questions": normalized_questions}


def _field_name(question_id: str, suffix: str) -> str:
    return f"q_{question_id}_{suffix}"


def _checked_field_name(question_id: str, index: int) -> str:
    return _field_name(question_id, f"opt_{index}")


def _checker_value_is_selected(value: Any, option_label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped == option_label or stripped.lower() in ("true", "1", "yes", "on")
    if isinstance(value, list):
        return option_label in [str(item).strip() for item in value]
    if isinstance(value, dict):
        return _checker_value_is_selected(value.get("checked"), option_label)
    return bool(value)


def selected_from_checkers(question: dict[str, Any], form_value: dict[str, Any]) -> list[str] | None:
    selected = []
    saw_checker_field = False
    for i, opt in enumerate(question.get("options", [])):
        field = _checked_field_name(question["id"], i)
        if field not in form_value:
            continue
        saw_checker_field = True
        if _checker_value_is_selected(form_value.get(field), opt["label"]):
            selected.append(opt["label"])
    return selected if saw_checker_field else None


def normalize_answer(question: dict[str, Any], form_value: dict[str, Any]) -> dict[str, Any]:
    """Normalize one Feishu card submission into the stored answer format."""
    qid = question["id"]
    qtype = question["type"]
    allowed = {opt["label"] for opt in question.get("options", [])}
    custom = str(form_value.get(_field_name(qid, "custom")) or "").strip()
    checked_options = selected_from_checkers(question, form_value)

    if qtype == "single":
        if checked_options is not None:
            selected_options = checked_options[:1]
        else:
            selected = str(form_value.get(_field_name(qid, "choice")) or "").strip()
            selected_options = [selected] if selected in allowed else []
        values = [custom] if custom else selected_options[:1]
    else:
        if checked_options is not None:
            selected_options = checked_options
        else:
            raw_selected = form_value.get(_field_name(qid, "choices"))
            if isinstance(raw_selected, list):
                selected_options = [str(v).strip() for v in raw_selected if str(v).strip() in allowed]
            elif isinstance(raw_selected, str) and raw_selected.strip() in allowed:
                selected_options = [raw_selected.strip()]
            else:
                selected_options = []
        values = selected_options[:]
        if custom:
            values.append(custom)

    return {
        "question_id": qid,
        "type": qtype,
        "values": values,
        "selected_options": selected_options,
        "custom_value": custom,
    }


SINGLE_CHOICE_TOO_MANY_OPTIONS_MESSAGE = "当前问题为单选，请选择一个合适的答案"

DIAGNOSTIC_MINIMAL_ACTION = "diagnostic_minimal"
DIAGNOSTIC_RESPONSE_MODES = frozenset(("ack", "toast", "sync_card"))


def normalize_diagnostic_response_mode(response_mode: Any) -> str:
    mode = str(response_mode or "ack").strip().lower()
    if mode not in DIAGNOSTIC_RESPONSE_MODES:
        raise ValueError("response_mode must be one of: ack, toast, sync_card")
    return mode


def _option_checkers(question: dict[str, Any], selected: list[str]) -> list[dict[str, Any]]:
    selected_set = set(selected or [])
    elements = []
    for i, opt in enumerate(question["options"]):
        elements.append({
            "tag": "checker",
            "name": _checked_field_name(question["id"], i),
            "value": opt["label"],
            "checked": opt["label"] in selected_set,
            "text": {"tag": "plain_text", "content": opt["label"]},
        })
    return elements


def _callback_button(
    *,
    name: str,
    label: str,
    session_id: str,
    action: str,
    question_index: int,
    button_type: str | None = None,
    submit: bool = False,
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "tag": "button",
        "name": name,
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [{
            "type": "callback",
            "value": {
                "session_id": session_id,
                "action": action,
                "question_index": question_index,
            },
        }],
    }
    if button_type:
        button["type"] = button_type
    if submit:
        button["form_action_type"] = "submit"
    return button


def render_diagnostic_minimal_card(response_mode: str = "ack", nonce: str = "") -> dict[str, Any]:
    """Render the smallest callback-only card used to isolate card action transport."""
    response_mode = normalize_diagnostic_response_mode(response_mode)
    value = {
        "action": DIAGNOSTIC_MINIMAL_ACTION,
        "response_mode": response_mode,
        "nonce": str(nonce or ""),
    }
    return {
        "schema": "2.0",
        "config": {"summary": {"content": "卡片回调诊断"}, "update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "**卡片回调诊断**"},
                {
                    "tag": "button",
                    "name": "diagnostic_minimal_btn",
                    "text": {"tag": "plain_text", "content": "触发诊断回调"},
                    "type": "primary",
                    "behaviors": [{"type": "callback", "value": value}],
                },
            ],
        },
    }


def render_diagnostic_received_card(response_mode: str = "sync_card", nonce: str = "") -> dict[str, Any]:
    response_mode = normalize_diagnostic_response_mode(response_mode)
    suffix = f"\n\nnonce: `{nonce}`" if nonce else ""
    return {
        "schema": "2.0",
        "config": {"summary": {"content": "诊断回调已收到"}, "update_multi": True},
        "body": {
            "elements": [{
                "tag": "markdown",
                "content": f"**诊断回调已收到**\n\nresponse_mode: `{response_mode}`{suffix}",
            }],
        },
    }


def _button_row(session_id: str, current_index: int) -> dict[str, Any]:
    """Render the action row: 上一题 on the left, 提交本题 on the right (same row)."""
    columns: list[dict[str, Any]] = []
    if current_index > 0:
        columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "vertical_align": "center",
            "elements": [
                _callback_button(
                    name="previous_btn",
                    label="上一题",
                    session_id=session_id,
                    action="previous",
                    question_index=current_index,
                ),
            ],
        })
    columns.append({
        "tag": "column",
        "width": "auto",
        "vertical_align": "center",
        "elements": [
            _callback_button(
                name="submit_btn",
                label="提交本题",
                session_id=session_id,
                action="submit",
                question_index=current_index,
                button_type="primary",
                submit=True,
            ),
        ],
    })
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "small",
        "columns": columns,
    }


def render_question_card(
    schema: dict[str, Any],
    session_id: str,
    current_index: int,
    answers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render a single-question Feishu interactive card."""
    schema = validate_form_schema(schema)
    questions = schema["questions"]
    if current_index < 0 or current_index >= len(questions):
        raise FormSchemaError("current_index out of range")

    answers = answers or {}
    question = questions[current_index]
    qid = question["id"]
    saved = answers.get(qid) or {}
    selected = saved.get("selected_options") or []
    custom = saved.get("custom_value") or ""
    is_single = question["type"] == "single"

    elements = [
        {"tag": "markdown", "content": f"**{schema['title']}**"},
        {"tag": "markdown", "content": f"**问题 {current_index + 1} / {len(questions)}**"},
        {"tag": "hr"},
        {
            "tag": "form",
            "name": "interactive_question_form",
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"**{'单选' if is_single else '复选'}** - {question['title']}",
                },
                *_option_checkers(question, selected),
                {
                    "tag": "input",
                    "name": _field_name(qid, "custom"),
                    "label": {"tag": "plain_text", "content": question["custom_input_label"]},
                    "placeholder": {"tag": "plain_text", "content": "如以上选项都不合适，可在这里填写"},
                    "default_value": custom,
                    "input_type": "text",
                },
                _button_row(session_id, current_index),
            ],
        },
    ]

    return {
        "schema": "2.0",
        "config": {"summary": {"content": schema["title"]}, "update_multi": True},
        "body": {"elements": elements},
    }


def render_completed_card(title: str = "信息已提交") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"summary": {"content": title}, "update_multi": True},
        "body": {"elements": [{"tag": "markdown", "content": f"**{title}**\n\n已提交，正在继续处理..."}]},
    }


def build_followup_payload(
    original_text: str,
    schema: dict[str, Any],
    answers: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    schema = validate_form_schema(schema)
    ordered_answers = []
    for q in schema["questions"]:
        answer = deepcopy(answers.get(q["id"]) or {})
        ordered_answers.append({
            "id": q["id"],
            "title": q["title"],
            "type": q["type"],
            "values": answer.get("values", []),
            "selected_options": answer.get("selected_options", []),
            "custom_value": answer.get("custom_value", ""),
        })
    return {
        "original_text": original_text,
        "form_title": schema["title"],
        "answers": ordered_answers,
    }


def build_followup_prompt(
    original_text: str,
    schema: dict[str, Any],
    answers: dict[str, dict[str, Any]],
) -> str:
    payload = build_followup_payload(original_text, schema, answers)
    return (
        "用户已经通过飞书交互表单补充了信息。请基于原始请求和下面的结构化答案继续处理，"
        "必要时可以继续使用工具或再次向用户提问。\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )
