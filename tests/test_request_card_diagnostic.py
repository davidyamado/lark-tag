import json
from unittest.mock import MagicMock

from src import request_card_diagnostic


def test_request_card_diagnostic_posts_minimal_card_request(monkeypatch, capsys):
    monkeypatch.setenv("INTERNAL_API_PORT", "4567")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "tok")
    monkeypatch.setenv("FEISHU_OPEN_ID", "ou_1")

    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"ok": true}'
    response.__exit__.return_value = None
    calls = []

    def fake_urlopen(req, timeout):
        calls.append((req, timeout))
        return response

    monkeypatch.setattr(request_card_diagnostic.urllib.request, "urlopen", fake_urlopen)

    assert request_card_diagnostic.main(["toast", "diag_1"]) == 0

    req, timeout = calls[0]
    assert timeout == 10
    assert req.full_url == "http://127.0.0.1:4567/interactive-form/diagnostic/minimal-card"
    assert req.headers["Authorization"] == "Bearer tok"
    assert json.loads(req.data) == {
        "open_id": "ou_1",
        "response_mode": "toast",
        "nonce": "diag_1",
    }
    assert json.loads(capsys.readouterr().out)["ok"] is True
