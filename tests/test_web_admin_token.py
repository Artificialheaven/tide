# -*- coding: utf-8 -*-
"""Web 面板密码（web_admin_token）与 WS token 分开后的脱敏与校验。"""
import configs
from ui.web.server import SECRET_MASK, _app, _validate_config_update


def test_default_config_has_separate_web_admin_token():
    assert "web_admin_token" in configs.default_config
    assert "token" in configs.default_config


def test_get_config_masks_web_admin_token():
    original = configs.conf.get("web_admin_token")
    configs.conf["web_admin_token"] = "real-web-token"
    try:
        client = _app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        payload = client.get("/api/config").get_json()
        assert payload["web_admin_token"] == SECRET_MASK
    finally:
        configs.conf["web_admin_token"] = original


def test_masked_web_admin_token_is_not_written_back():
    clean, err = _validate_config_update(
        {"web_admin_token": SECRET_MASK, "token": "ws-token"})
    assert err == ""
    assert "web_admin_token" not in clean
    assert clean["token"] == "ws-token"


def test_web_admin_token_update_is_trimmed():
    clean, err = _validate_config_update(
        {"web_admin_token": "  new-panel-pwd  "})
    assert err == ""
    assert clean["web_admin_token"] == "new-panel-pwd"