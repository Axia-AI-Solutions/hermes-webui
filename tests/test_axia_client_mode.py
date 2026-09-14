"""Axia fork — client mode (api/client_mode.py).

Pure tests: no server, no hermes-agent. Two families of proof:

1. INVENTORY: every `/api/<family>` literal in api/*.py + server.py is either
   allowed or denied ON PURPOSE. A family the table does not name fails here —
   an upstream route landing after a rebase cannot leak silently.
2. DECISIONS: the allow/deny/verb rules the product relies on, the workspace
   pin, the dashboard file resolver (traversal, dotfiles, missing file) and the
   shell marker.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from api import client_mode as cm

REPO = Path(__file__).resolve().parent.parent
_LITERAL = re.compile(r"""["']/api/([A-Za-z0-9_-]+)""")


def _inventory() -> set[str]:
    families: set[str] = set()
    for f in [*sorted((REPO / "api").glob("*.py")), REPO / "server.py"]:
        families.update(_LITERAL.findall(f.read_text(encoding="utf-8", errors="replace")))
    return families


class FakeHandler:
    def __init__(self, command="GET", body: bytes = b""):
        self.command = command
        self.status = None
        self.sent = []
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if body else {}
        self.close_connection = False

    def send_response(self, status):
        self.status = status

    def send_header(self, k, v):
        self.sent.append((k, v))

    def end_headers(self):
        pass

    def header(self, name):
        for k, v in self.sent:
            if k.lower() == name.lower():
                return v
        return None


# ── 1. inventory ─────────────────────────────────────────────────────────────

def test_every_api_family_in_the_tree_is_classified():
    found = _inventory()
    assert found, "inventory grep found nothing — regex or tree moved"
    unclassified = sorted(f for f in found if cm.classification(f) == "unclassified")
    assert not unclassified, (
        "api families with no client-mode decision (add to ALLOWED or DENIED in api/client_mode.py): "
        + ", ".join(unclassified)
    )


def test_inventory_test_can_go_red(monkeypatch):
    # Positive control: a family the table does not know must read as unclassified.
    assert cm.classification("family-that-does-not-exist") == "unclassified"


def test_no_family_is_both_allowed_and_denied():
    assert not (set(cm.ALLOWED) & cm.DENIED)


# ── 2. decisions ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/sessions"),
    ("POST", "/api/session/new"),
    ("DELETE", "/api/session/abc"),
    ("POST", "/api/chat"),
    ("POST", "/api/upload"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/settings"),
    ("GET", "/api/models"),
    ("GET", "/api/file?session_id=x&path=report.png"),
    ("GET", "/api/media?path=/x/y.png"),
    ("GET", "/api/client-dashboard"),
    ("POST", "/api/clarify/respond"),
    ("POST", "/api/approval/respond"),
    ("GET", "/"),
    ("GET", "/dashboard/"),
    ("GET", "/static/style.css"),
])
def test_allowed(method, path):
    assert cm.client_mode_allows(method, path) is True


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/skills"),
    ("GET", "/api/crons"),
    ("GET", "/api/kanban/boards"),
    ("GET", "/api/git/status"),
    ("GET", "/api/mcp"),
    ("GET", "/api/terminal/list"),
    ("GET", "/api/escape/list?session_id=x&token=y"),   # browse outside the workspace
    ("POST", "/api/escape/authorize"),
    ("GET", "/api/memory"),
    ("GET", "/api/logs"),
    ("GET", "/api/dashboard/status"),        # upstream's loopback probe, not our tab
    ("GET", "/api/share/sometoken"),         # a share link is a door around the login
    ("POST", "/api/share/create"),
    ("POST", "/api/settings"),               # writes on boot-read families
    ("POST", "/api/models"),
    ("POST", "/api/model"),
    ("POST", "/api/default-model"),
    ("POST", "/api/file/save"),
    ("POST", "/api/file/delete"),
    ("POST", "/api/workspaces/add"),
    ("POST", "/api/profile/switch"),
    ("POST", "/api/shutdown"),
    ("POST", "/api/admin/reload"),
    ("GET", "/api/never-heard-of-it"),       # unknown == denied
])
def test_denied(method, path):
    assert cm.client_mode_allows(method, path) is False


