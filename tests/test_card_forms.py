import json

import pytest

from src.card_forms import (
    FormSchemaError,
    build_followup_prompt,
    normalize_answer,
    render_diagnostic_minimal_card,
    render_diagnostic_received_card,
    render_question_card,
    validate_form_schema,
)


def _schema():
    return {
        "title": "Need more info",
        "questions": [
            {
                "id": "priority",
                "title": "Priority?",
                "type": "single",
                "options": [
                    {"label": "P0", "description": "Blocking"},
                    {"label": "P1", "description": "This week"},
                ],
                "custom_input_label": "Other",
            },
            {
                "id": "modules",
                "title": "Modules?",
                "type": "multi",
                "options": [
                    {"label": "Frontend", "description": "Web/client"},
                    {"label": "Backend", "description": "Server"},
                ],
            },
        ],
    }


def _find_form(card):
    for element in card["body"]["elements"]:
        if element.get("tag") == "form":
            return element
    raise AssertionError("form element not found")


def _named_elements(element):
    found = {}
    if isinstance(element, dict):
        if element.get("name"):
            found[element["name"]] = element
        for value in element.values():
            found.update(_named_elements(value))
    elif isinstance(element, list):
        for item in element:
            found.update(_named_elements(item))
    return found


def _elements_by_tag(element, tag):
    found = []
    if isinstance(element, dict):
        if element.get("tag") == tag:
            found.append(element)
        for value in element.values():
            found.extend(_elements_by_tag(value, tag))
    elif isinstance(element, list):
        for item in element:
            found.extend(_elements_by_tag(item, tag))
    return found


def test_diagnostic_minimal_card_has_only_callback_button_controls():
    card = render_diagnostic_minimal_card(response_mode="toast", nonce="diag_1")
    raw = json.dumps(card, ensure_ascii=False)
    named = _named_elements(card)
    buttons = _elements_by_tag(card, "button")

    assert card["schema"] == "2.0"
    assert "form" not in raw
    assert "checker" not in raw
    assert "input" not in raw
    assert len(buttons) == 1
    assert named["diagnostic_minimal_btn"]["behaviors"][0]["value"] == {
        "action": "diagnostic_minimal",
        "response_mode": "toast",
        "nonce": "diag_1",
    }


def test_diagnostic_received_card_is_static_feedback_card():
    card = render_diagnostic_received_card(response_mode="sync_card", nonce="diag_2")
    raw = json.dumps(card, ensure_ascii=False)

    assert card["schema"] == "2.0"
    assert "form" not in raw
    assert "checker" not in raw
    assert "diagnostic_minimal" not in raw


def test_validate_form_schema_rejects_empty_questions():
    with pytest.raises(FormSchemaError):
        validate_form_schema({"title": "Need more info", "questions": []})


def test_single_choice_custom_input_overrides_selected_option():
    schema = validate_form_schema(_schema())
    answer = normalize_answer(
        schema["questions"][0],
        {
            "q_priority_choice": "P0",
            "q_priority_custom": "P1.5",
        },
    )

    assert answer == {
        "question_id": "priority",
        "type": "single",
        "values": ["P1.5"],
        "selected_options": ["P0"],
        "custom_value": "P1.5",
    }


def test_multi_choice_custom_input_appends_to_selected_options():
    schema = validate_form_schema(_schema())
    answer = normalize_answer(
        schema["questions"][1],
        {
            "q_modules_choices": ["Frontend", "Backend"],
            "q_modules_custom": "Migration",
        },
    )

    assert answer["values"] == ["Frontend", "Backend", "Migration"]
    assert answer["selected_options"] == ["Frontend", "Backend"]
    assert answer["custom_value"] == "Migration"


def test_single_choice_reads_checked_option_fields():
    schema = validate_form_schema(_schema())
    answer = normalize_answer(
        schema["questions"][0],
        {
            "q_priority_opt_0": True,
            "q_priority_opt_1": False,
            "q_priority_custom": "",
        },
    )

    assert answer["values"] == ["P0"]
    assert answer["selected_options"] == ["P0"]


def test_single_choice_checkboxes_do_not_callback_until_submit():
    schema = validate_form_schema(_schema())
    card = render_question_card(schema, session_id="form_1", current_index=0)
    controls = _named_elements(_find_form(card))

    assert "behaviors" not in controls["q_priority_opt_0"]
    assert "behaviors" not in controls["q_priority_opt_1"]


def test_multi_choice_reads_checked_option_fields():
    schema = validate_form_schema(_schema())
    answer = normalize_answer(
        schema["questions"][1],
        {
            "q_modules_opt_0": True,
            "q_modules_opt_1": True,
            "q_modules_custom": "",
        },
    )

    assert answer["values"] == ["Frontend", "Backend"]
    assert answer["selected_options"] == ["Frontend", "Backend"]


