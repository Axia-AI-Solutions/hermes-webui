"""Axia fork — the Plan view and the channel connections.

Pure tests: no server, no hermes-agent, no network. Three things are proved
here, because each of them is silent when it breaks:

1. WHO clicked. The OIDC callback stores the email on the session and
   `session_username` reads it back, so a plan event carries a person.
2. WHAT the sidecar will accept into `events.jsonl`: a task id that matches the
   plan skill's own pattern, and `done`/`open` only — an `inferred_*` status is
   the agent's to write, in its own file.
3. That the SAME task-id pattern lives in `static/client-mode.js`, so the bridge
   and the API cannot drift into accepting different ids.
"""

from __future__ import annotations

import io
import json
import os
import re
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

from api import auth as A
from api import client_mode as cm

REPO = Path(__file__).resolve().parent.parent


class FakeHandler:
    """The smallest thing the handlers touch: headers, a body, a response sink."""

    def __init__(self, method="GET", body=b"", cookie=None, path="/"):
        self.command = method
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        if cookie:
            self.headers["Cookie"] = cookie
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.status = None
        self.sent_headers = {}
        self.client_address = ("127.0.0.1", 0)
        self.request = object()   # api.auth probes it for TLS; a plain object means "not TLS"

    def send_response(self, code, *a):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers.setdefault(k, []).append(v)

    def end_headers(self):
        pass

    def body_json(self):
        raw = self.wfile.getvalue()
        if b"\r\n\r\n" in raw:
            raw = raw.split(b"\r\n\r\n", 1)[1]
        return json.loads(raw.decode("utf-8"))


def _cookie_header(value: str) -> str:
    name = A._resolve_cookie_name()
    return f"{name}={value}"


# ── 1. who clicked ──────────────────────────────────────────────────────────
def test_session_username_roundtrip():
    cookie = A.create_session(auth_type="oidc", username="brandi@potomac.edu")
    h = FakeHandler(cookie=_cookie_header(cookie))
    assert A.session_username(h) == "brandi@potomac.edu"


def test_session_username_none_for_plain_session():
    cookie = A.create_session()
    h = FakeHandler(cookie=_cookie_header(cookie))
    assert A.session_username(h) is None
    assert A.session_username(FakeHandler()) is None
    assert A.session_username(FakeHandler(cookie=_cookie_header("garbage"))) is None


def test_oidc_callback_stores_email_as_username(monkeypatch):
    """Drive the real callback and read the cookie it set.

    The edit this protects is one argument in one line of `api/routes.py`; a
    test that only called `create_session` directly would stay green if that
    line were reverted.
    """
    from urllib.parse import urlparse

    import api.auth_oidc as oidc
    from api import routes

    monkeypatch.setattr(
        oidc, "complete_authorization_code_flow",
        lambda base, state, code: {"email": "brandi@potomac.edu", "next_path": "/"})
    monkeypatch.setattr(routes, "_request_base_url", lambda handler: "https://example.test", raising=False)

    h = FakeHandler(path="/api/auth/oidc/callback?state=s&code=c")
    handled = routes.handle_get(h, urlparse(h.path))
    assert handled is True and h.status == 302

    raw = next((v for v in h.sent_headers.get("Set-Cookie", []) if "=" in v), None)
    assert raw, f"no Set-Cookie in {h.sent_headers}"
    jar = SimpleCookie()
    jar.load(raw)
    value = jar[A._resolve_cookie_name()].value
    assert A.session_username(FakeHandler(cookie=_cookie_header(value))) == "brandi@potomac.edu"


# ── 2. the plan API ─────────────────────────────────────────────────────────
STATE = {
    "v": 1, "week": "2026-09-07", "revision": "2026-09-07",
    "objectives": [{"id": "obj.earned_press", "title": "One placement a quarter",
                    "series": "series.earned_press_90d", "current": 0,
                    "baseline": {"week": "2026-09-07", "value": 0},
                    "target": {"value": 1, "by": "2026-09-30"}, "history": [], "flag": None}],
    "tasks": [{"id": "task.rule.press_pitch", "title": "Pitch one story", "owner": "Brandi",
               "cadence": "once", "due_week": "2026-09-07", "resolved_status": "open",
               "inferred": None, "close_reason": None, "age_weeks": 0, "overdue": False,
               "cta": "Find three angles", "prompt": "Three angles."}],
    "this_week": ["task.rule.press_pitch"],
    "counts": {"this_week": 1, "overdue": 0, "inferred": 0, "done_this_week": 0},
    "skipped_events": 0,
}