def test_family_is_the_first_segment_only():
    assert cm.api_family("/api/session/abc/export") == "session"
    assert cm.api_family("/api/sessions?limit=5") == "sessions"
    assert cm.api_family("/api/settings") == "settings"
    assert cm.api_family("/login") is None


def test_gate_is_a_no_op_when_client_mode_is_off(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_CLIENT_MODE", raising=False)
    h = FakeHandler("GET")
    assert cm.gate(h, urlparse("/api/skills")) is True
    assert h.status is None


def test_gate_refuses_with_403_json_when_on(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", "1")
    h = FakeHandler("GET")
    assert cm.gate(h, urlparse("/api/skills")) is False
    assert h.status == 403
    assert h.wfile.getvalue() == b'{"error":"client mode"}'
    # positive control on the same switch: an allowed family proceeds untouched
    h2 = FakeHandler("GET")
    assert cm.gate(h2, urlparse("/api/sessions")) is True
    assert h2.status is None


def test_refused_post_drains_its_body_so_keepalive_stays_sane(monkeypatch):
    # Measured before the fix: every refused POST logged a second `400` request —
    # the unread body parsed as the next request on the keep-alive connection.
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", "1")
    h = FakeHandler("POST", body=b'{"hidden_tabs":[]}')
    assert cm.gate(h, urlparse("/api/settings")) is False
    assert h.status == 403
    assert h.rfile.read() == b""            # body consumed
    assert h.close_connection is False      # small body: connection kept


def test_refused_post_with_huge_body_closes_instead_of_reading(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", "1")
    h = FakeHandler("POST")
    h.headers = {"Content-Length": str(50 * 1024 * 1024)}
    assert cm.gate(h, urlparse("/api/upload-not-allowed")) is False
    assert h.close_connection is True
    assert h.header("Connection") == "close"


@pytest.mark.parametrize("raw,expected", [("1", True), ("true", True), ("ON", True), ("0", False), ("", False)])
def test_switch_values(monkeypatch, raw, expected):
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", raw)
    assert cm.is_client_mode() is expected


# ── workspace pin ─────────────────────────────────────────────────────────────

def test_pin_workspace_keeps_home_and_subfolders_coerces_the_rest(tmp_path):
    home = tmp_path / "home"
    (home / "reports").mkdir(parents=True)
    (tmp_path / "secrets").mkdir()
    assert cm.pin_workspace(home, home) == home
    assert cm.pin_workspace(home / "reports", home) == home / "reports"
    assert cm.pin_workspace(tmp_path / "secrets", home) == home
    assert cm.pin_workspace(tmp_path, home) == home


def test_resolve_trusted_workspace_is_pinned_under_client_mode(monkeypatch, tmp_path):
    from api import workspace as ws

    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", "1")
    monkeypatch.setattr(ws, "_BOOT_DEFAULT_WORKSPACE", home)
    monkeypatch.setattr(ws, "_home_path", lambda: tmp_path)  # (A) would have trusted `elsewhere`
    assert ws.resolve_trusted_workspace(str(tmp_path / "elsewhere")) == home.resolve()
    assert ws.resolve_trusted_workspace(None) == home.resolve()
    # off: upstream behaviour — under home is trusted
    monkeypatch.delenv("HERMES_WEBUI_CLIENT_MODE")
    assert ws.resolve_trusted_workspace(str(tmp_path / "elsewhere")) == (tmp_path / "elsewhere").resolve()


# ── dashboard files ───────────────────────────────────────────────────────────

@pytest.fixture
def dash(tmp_path):
    root = tmp_path / "home" / "dashboard"
    (root / "assets").mkdir(parents=True)
    (root / "latest.html").write_text("<h1>this week</h1>", encoding="utf-8")
    (root / "2026-09-07.html").write_text("<h1>last week</h1>", encoding="utf-8")
    (root / "assets" / "chart.png").write_bytes(b"\x89PNG")
    (root / ".secret.html").write_text("no", encoding="utf-8")
    (tmp_path / "home" / "outside.html").write_text("no", encoding="utf-8")
    return root


def test_resolve_default_and_named(dash):
    assert cm.resolve_dashboard_file("", dash) == (dash / "latest.html").resolve()
    assert cm.resolve_dashboard_file("/", dash) == (dash / "latest.html").resolve()
    assert cm.resolve_dashboard_file("2026-09-07.html", dash) == (dash / "2026-09-07.html").resolve()
    assert cm.resolve_dashboard_file("assets/chart.png", dash) == (dash / "assets" / "chart.png").resolve()


@pytest.mark.parametrize("rel", [
    "../outside.html", "..%2Foutside.html", ".secret.html", "assets/../../outside.html",
    "/etc/passwd", "assets", "missing.html", "a b.html",
])
def test_resolve_refuses_traversal_dotfiles_dirs_and_missing(dash, rel):
    assert cm.resolve_dashboard_file(rel, dash) is None


def test_list_pages_latest_first_then_newest_html_only(dash):
    os.utime(dash / "2026-09-07.html", (1_000_000, 1_000_000))
    names = [p["name"] for p in cm.list_dashboard_pages(dash)]
    assert names == ["latest.html", "2026-09-07.html"]  # no .secret.html, no png


def test_dashboard_get_serves_html_sandboxed(dash):
    h = FakeHandler("GET")
    assert cm.handle_dashboard_get(h, urlparse("/dashboard/"), dash) is True
    assert h.status == 200
    assert h.header("Content-Security-Policy") == "sandbox allow-scripts allow-popups; frame-ancestors 'self'"
    assert h.header("X-Frame-Options") is None  # t() would have sent DENY and broken the iframe
    assert h.header("Content-Type").startswith("text/html")
    assert b"this week" in h.wfile.getvalue()


def test_dashboard_get_empty_state_is_a_page_not_a_404(tmp_path):
    root = tmp_path / "nothing-here"
    h = FakeHandler("GET")
    assert cm.handle_dashboard_get(h, urlparse("/dashboard/"), root) is True
    assert h.status == 200
    assert b"No dashboard yet" in h.wfile.getvalue()
    assert h.header("Content-Security-Policy") == "sandbox allow-scripts allow-popups; frame-ancestors 'self'"
    assert h.header("X-Frame-Options") is None  # t() would have sent DENY and broken the iframe


def test_dashboard_get_traversal_is_404(dash):
    h = FakeHandler("GET")
    assert cm.handle_dashboard_get(h, urlparse("/dashboard/../outside.html"), dash) is True
    assert h.status == 404


def test_dashboard_list_endpoint(dash):
    h = FakeHandler("GET")
    assert cm.handle_dashboard_list(h, dash) is True
    assert h.status == 200
    assert b'"latest.html"' in h.wfile.getvalue()


# ── shell marker ──────────────────────────────────────────────────────────────

def test_mark_shell(monkeypatch):
    html = '<!doctype html>\n<html lang="en">\n<head>'
    monkeypatch.delenv("HERMES_WEBUI_CLIENT_MODE", raising=False)
    assert cm.mark_shell(html) == html
    monkeypatch.setenv("HERMES_WEBUI_CLIENT_MODE", "1")
    assert '<html data-client-mode="1" lang="en">' in cm.mark_shell(html)


def test_index_html_still_has_the_html_tag_the_marker_targets():
    src = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    assert "<html lang=" in src


def test_server_calls_the_gate_on_both_dispatch_paths():
    src = (REPO / "server.py").read_text(encoding="utf-8")
    # once for GET, once for the shared write path — after check_auth, before the route
    get_block = src[src.index("def do_GET"):src.index("def _handle_write")]
    write_block = src[src.index("def _handle_write"):src.index("def do_POST")]
    for block in (get_block, write_block):
        assert "check_auth(self, parsed)" in block
        assert "client_mode_gate(self, parsed)" in block
        assert block.index("check_auth(self, parsed)") < block.index("client_mode_gate(self, parsed)")


# ── dashboard_context (the bridge's structured item context) ─────────────────

def test_dashboard_context_round_trip():
    ctx = {"item_id": "2026-09-07:what-moved:01", "title": "2-star Google review, Sep 4",
           "section": "what-moved", "kind": "finding", "week": "2026-09-07"}
    assert cm.normalize_dashboard_context(dict(ctx)) == ctx


def test_dashboard_context_caps_and_drops_unknown_keys():
    out = cm.normalize_dashboard_context({"title": "x" * 500, "section": "y" * 500, "item_id": "z" * 500,
                                          "kind": "cta", "week": "2026-09-07", "evil": "<script>", "n": 3})
    assert len(out["title"]) == 200 and len(out["section"]) == 100 and len(out["item_id"]) == 200
    assert "evil" not in out and "n" not in out


def test_dashboard_context_rejects_bad_kind_and_week_but_keeps_the_rest():
    out = cm.normalize_dashboard_context({"title": "t", "kind": "hack", "week": "Sep 7"})
    assert out == {"title": "t"}


@pytest.mark.parametrize("bad", [None, "text", 7, [], {}, {"title": "", "kind": "nope"}, {"title": 12}])
def test_dashboard_context_non_dict_or_empty_is_none(bad):
    assert cm.normalize_dashboard_context(bad) is None


def test_dashboard_context_prompt_line_names_the_item():
    line = cm.dashboard_context_prompt_line({"item_id": "2026-09-07:what-moved:01", "title": "2-star review",
                                             "section": "what-moved", "kind": "finding", "week": "2026-09-07"})
    for needle in ("2-star review", "finding", "what-moved", "2026-09-07", "2026-09-07:what-moved:01"):
        assert needle in line
    assert cm.dashboard_context_prompt_line(None) == ""
    assert cm.dashboard_context_prompt_line({}) == ""


# ── dashboard_context travels: persisted on the session, injected per turn ───

def test_dashboard_context_is_a_persisted_session_field():
    src = (REPO / "api" / "models.py").read_text(encoding="utf-8")
    meta_block = src[src.index("METADATA_FIELDS = ["):src.index("]", src.index("METADATA_FIELDS = ["))]
    assert "'dashboard_context'" in meta_block
    compact_block = src[src.index("def compact(self"):src.index("def compact(self") + 6000]
    assert "'dashboard_context'" in compact_block
    assert "self.dashboard_context = " in src


def test_chat_start_reads_the_context_through_the_normaliser_and_sets_it_once():
    src = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
    # Both the read and the assignment must live in the HANDLER, where `body` and `s`
    # are in scope — the first cut put the assignment in _start_chat_stream_for_session,
    # three call layers down, where the name did not exist (a NameError only at runtime).
    start = src.index("def _handle_chat_start(")
    end = src.index("\ndef ", start + 1)
    handler = src[start:end]
    assert 'normalize_dashboard_context(body.get("dashboard_context"))' in handler
    # first turn wins: a later turn of the same session must not overwrite it
    assert 'if dashboard_context and not getattr(s, "dashboard_context", None):' in handler
    assert "s.dashboard_context = dashboard_context" in handler
    assert handler.index('normalize_dashboard_context(body.get(') < handler.index("s.dashboard_context = dashboard_context")
    # and nowhere else in the file
    assert src.count("s.dashboard_context = dashboard_context") == 1


def test_both_turn_paths_hand_the_context_to_the_surface_prompt():
    for name in ("gateway_chat.py", "streaming.py"):
        src = (REPO / "api" / name).read_text(encoding="utf-8")
        assert "'dashboard_context'" in src or '"dashboard_context"' in src, name


def test_surface_prompt_renders_the_item_line_only_when_present():
    from api.streaming import _webui_surface_context_prompt
    base = {"source": "webui", "session_id": "abc", "profile": "default", "workspace": "/opt/data/home"}
    without = _webui_surface_context_prompt(dict(base))
    assert "Dashboard item" not in without
    with_ctx = _webui_surface_context_prompt(dict(base, dashboard_context={"title": "2-star review", "kind": "finding"}))
    assert with_ctx.startswith(without)
    assert '- Dashboard item: this conversation was started from the weekly marketing dashboard, item "2-star review", finding.' in with_ctx