def test_multi_choice_checkboxes_do_not_callback_until_submit():
    schema = validate_form_schema(_schema())
    card = render_question_card(schema, session_id="form_1", current_index=1)
    controls = _named_elements(_find_form(card))

    assert "behaviors" not in controls["q_modules_opt_0"]
    assert "behaviors" not in controls["q_modules_opt_1"]


def test_first_question_card_has_submit_but_no_previous_button():
    schema = validate_form_schema(_schema())
    card = render_question_card(schema, session_id="form_1", current_index=0)
    controls = _named_elements(_find_form(card))

    assert "previous_btn" not in controls
    assert controls["submit_btn"]["form_action_type"] == "submit"


def test_question_card_renders_flat_checkers_inside_cardkit_form():
    schema = validate_form_schema(_schema())
    card = render_question_card(schema, session_id="form_1", current_index=0)

    assert card["schema"] == "2.0"
    assert card["config"]["update_multi"] is True
    form = _find_form(card)
    controls = _named_elements(form)
    raw = json.dumps(form, ensure_ascii=False)

    assert "select_static" not in raw
    assert "multi_select_static" not in raw
    assert len(_elements_by_tag(form, "checker")) == 2
    assert controls["q_priority_opt_0"]["tag"] == "checker"
    assert controls["q_priority_opt_0"]["checked"] is False
    assert "behaviors" not in controls["q_priority_opt_0"]
    assert controls["q_priority_opt_1"]["tag"] == "checker"
    assert controls["q_priority_opt_1"]["checked"] is False
    assert "behaviors" not in controls["q_priority_opt_1"]
    assert controls["q_priority_custom"]["tag"] == "input"
    assert controls["submit_btn"]["behaviors"][0]["value"] == {
        "session_id": "form_1",
        "action": "submit",
        "question_index": 0,
    }


def test_later_question_card_has_previous_and_submit_buttons():
    schema = validate_form_schema(_schema())
    card = render_question_card(schema, session_id="form_1", current_index=1)
    form = _find_form(card)
    form_controls = _named_elements(form)
    form_submit_buttons = [
        control
        for control in form_controls.values()
        if control.get("tag") == "button" and control.get("form_action_type") == "submit"
    ]

    # Both buttons live inside the form so submit can collect form_value.
    assert "previous_btn" in form_controls
    assert "submit_btn" in form_controls
    assert "form_action_type" not in form_controls["previous_btn"]
    assert form_controls["submit_btn"]["form_action_type"] == "submit"
    assert [button["name"] for button in form_submit_buttons] == ["submit_btn"]

    # Both buttons share one row: 上一题 on the left, 提交本题 on the right.
    rows = [el for el in _elements_by_tag(form, "column_set")]
    button_rows = [
        row for row in rows
        if any(
            el.get("tag") == "button"
            for col in row["columns"]
            for el in col["elements"]
        )
    ]
    assert len(button_rows) == 1
    row_button_names = [
        el["name"]
        for col in button_rows[0]["columns"]
        for el in col["elements"]
        if el.get("tag") == "button"
    ]
    assert row_button_names == ["previous_btn", "submit_btn"]


def test_answered_question_prefills_saved_values():
    schema = validate_form_schema(_schema())
    answers = {
        "priority": {
            "question_id": "priority",
            "type": "single",
            "values": ["P0"],
            "selected_options": ["P0"],
            "custom_value": "Custom priority",
        },
    }
    card = render_question_card(schema, session_id="form_1", current_index=0, answers=answers)
    controls = _named_elements(_find_form(card))

    assert controls["q_priority_opt_0"]["checked"] is True
    assert controls["q_priority_opt_1"]["checked"] is False
    assert controls["q_priority_custom"]["default_value"] == "Custom priority"


def test_multi_question_prefills_saved_values_inside_form():
    schema = validate_form_schema(_schema())
    answers = {
        "modules": {
            "question_id": "modules",
            "type": "multi",
            "values": ["Frontend", "Custom module"],
            "selected_options": ["Frontend"],
            "custom_value": "Custom module",
        },
    }

    card = render_question_card(schema, session_id="form_1", current_index=1, answers=answers)
    form = _find_form(card)
    controls = _named_elements(form)
    raw = json.dumps(form, ensure_ascii=False)

    assert "multi_select_static" not in raw
    assert controls["q_modules_opt_0"]["tag"] == "checker"
    assert controls["q_modules_opt_0"]["checked"] is True
    assert controls["q_modules_opt_1"]["checked"] is False
    assert controls["q_modules_custom"]["default_value"] == "Custom module"


def test_build_followup_prompt_contains_structured_answers():
    schema = validate_form_schema(_schema())
    prompt = build_followup_prompt(
        original_text="Create a task",
        schema=schema,
        answers={
            "priority": {
                "question_id": "priority",
                "type": "single",
                "values": ["P0"],
                "selected_options": ["P0"],
                "custom_value": "",
            },
        },
    )

    assert "Create a task" in prompt
    assert '"priority"' in prompt
    assert '"P0"' in prompt
