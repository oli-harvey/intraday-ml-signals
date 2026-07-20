"""Shared Telegram plumbing: escaping, non-raising send, Web App button shape."""

from __future__ import annotations

import json
import urllib.parse

from signals import telegram as tg


def test_esc_neutralizes_html_breaking_characters():
    """The exact bug that killed stocks alerts for hours: a raw '<' in a config
    string made Telegram read '<2bp' as a broken tag and 400 the whole message."""
    assert tg.esc("spread<2bp") == "spread&lt;2bp"
    assert tg.esc("A & B") == "A &amp; B"


def test_dashboard_button_shape_is_a_valid_web_app_inline_keyboard():
    btn = tg.dashboard_button("https://example.com/app")
    kb = btn["inline_keyboard"][0][0]
    assert kb["web_app"]["url"] == "https://example.com/app"
    assert "text" in kb


class _FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"ok":true}'


def test_send_returns_true_on_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, data=None, timeout=None):
        captured["data"] = urllib.parse.parse_qs(data.decode())
        captured["url"] = req if isinstance(req, str) else req.full_url
        return _FakeResponse()

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    ok = tg.send({"TELEGRAM_CHAT_ID": "1", "TELEGRAM_BOT_TOKEN": "tok"}, "hello")
    assert ok is True
    assert captured["data"]["text"] == ["hello"]
    assert captured["data"]["parse_mode"] == ["HTML"]


def test_send_never_raises_on_failure(monkeypatch, capsys):
    def fake_urlopen(req, data=None, timeout=None):
        raise TimeoutError("network down")

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    ok = tg.send({"TELEGRAM_CHAT_ID": "1", "TELEGRAM_BOT_TOKEN": "tok"}, "hello")
    assert ok is False  # never raises — caller must be free to persist state
    err = capsys.readouterr().err
    assert "TELEGRAM SEND FAILED" in err and "hello" in err


def test_send_with_reply_markup_encodes_json(monkeypatch):
    captured = {}

    def fake_urlopen(req, data=None, timeout=None):
        captured["data"] = urllib.parse.parse_qs(data.decode())
        return _FakeResponse()

    monkeypatch.setattr(tg.urllib.request, "urlopen", fake_urlopen)
    btn = tg.dashboard_button("https://example.com/app")
    tg.send({"TELEGRAM_CHAT_ID": "1", "TELEGRAM_BOT_TOKEN": "tok"}, "hi",
             reply_markup=btn)
    markup = json.loads(captured["data"]["reply_markup"][0])
    assert markup == btn


def test_load_env_parses_key_value_pairs_and_skips_comments(tmp_path):
    p = tmp_path / "env"
    p.write_text('# comment\nTELEGRAM_CHAT_ID="123"\nTELEGRAM_BOT_TOKEN=abc\n')
    env = tg.load_env(str(p))
    assert env == {"TELEGRAM_CHAT_ID": "123", "TELEGRAM_BOT_TOKEN": "abc"}
