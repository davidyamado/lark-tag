import json
from unittest.mock import patch

from src import feishu_api


def test_send_interactive_card_uses_open_id_receive_type():
    card = {"config": {}, "elements": []}

    with patch("src.feishu_api._api", return_value={"data": {"message_id": "om_1"}}) as api:
        msg_id = feishu_api.send_interactive_card("ou_1", card, "token")

    assert msg_id == "om_1"
    req = api.call_args.args[0]
    assert "receive_id_type=open_id" in req.full_url
    body = json.loads(req.data)
    assert body["receive_id"] == "ou_1"
    assert body["msg_type"] == "interactive"
    assert json.loads(body["content"]) == card


def test_reply_interactive_card_in_thread_sets_reply_in_thread():
    card = {"config": {}, "elements": []}

    with patch("src.feishu_api._api", return_value={"data": {"message_id": "om_reply"}}) as api:
        msg_id = feishu_api.reply_interactive_card_in_thread("om_parent", card, "token")

    assert msg_id == "om_reply"
    req = api.call_args.args[0]
    assert req.full_url.endswith("/im/v1/messages/om_parent/reply")
    body = json.loads(req.data)
    assert body["reply_in_thread"] is True
    assert json.loads(body["content"]) == card


def test_update_interactive_card_patches_message_content():
    card = {"config": {}, "elements": [{"tag": "div"}]}

    with patch("src.feishu_api._api") as api:
        feishu_api.update_interactive_card("om_card", card, "token", sequence=3)

    req = api.call_args.args[0]
    assert req.get_method() == "PATCH"
    assert req.full_url.endswith("/im/v1/messages/om_card")
    body = json.loads(req.data)
    assert json.loads(body["content"]) == card


def test_update_interactive_card_by_token_posts_delay_update_payload():
    card = {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": []}}

    with patch("src.feishu_api._api") as api:
        feishu_api.update_interactive_card_by_token("c_update_token", card, "token", sequence=4)

    req = api.call_args.args[0]
    assert req.get_method() == "POST"
    assert req.full_url.endswith("/interactive/v1/card/update")
    body = json.loads(req.data)
    assert body == {"token": "c_update_token", "card": card}
