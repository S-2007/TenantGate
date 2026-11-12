"""The web UI is static, self-contained and served with a strict Content-Security-Policy."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "app" / "web"


def test_index_is_served_with_strict_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    for directive in ("default-src 'self'", "script-src 'self'", "frame-ancestors 'none'",
                      "object-src 'none'", "base-uri 'none'"):  # fmt: skip
        assert directive in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_static_assets_are_served(client):
    for name, ctype in (("app.js", "javascript"), ("style.css", "css")):
        r = client.get(f"/static/{name}")
        assert r.status_code == 200 and ctype in r.headers["content-type"]


def test_ui_has_no_inline_or_third_party_code():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script> would violate the CSP"
    assert not re.search(r"\son[a-z]+=", html), "inline event handlers would violate the CSP"
    assert not re.search(r"(src|href)=\"https?://", html), "no third-party assets"


def test_app_js_never_writes_raw_html():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, f"{sink} would let document text inject markup"
    assert "localStorage" not in js and "sessionStorage" not in js  # token stays in memory


def test_documents_endpoint_requires_auth(client):
    assert client.get("/documents").status_code == 401