@pytest.fixture
def plan(tmp_path):
    d = tmp_path / "plan"
    d.mkdir()
    (d / "plan.state.json").write_text(json.dumps(STATE), encoding="utf-8")
    (d / "events.jsonl").write_text("", encoding="utf-8")
    return d


def _post(plan, payload, by="brandi@potomac.edu"):
    body = json.dumps(payload).encode("utf-8")
    h = FakeHandler("POST", body=body)
    cm.handle_plan_event_post(h, plan, by=by)
    return h


def test_plan_event_post_appends_one_line_with_session_email(plan):
    h = _post(plan, {"task_id": "task.rule.press_pitch", "status": "done", "note": "did it"})
    assert h.body_json() == {"ok": True}
    lines = (plan / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    e = json.loads(lines[0])
    assert e["task"] == "task.rule.press_pitch" and e["status"] == "done"
    assert e["by"] == "brandi@potomac.edu" and e["via"] == "shell" and e["note"] == "did it"


def test_plan_event_post_rejects_traversal_task_id(plan):
    before = (plan / "events.jsonl").read_text(encoding="utf-8")
    for bad in ["task.../etc/passwd", "nottask.x", "", "task." + "x" * 200, "task.UPPER"]:
        h = _post(plan, {"task_id": bad, "status": "done"})
        assert h.status == 400, bad
    assert (plan / "events.jsonl").read_text(encoding="utf-8") == before


def test_plan_event_post_rejects_inferred_status(plan):
    for bad in ["inferred_done", "inferred_not_started", "closed_by_data", "DONE", None]:
        h = _post(plan, {"task_id": "task.rule.press_pitch", "status": bad})
        assert h.status == 400, bad
    assert (plan / "events.jsonl").read_text(encoding="utf-8") == ""


def test_plan_event_post_rejects_a_long_note(plan):
    h = _post(plan, {"task_id": "task.rule.press_pitch", "status": "done", "note": "x" * 501})
    assert h.status == 400
    assert (plan / "events.jsonl").read_text(encoding="utf-8") == ""


def test_plan_event_post_without_a_session_is_attributed_to_client(plan):
    _post(plan, {"task_id": "task.rule.press_pitch", "status": "open"}, by=None)
    e = json.loads((plan / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert e["by"] == "client"


def test_plan_family_is_post_only():
    """`GET /api/plan` served the Plan view and went with it. A live endpoint with
    no consumer is surface area on a client-facing box, so the family narrowed
    rather than kept a door nobody walks through."""
    assert cm.client_mode_allows("POST", "/api/plan/events") is True
    assert cm.client_mode_allows("GET", "/api/plan") is False
    assert cm.client_mode_allows("DELETE", "/api/plan/events") is False
    assert not hasattr(cm, "handle_plan_get")


# ── 3. one pattern, two sides ───────────────────────────────────────────────
def test_task_id_re_accepts_and_rejects():
    ok = ["task.rule.press_pitch", "task.q4.press-1", "task.a"]
    bad = ["task.../etc", "nottask.x", "", "task." + "x" * 121, "task.Upper", "task .x"]
    for s in ok:
        assert cm.TASK_ID_RE.match(s), s
    for s in bad:
        assert not cm.TASK_ID_RE.match(s), s


def test_task_statuses_are_done_open():
    assert cm.TASK_STATUSES == frozenset({"done", "open"})


def test_client_mode_js_carries_the_same_task_regex():
    js = (REPO / "static" / "client-mode.js").read_text(encoding="utf-8")
    assert cm.TASK_ID_RE.pattern in js


# ── 4. connections ──────────────────────────────────────────────────────────
STATUS = {
    "v": 1, "checked_at": "2026-09-15T12:00:00Z",
    "sa_email": "marketing-potomac-data@axia-marketing-agent.iam.gserviceaccount.com",
    "providers": {
        "ga4": {"state": "connected", "properties": [{"id": "properties/123", "name": "Potomac"}],
                "selected": "properties/123"},
        "gsc": {"state": "not_connected", "sites": [], "selected": None},
        "ads": {"state": "coming_later"}, "leads": {"state": "coming_later"},
        "social": {"state": "coming_later"},
    },
}


def test_connections_get_returns_status(tmp_path):
    d = tmp_path / "connections"
    d.mkdir()
    (d / "status.json").write_text(json.dumps(STATUS), encoding="utf-8")
    h = FakeHandler("GET")
    cm.handle_connections_get(h, d)
    assert h.body_json() == STATUS


def test_connections_get_404_without_file(tmp_path):
    h = FakeHandler("GET")
    cm.handle_connections_get(h, tmp_path / "nope")
    assert h.status == 404 and h.body_json() == {"error": "no connections yet"}


def test_connections_family_is_read_only():
    assert cm.client_mode_allows("GET", "/api/connections") is True
    assert cm.client_mode_allows("POST", "/api/connections") is False


# ── 5. the dirs resolve under HERMES_HOME ───────────────────────────────────
def test_plan_and_connections_dirs_follow_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cm.plan_dir() == tmp_path / "home" / "plan"
    assert cm.connections_dir() == tmp_path / "home" / "data" / "connections"


def test_task_is_a_context_kind():
    ctx = cm.normalize_dashboard_context({"kind": "task", "item_id": "task.rule.press_pitch"})
    assert ctx and ctx["kind"] == "task" and ctx["item_id"] == "task.rule.press_pitch"


# ── 6. the announcement's plan line ─────────────────────────────────────────
DASH = {
    "week_start": "2026-09-07",
    "overview": {"findings": [
        {"rank": 1, "headline": "A new 2-star review has no reply", "action_tag": "needs_you"},
        {"rank": 2, "headline": "A spam page is using the name", "action_tag": "watch"},
    ]},
}


def test_announcement_carries_plan_line():
    state = dict(STATE, counts={"this_week": 3, "overdue": 1, "inferred": 1, "done_this_week": 0})
    _, text = cm.compose_announcement(DASH, "Marketing Agent", state)
    assert "Your plan this week: 3 tasks, 1 overdue, 1 I think you did" in text
    assert text.index("Your plan this week") < text.index("Open the Dashboard tab")


def test_announcement_plan_line_is_singular_for_one_task():
    state = dict(STATE, counts={"this_week": 1, "overdue": 0, "inferred": 0, "done_this_week": 0})
    _, text = cm.compose_announcement(DASH, "Marketing Agent", state)
    assert "Your plan this week: 1 task, 0 overdue" in text


def test_announcement_without_plan_is_unchanged():
    base = cm.compose_announcement(DASH, "Marketing Agent")
    assert cm.compose_announcement(DASH, "Marketing Agent", None) == base
    assert cm.compose_announcement(DASH, "Marketing Agent", {"no": "counts"}) == base
    assert "Your plan this week" not in base[1]


def test_maybe_announce_reads_the_plan_state_fail_soft(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    dash = tmp_path / "home" / "dashboard"
    dash.mkdir(parents=True)
    (dash / "latest.html").write_text("<html></html>", encoding="utf-8")
    (dash / "dashboard.json").write_text(json.dumps(DASH), encoding="utf-8")
    seen = []
    # no plan yet: the announcement still goes out
    assert cm.maybe_announce(dash, lambda t, x: seen.append((t, x)) or "sid") is True
    assert "Your plan this week" not in seen[0][1]
    # with a plan, the line appears
    plan = tmp_path / "home" / "plan"
    plan.mkdir(parents=True)
    (plan / "plan.state.json").write_text(json.dumps(dict(
        STATE, counts={"this_week": 2, "overdue": 0, "inferred": 0, "done_this_week": 0})), encoding="utf-8")
    (dash / "latest.html").write_text("<html>new</html>", encoding="utf-8")
    assert cm.maybe_announce(dash, lambda t, x: seen.append((t, x)) or "sid") is True
    assert "Your plan this week: 2 tasks" in seen[1][1]


# ── 7. the live file, not a hand-made one ───────────────────────────────────


def test_the_announcement_reads_the_live_plan_state():
    """`tests/fixtures/plan_state_live.json` is the real file off marketing-potomac.
    The weekly announcement is the one thing in this process that still reads it, so
    the contract is pinned here: the counts it needs, present and numeric."""
    live = json.loads((REPO / "tests" / "fixtures" / "plan_state_live.json").read_text(encoding="utf-8"))
    counts = live["counts"]
    for k in ("this_week", "overdue", "inferred"):
        assert isinstance(counts[k], int), k
    _, text = cm.compose_announcement(DASH, "Marketing Agent", live)
    assert f"Your plan this week: {counts['this_week']} task" in text
