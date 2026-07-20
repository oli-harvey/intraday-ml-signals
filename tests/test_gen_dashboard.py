"""gen_dashboard.py's render() has broken twice on f-string brace escaping
(07-16: a doubled brace leaked a raw '{:+.1f}%' placeholder into the page;
07-20: an under-escaped brace made svg_chart's dict argument parse as an
invalid set literal, raising TypeError at render time). Both bugs were only
visible by actually EXECUTING render(), not by syntax-checking the file —
these tests exist so the next one is caught the same way.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gen_dashboard as gd  # noqa: E402

PLACEHOLDER = re.compile(r"\{:[+.\-\w]*\}")


def test_render_executes_without_raising(tmp_path):
    # no status.json / history at all — the coldest possible start
    out = gd.render(tmp_path)
    assert "<html" in out and "</html>" in out


def test_render_leaves_no_raw_format_placeholders(tmp_path):
    out = gd.render(tmp_path)
    assert not PLACEHOLDER.findall(out)


def test_render_is_telegram_web_app_aware(tmp_path):
    out = gd.render(tmp_path)
    assert "telegram-web-app.js" in out
    assert "Telegram.WebApp" in out


def test_page_shell_is_well_formed_and_escapes_the_title():
    out = gd.page_shell("a <b>title</b>", "<p>body</p>")
    assert out.count("<html") == 1 and out.count("</html>") == 1
    assert "&lt;b&gt;title&lt;/b&gt;" in out  # title is escaped, not raw HTML
    assert "<p>body</p>" in out  # body is NOT escaped (it's already HTML)
