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


def test_phase6_sections_and_endpoints_are_wired():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for anchor in ('id="register-form"', 'id="upload-form"', 'id="mine-rows"', 'id="user-form"',
                   'data-tab="users"'):  # fmt: skip
        assert anchor in html
    for path in ("/tenants/register", "/tenants/registration-info", "/documents/upload",
                 "/documents/mine", "/documents/upload-info", "/admin/users"):  # fmt: skip
        assert path in js
    # the client never sends identity or ids it shouldn't choose
    assert (
        "tenant_id"
        not in js.split(
            "// ---------------------------------------------------------------- register"
        )[1].split("enterApp")[0]
    )


def test_upload_info_requires_auth(client):
    assert client.get("/documents/upload-info").status_code == 401
    assert client.get("/documents/mine").status_code == 401
    assert client.post("/documents/upload").status_code == 401


def test_redesign_keeps_csp_safe_assets():
    """The redesign (React/Tailwind reference ported to plain files) adds no remote assets."""
    css = (WEB / "style.css").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css, "no web fonts or remote CSS"
    assert not re.search(r"\sstyle=", html), "inline style attributes would violate style-src"
    assert 'id="i-mark"' in html and "createElementNS" in js  # app-owned SVG icons
    assert "fonts.googleapis" not in html + css + js and "lucide" not in html + js


def test_redesign_navigation_and_mobile_drawer():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for tab in ("chat", "docs", "security", "users", "audit", "account"):
        assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
    for admin_tab in ("security", "users", "audit"):  # hidden until /auth/me says admin
        assert re.search(rf'class="nav-item tab admin-only hidden" data-tab="{admin_tab}"', html)
    for control in ('id="menu-open"', 'id="menu-close"', 'id="scrim"', 'aria-label="Close menu"'):
        assert control in html
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert (
        "PAGES[name].admin && !isAdmin()" in js
    )  # admin pages also refused in code, not only hidden


def test_workspace_endpoint_requires_auth(client):
    assert client.get("/auth/workspace").status_code == 401
