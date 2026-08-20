import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

from src.internal_api import start_internal_api


def _post(port, token, payload, path="/interactive-form/create"):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_interactive_form_create_calls_form_service_with_bound_context():
    store = MagicMock()
    auth = MagicMock()
    job_store = MagicMock()
    form_service = MagicMock()
    form_service.create_form.return_value = {"session_id": "form_1", "message_id": "om_card"}

    port, registry = start_internal_api(store, auth, job_store, form_service=form_service)
    token = registry.create(
        "g_oc_1_ou_1",
        metadata={
            "operator_open_id": "ou_1",
            "chat_id": "oc_1",
            "chat_type": "group",
            "reply_msg_id": "om_parent",
            "root_id": "om_root",
            "thread_session_key": "thread_key",
            "message_id": "om_user",
            "original_text": "创建需求",
        },
    )

    resp = _post(
        port,
        token,
        {
            "open_id": "g_oc_1_ou_1",
            "title": "补充信息",
            "questions": [
                {
                    "id": "priority",
                    "title": "优先级？",
                    "type": "single",
                    "options": [{"label": "P0"}],
                }
            ],
        },
    )

    assert resp["ok"] is True
    assert resp["session_id"] == "form_1"
    form_service.create_form.assert_called_once()
    kwargs = form_service.create_form.call_args.kwargs
    assert kwargs["context_id"] == "g_oc_1_ou_1"
    assert kwargs["operator_open_id"] == "ou_1"
    assert kwargs["reply_msg_id"] == "om_parent"
    assert kwargs["schema"]["title"] == "补充信息"


def test_interactive_form_create_requires_form_service():
    port, registry = start_internal_api(MagicMock(), MagicMock(), MagicMock())
    token = registry.create("ou_1")

    try:
        _post(
            port,
            token,
            {
                "open_id": "ou_1",
                "title": "补充信息",
                "questions": [
                    {
                        "id": "priority",
                        "title": "优先级？",
                        "type": "single",
                        "options": [{"label": "P0"}],
                    }
                ],
            },
        )
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        assert e.code == 503
        assert body["ok"] is False
        assert "not configured" in body["error"]
    else:
        raise AssertionError("expected HTTPError")


def test_interactive_form_diagnostic_minimal_card_uses_bound_context():
    store = MagicMock()
    auth = MagicMock()
    job_store = MagicMock()
    form_service = MagicMock()
    form_service.send_diagnostic_minimal_card.return_value = {
        "message_id": "om_diag",
        "response_mode": "toast",
        "nonce": "diag_1",
    }

    port, registry = start_internal_api(store, auth, job_store, form_service=form_service)
    token = registry.create(
        "g_oc_1_ou_1",
        metadata={
            "operator_open_id": "ou_1",
            "chat_id": "oc_1",
            "chat_type": "group",
            "reply_msg_id": "om_parent",
        },
    )

    resp = _post(
        port,
        token,
        {
            "open_id": "g_oc_1_ou_1",
            "response_mode": "toast",
            "nonce": "diag_1",
        },
        path="/interactive-form/diagnostic/minimal-card",
    )

    assert resp["ok"] is True
    assert resp["message_id"] == "om_diag"
    form_service.send_diagnostic_minimal_card.assert_called_once_with(
        operator_open_id="ou_1",
        chat_id="oc_1",
        chat_type="group",
        reply_msg_id="om_parent",
        response_mode="toast",
        nonce="diag_1",
    )


def test_interactive_form_diagnostic_minimal_card_rejects_invalid_response_mode():
    form_service = MagicMock()
    port, registry = start_internal_api(MagicMock(), MagicMock(), MagicMock(), form_service=form_service)
    token = registry.create("ou_1")

    try:
        _post(
            port,
            token,
            {"open_id": "ou_1", "response_mode": "large_card"},
            path="/interactive-form/diagnostic/minimal-card",
        )
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        assert e.code == 400
        assert body["ok"] is False
        assert "response_mode" in body["error"]
    else:
        raise AssertionError("expected HTTPError")
