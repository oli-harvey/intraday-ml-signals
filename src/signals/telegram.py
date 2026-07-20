"""Shared Telegram plumbing for every bot script (crypto alerts, stocks alerts,
research digest). One place for send/escape/retry, so the failure modes that
have already bitten this project can't be reintroduced by a fourth script:

  - HTML-escape everything interpolated into a message. A raw '<' in a config
    string ('spread<2bp') once made Telegram's parser reject an entire message
    with 400 — and because the exception propagated up before state was saved,
    the same broken message retried and failed identically every 5 minutes for
    hours (2026-07-20). `esc()` exists so no script hand-rolls interpolation
    into an HTML message again.
  - A send failure must never raise past the caller — the caller almost always
    needs to persist state/history regardless of whether Telegram is reachable.
    `send()` returns a bool and logs to stderr; it does not raise.
  - Web App buttons (send_dashboard_button) are how a message gets a genuinely
    interactive chart in Telegram: there is no way to embed a live chart in the
    message body itself (Telegram has no such plugin surface — even
    TradingView's own bot only posts static images), but a button can open a
    real HTML/JS page in Telegram's own in-app webview with one tap. That page
    is served from a dedicated no-auth, unguessable-path Caddy route (see
    docs/DEPLOY.md) since embedding basic-auth credentials in a URL opened by
    an embedded webview is fragile and bad practice.
"""

from __future__ import annotations

import html
import json
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"


def load_env(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def esc(text: object) -> str:
    """HTML-escape any dynamic value before it goes into a parse_mode=HTML
    message. Numbers/format specs are safe as-is; free-text (config strings,
    order notes, symbols) is NOT — escape at the point of interpolation."""
    return html.escape(str(text))


def dashboard_button(url: str, label: str = "\N{BAR CHART} Live dashboard") -> dict:
    """An inline keyboard with one Web App button — tapping it opens `url`
    inside Telegram's own webview (interactive, native-feeling, never leaves
    the app), rather than the system browser. This is the only mechanism
    Telegram offers for a genuinely interactive chart; a chat message itself
    can never contain a live widget."""
    return {"inline_keyboard": [[{"text": label, "web_app": {"url": url}}]]}


def send(creds: dict[str, str], text: str, *, reply_markup: dict | None = None,
         prefix: str = "") -> bool:
    """Send one HTML-mode message. Returns True/False; NEVER raises — a caller
    that needs to persist state/history regardless of Telegram reachability
    must not have that blocked by a network hiccup or (despite esc()) an
    unforeseen malformed message. Logs the failure and the dropped message to
    stderr (captured by the cron log) instead."""
    payload = {
        "chat_id": creds["TELEGRAM_CHAT_ID"], "text": f"{prefix}{text}",
        "parse_mode": "HTML", "disable_web_page_preview": "true",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = urllib.parse.urlencode(payload).encode()
    url = API.format(token=creds["TELEGRAM_BOT_TOKEN"], method="sendMessage")
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        print("TELEGRAM SEND FAILED (message dropped, caller continues):",
              file=sys.stderr)
        print(text, file=sys.stderr)
        traceback.print_exc()
        return False
