"""Client mode — the WebUI as a client-facing product surface (Axia fork).

`HERMES_WEBUI_CLIENT_MODE=1` turns the operator console into a chat + dashboard
surface for a customer's staff:

* SERVER (the boundary): every `/api/` request is checked against the
  classification below AFTER authentication and BEFORE any route code runs. A
  family that is not allowed answers `403 {"error": "client mode"}`. Unknown
  families are denied too — the allowlist is the only door.
* WORKSPACE: the session workspace is pinned to the boot default (the agent's
  home). A client cannot open a session on another directory and read it
  through the file routes (see `pin_workspace`).
* DASHBOARD: `/dashboard/` serves, read-only and sandboxed, the HTML the agent
  writes under `<HERMES_HOME>/home/dashboard/` (`latest.html` by default).
* BROWSER (cosmetic): the shell carries `data-client-mode` and the front-end
  hides every tab but Chat and Dashboard, the gear, the model picker and the
  toolset controls. Nothing the browser hides is reachable anyway.

The classification is a table of API FAMILIES — the first path segment after
`/api/` — so `session` and `sessions`, `model` and `models` are distinct rows.
`tests/test_axia_client_mode.py` enumerates every `/api/<family>` literal in the
tree and fails on a family this table does not name, which is how an upstream
route added after a rebase cannot leak into client mode silently.
"""

from __future__ import annotations

import html as _html
import json
import mimetypes
import os
import re
from datetime import date as _date, datetime, timezone
from pathlib import Path
from urllib.parse import unquote

ANY = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
READ = frozenset({"GET", "HEAD"})

# family -> verbs allowed. Absent family == denied on every verb.
ALLOWED: dict[str, frozenset[str]] = {
    # login / logout / OIDC / CSRF
    "auth": ANY,
    # the conversation itself
    "session": ANY,
    "sessions": ANY,
    "chat": ANY,
    "upload": ANY,
    "transcribe": ANY,
    "tts": ANY,
    "reasoning": ANY,
    # the agent asking the user something / the user answering
    "clarify": ANY,
    "approval": ANY,
    # chat-side bookkeeping the UI performs on its own behalf
    "background": ANY,
    "bg-task-complete-ack": ANY,
    "process-complete-ack": ANY,
    "client-events": ANY,
    "csp-report": ANY,
    # read-only views of the agent's own files and images (workspace-anchored)
    "file": READ,
    "media": READ,
    "workspace": READ,
    "workspaces": READ,
    "folder": READ,
    "list": READ,
    # the UI boots from these; the pickers that would write are hidden
    "settings": READ,
    "models": READ,
    "model": READ,
    "providers": READ,
    "provider": READ,
    "profile": READ,
    "profiles": READ,
    "gateway": READ,
    "health": READ,
    "updates": READ,
    "personalities": READ,
    "commands": READ,
    # the dashboard tab's own listing
    "client-dashboard": READ,
    # the plan: POST one event when a person marks a task done on the weekly page,
    # GET the closed set so a reloaded page can re-apply it (/api/plan/resolved only)
    "plan": frozenset({"GET", "POST"}),
    # the channel connection state the host-side puller writes
    "connections": READ,
}

# Every other family the tree carries today, DENIED ON PURPOSE. Listed so the
# inventory test can tell "denied by decision" from "never classified".
DENIED: frozenset[str] = frozenset({
    "admin", "btw", "codex", "crons", "dashboard", "default-model",
    "escape",  # browse OUTSIDE the workspace — the one thing the pin exists to prevent
    "extensions", "git", "git-info", "goal", "insights", "kanban",
    "logs", "mcp", "memory", "notes", "onboarding", "personality", "plugins",
    "project-os", "projects", "prompts", "rollback", "share", "shutdown",
    "skills", "system", "terminal", "v1", "wiki",
})


def is_client_mode() -> bool:
    return os.getenv("HERMES_WEBUI_CLIENT_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def api_family(path: str) -> str | None:
    """`/api/session/abc/export` -> `session`; non-API paths -> None."""
    if not path.startswith("/api/"):
        return None
    rest = path[len("/api/"):]
    return rest.split("/", 1)[0].split("?", 1)[0]


def client_mode_allows(method: str, path: str) -> bool:
    """Pure decision: may this (verb, path) run under client mode?

    Non-API paths are not this gate's business (they answer True); the page
    routes are already behind `check_auth`.
    """
    family = api_family(path)
    if family is None:
        return True
    verbs = ALLOWED.get(family)
    if verbs is None:
        return False
    return (method or "").upper() in verbs


def classification(family: str) -> str:
    """'allowed' | 'denied' | 'unclassified' — for the inventory test and the log."""
    if family in ALLOWED:
        return "allowed"
    if family in DENIED:
        return "denied"
    return "unclassified"


_DRAIN_CAP = 1 << 20  # refuse without reading a large body; close instead


def refuse(handler) -> None:
    """403 before the route runs — and leave the connection in a sane state.

    The route would have consumed the request body; we refuse before it can, so
    on a keep-alive connection the unread bytes would be parsed as the NEXT
    request (measured: every refused POST logged a second `400` line). Drain a
    small body; for anything larger, close the connection instead.
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError, AttributeError):
        length = 0
    if 0 < length <= _DRAIN_CAP:
        try:
            handler.rfile.read(length)
        except Exception:
            handler.close_connection = True
    elif length > _DRAIN_CAP:
        handler.close_connection = True
    body = b'{"error":"client mode"}'
    handler.send_response(403)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if getattr(handler, "close_connection", False):
        handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def gate(handler, parsed) -> bool:
    """Server call site: True = proceed to the route, False = 403 already sent."""
    if not is_client_mode():
        return True
    if client_mode_allows(getattr(handler, "command", "GET"), parsed.path):
        return True
    refuse(handler)
    return False


# ── Dashboard item context (the iframe→shell bridge) ──────────────────────────
#
# A dashboard button carries a prompt AND structured context about the item it
# belongs to. The prompt becomes the user's message verbatim; the context is
# persisted on the session (`Session.dashboard_context`) and rendered into the
# ephemeral system prompt of every gateway turn, so the agent knows WHICH
# dashboard item the conversation is about without the transcript carrying it.

_CONTEXT_CAPS = {"item_id": 200, "title": 200, "section": 100}
_CONTEXT_KINDS = frozenset({"finding", "action", "cta", "task"})
_WEEK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_dashboard_context(obj) -> dict | None:
    """Validate and cap the bridge's context; None when nothing usable is left.

    Unknown keys are dropped, strings are truncated to their cap, `kind` must
    be one of the three the template emits, `week` must be an ISO date. A bad
    context never fails the request — the user's message still goes through
    without it (the caller treats None as "no context").
    """
    if not isinstance(obj, dict):
        return None
    out: dict = {}
    for key, cap in _CONTEXT_CAPS.items():
        raw = obj.get(key)
        if isinstance(raw, str) and raw.strip():
            out[key] = raw.strip()[:cap]
    kind = obj.get("kind")
    if isinstance(kind, str) and kind in _CONTEXT_KINDS:
        out["kind"] = kind
    week = obj.get("week")
    if isinstance(week, str) and _WEEK_RE.match(week):
        out["week"] = week
    return out or None


def dashboard_context_prompt_line(ctx) -> str:
    """One line for the agent's ephemeral context; '' when there is no context."""
    if not isinstance(ctx, dict) or not ctx:
        return ""
    bits = []
    if ctx.get("title"):
        bits.append(f'item "{ctx["title"]}"')
    if ctx.get("kind"):
        bits.append(ctx["kind"])
    if ctx.get("section"):
        bits.append(f"section {ctx['section']}")
    if ctx.get("week"):
        bits.append(f"week of {ctx['week']}")
    if ctx.get("item_id"):
        bits.append(f"id {ctx['item_id']}")
    return ("- Dashboard item: this conversation was started from the weekly marketing dashboard, "
            + ", ".join(bits)
            + ". Answer about that item unless the user changes subject.")


# ── Workspace pin ─────────────────────────────────────────────────────────────

def pin_workspace(candidate: Path, boot_default: Path) -> Path:
    """Under client mode every session lives in the agent's home.

    A candidate equal to or under the boot default is kept (sub-folders of the
    home are still the home); anything else is COERCED to the default. This is
    coercion, not refusal, on purpose: there is no second workspace to choose in
    a client box, so the field has nothing to say.
    """
    try:
        candidate.relative_to(boot_default)
        return candidate
    except ValueError:
        return boot_default


# ── Dashboard files ───────────────────────────────────────────────────────────

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# sandbox: opaque origin (no cookies, no API); frame-ancestors: only our own shell may embed it.
_DASHBOARD_CSP = "sandbox allow-scripts allow-popups; frame-ancestors 'self'"
_DASHBOARD_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def dashboard_dir() -> Path:
    """`<HERMES_HOME>/home/dashboard` — the agent writes here, the WebUI reads."""
    home = os.getenv("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "home" / "dashboard"


def plan_dir() -> Path:
    """`<HERMES_HOME>/home/plan` — the plan skill writes here, the Plan view reads."""
    home = os.getenv("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "home" / "plan"


def connections_dir() -> Path:
    """`<HERMES_HOME>/home/data/connections` — the HOST-side puller writes here.

    The service-account key never comes near this process or the agent's
    container; what lands here is the state and the weekly numbers it pulled.
    """
    home = os.getenv("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "home" / "data" / "connections"


# A task id is a path segment that the plan skill minted, and the same literal
# is asserted against `static/client-mode.js` by a test: the two sides of the
# bridge cannot drift into accepting different ids.
TASK_ID_RE = re.compile(r"^task\.[a-z0-9_.-]{1,120}$")
TASK_STATUSES = frozenset({"done", "open"})


# A task is CLOSED when the data closed it or a person said so. `inferred_done`
# is deliberately absent: the agent thinking it saw the work is not the person
# saying so, and the entire plan hangs on that line staying drawn.
CLOSED_STATUSES = frozenset({"done", "closed_by_data"})


def handle_plan_resolved_get(handler, root: Path | None = None) -> bool:
    """`GET /api/plan/resolved` - which tasks are already closed.

    The published page cannot know this: it is written once a week and frozen, so
    a task closed on Wednesday still renders its `Mark done` button. Inside the
    app the page asks for this on load and re-applies what it gets; opened OUTSIDE
    the app it stays the honest Monday photograph, because there is no shell to ask.

    It answers the closed set and nothing else - not the titles, not the prompts,
    not the objectives. The page already has every word it renders; what it lacks
    is one bit per task.
    """
    from api.helpers import j

    root = root or plan_dir()
    try:
        state = json.loads((root / "plan.state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return j(handler, {"error": "no plan yet"}, status=404) or True
    resolved = {}
    for task in (state.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        tid, status = task.get("id"), task.get("resolved_status")
        if isinstance(tid, str) and TASK_ID_RE.match(tid) and status in CLOSED_STATUSES:
            resolved[tid] = status
    return j(handler, {"week": state.get("week"), "resolved": resolved}) or True


def handle_plan_event_post(handler, root: Path | None = None, by: str | None = None) -> bool:
    """`POST /api/plan/events` — one line, appended, attributed to the session.

    The sidecar is one of the two writers of `events.jsonl` (the other is the
    tick, for a data close). It writes `via: "shell"` and never anything else:
    an `inferred_*` status is the agent's to record, in its own file, and is
    refused here so a click can never be confused with a guess.
    """
    from api.helpers import j

    root = root or plan_dir()
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        payload = json.loads(handler.rfile.read(length) or b"{}")
    except (ValueError, OSError):
        return j(handler, {"error": "bad request body"}, status=400) or True
    if not isinstance(payload, dict):
        return j(handler, {"error": "bad request body"}, status=400) or True
    task_id = payload.get("task_id")
    status = payload.get("status")
    note = payload.get("note")
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        return j(handler, {"error": "bad task_id"}, status=400) or True
    if status not in TASK_STATUSES:
        return j(handler, {"error": "bad status"}, status=400) or True
    if note is not None and (not isinstance(note, str) or len(note) > 500):
        return j(handler, {"error": "bad note"}, status=400) or True
    event = {
        "ts": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "task": task_id,
        "status": status,
        "by": by or "client",
        "via": "shell",
    }
    if note:
        event["note"] = note
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(root / "events.jsonl"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        return j(handler, {"error": f"could not record: {exc.__class__.__name__}"}, status=500) or True
    return j(handler, {"ok": True}) or True


def handle_connections_get(handler, root: Path | None = None) -> bool:
    """`GET /api/connections` — what the puller last saw, or 404 before it ran."""
    from api.helpers import j

    root = root or connections_dir()
    try:
        data = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return j(handler, {"error": "no connections yet"}, status=404) or True
    return j(handler, data) or True


def resolve_dashboard_file(rel: str, root: Path | None = None) -> Path | None:
    """Map a `/dashboard/<rel>` tail to a file under the dashboard dir, or None.

    Each segment must match a conservative charset (no dot-leading names, so no
    `..` and no dotfiles); the resolved path must stay under the root; only a
    regular file is served. `''` means `latest.html`.
    """
    root = (root or dashboard_dir()).resolve()
    rel = unquote(rel or "").strip("/")
    if not rel:
        rel = "latest.html"
    parts = rel.split("/")
    if any(not _SAFE_SEGMENT.match(p) for p in parts):
        return None
    target = (root.joinpath(*parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def list_dashboard_pages(root: Path | None = None) -> list[dict]:
    """The `*.html` files of the dashboard dir, newest first, for the tab list."""
    root = root or dashboard_dir()
    out: list[dict] = []
    if not root.is_dir():
        return out
    for p in root.iterdir():
        if not p.is_file() or p.suffix.lower() != ".html" or not _SAFE_SEGMENT.match(p.name):
            continue
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda d: (d["name"] != "latest.html", -d["mtime"], d["name"]))
    return out


def _empty_dashboard_html(root: Path) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Dashboard</title>"
        "<style>body{font:15px/1.5 system-ui,sans-serif;color:#333;background:#fafafa;"
        "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
        "div{max-width:32em;text-align:center}code{font-size:13px}</style></head><body><div>"
        "<h1 style='font-size:20px;margin:0 0 .5em'>No dashboard yet</h1>"
        "<p>The agent has not written one. Ask it for this week's dashboard and it will "
        "appear here.</p>"
        f"<p><code>{_html.escape(str(root / 'latest.html'))}</code></p></div></body></html>"
    )


def _send(handler, body: bytes, status: int, content_type: str, extra: dict | None = None) -> bool:
    """Emit a response with the headers the dashboard needs and none it cannot carry.

    The shared `t()` helper adds `X-Frame-Options: DENY` and a CSP with
    `frame-ancestors 'none'`, which would refuse the very <iframe> the Dashboard
    tab embeds this page in. Browsers enforce EVERY CSP header present, so a
    second header cannot relax the first — the response has to be written here.
    """
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "same-origin")
    handler.send_header("X-Robots-Tag", "noindex, nofollow")
    for k, v in (extra or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)
    return True


def handle_dashboard_get(handler, parsed, root: Path | None = None) -> bool:
    """`GET /dashboard[/<file>]` — read-only, sandboxed, no listing."""
    root = root or dashboard_dir()
    rel = parsed.path[len("/dashboard"):]
    target = resolve_dashboard_file(rel, root)
    if target is None:
        if rel.strip("/") in ("", "latest.html"):
            return _send(handler, _empty_dashboard_html(root).encode("utf-8"), 200,
                         "text/html; charset=utf-8", {"Content-Security-Policy": _DASHBOARD_CSP})
        return _send(handler, b"not found", 404, "text/plain; charset=utf-8",
                     {"Content-Security-Policy": _DASHBOARD_CSP})
    ext = target.suffix.lower()
    mime = _DASHBOARD_MIME.get(ext) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    extra: dict = {}
    if ext in (".html", ".svg", ".js"):
        extra["Content-Security-Policy"] = _DASHBOARD_CSP
    if ext in (".xlsx", ".pdf"):
        extra["Content-Disposition"] = f'attachment; filename="{target.name}"'
    return _send(handler, target.read_bytes(), 200, mime, extra)


def handle_dashboard_list(handler, root: Path | None = None) -> bool:
    from api.helpers import j

    root = root or dashboard_dir()
    if is_client_mode():
        # A newly published dashboard is announced by the agent in a new
        # conversation. Fail-soft: the listing must never break because of it.
        try:
            maybe_announce(root, _create_announcement_session, bot_name=_bot_name())
        except Exception:  # pragma: no cover - defensive; the helper already isolates I/O
            pass
    return j(handler, {"pages": list_dashboard_pages(root), "dir": str(root)}) or True


# ── Weekly announcement ───────────────────────────────────────────────────────
#
# When the skill publishes a new `latest.html`, the agent should tell the client
# in a new conversation what needs their attention, and the drawer badge should
# show it until read. The hermes container has no authenticated path into this
# sidecar (auth is an OIDC cookie), and a cron-origin hermes session would be
# read-only here, so the SIDECAR creates the session, composing the message from
# the `dashboard.json` the skill publishes beside the page. The trigger is the
# dashboard listing (called on every Dashboard open); the marker is a dotfile,
# which the dashboard route never serves (`_SAFE_SEGMENT` refuses a leading dot).

_MARKER_NAME = ".announced.json"


def _bot_name() -> str:
    try:
        from api.config import load_settings

        return str(load_settings().get("bot_name") or "").strip() or "your agent"
    except Exception:
        return "your agent"


def _create_announcement_session(title: str, text: str) -> str:
    """Indirection so the routes-side creator is resolved lazily (and patchable)."""
    from api.routes import _create_announcement_session as _impl

    return _impl(title, text)


def latest_dashboard_state(root: Path) -> dict | None:
    p = root / "latest.html"
    if not p.is_file():
        return None
    st = p.stat()
    return {"mtime": int(st.st_mtime), "size": int(st.st_size)}


def read_announcement_marker(root: Path) -> dict | None:
    p = root / _MARKER_NAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_announcement_marker(root: Path, state: dict) -> None:
    p = root / _MARKER_NAME
    tmp = root / (_MARKER_NAME + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def _week_label(week_start) -> str | None:
    """'2026-09-07' -> 'Sep 7'; None when the value is not an ISO date."""
    if not isinstance(week_start, str) or not _WEEK_RE.match(week_start):
        return None
    try:
        y, m, d = (int(x) for x in week_start.split("-"))
        dt = _date(y, m, d)
    except ValueError:
        return None
    return f"{dt:%b} {dt.day}"


def compose_announcement(dashboard_json, bot_name: str, plan_state=None) -> tuple[str, str]:
    """(title, text) for the announcement session, from the skill's dashboard.json.

    Written in the agent's first person. With no readable JSON the message is
    generic rather than absent: the client still learns there is a new page.
    """
    data = dashboard_json if isinstance(dashboard_json, dict) else {}
    week = _week_label(data.get("week_start"))
    findings = []
    overview = data.get("overview")
    if isinstance(overview, dict) and isinstance(overview.get("findings"), list):
        for f in overview["findings"]:
            if isinstance(f, dict) and isinstance(f.get("headline"), str) and f["headline"].strip():
                findings.append(f)
    findings.sort(key=lambda f: (f.get("rank") if isinstance(f.get("rank"), int) else 999))
    title = f"Week of {week} dashboard" if week else "This week's dashboard"
    when = f"the week of {week}" if week else "this week"
    if not findings:
        text = (f"Your dashboard for {when} is ready. Open the Dashboard tab to read it, "
                "or ask me here about anything in it.")
        return title, text
    needs = [f for f in findings if f.get("action_tag") == "needs_you"]
    others = [f for f in findings if f.get("action_tag") != "needs_you"]
    n = len(findings)
    changed = "1 thing changed" if n == 1 else f"{n} things changed"
    lines = []
    if needs:
        if n == 1:
            lines.append(f"Your dashboard for {when} is ready. {changed}, and it needs you:")
        else:
            m = len(needs)
            lines.append(f"Your dashboard for {when} is ready. {changed}, {m} need{'s' if m == 1 else ''} you:")
        lines.extend(f"• {f['headline'].strip()}" for f in needs)
        if others:
            lines.append("Also worth a look: " + "; ".join(f["headline"].strip() for f in others[:3]) + ".")
    else:
        lines.append(f"Your dashboard for {when} is ready. {changed} and none needs you this week:")
        lines.extend(f"• {f['headline'].strip()}" for f in findings[:4])
    plan_line = _plan_line(plan_state)
    if plan_line:
        lines.append(plan_line)
    lines.append("Open the Dashboard tab to read it, or ask me here about any of these.")
    return title, "\n".join(lines)


def _plan_line(plan_state) -> str | None:
    """One sentence about the plan, or nothing at all.

    `inferred` is said out loud because an inference is NOT a completion: the
    person is the only one who can close a task the data cannot close, and the
    weekly message is where they are asked to.
    """
    if not isinstance(plan_state, dict):
        return None
    counts = plan_state.get("counts")
    if not isinstance(counts, dict):
        return None
    def _n(key):
        v = counts.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) else 0
    this_week, overdue, inferred = _n("this_week"), _n("overdue"), _n("inferred")
    word = "task" if this_week == 1 else "tasks"
    return (f"Your plan this week: {this_week} {word}, {overdue} overdue, "
            f"{inferred} I think you did - they carry a note on the dashboard, "
            f"confirm them there.")


def maybe_announce(root: Path, create_session, bot_name: str = "your agent") -> bool:
    """Announce a new latest.html exactly once; True when a session was created.

    The marker is written only AFTER the creator succeeded, so a failed create is
    retried on the next listing instead of being lost.
    """
    state = latest_dashboard_state(root)
    if state is None:
        return False
    if read_announcement_marker(root) == state:
        return False
    dashboard_json = None
    try:
        dashboard_json = json.loads((root / "dashboard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dashboard_json = None
    plan_state = None
    try:                                  # fail-soft: no plan yet is the normal first week
        plan_state = json.loads((plan_dir() / "plan.state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        plan_state = None
    title, text = compose_announcement(dashboard_json, bot_name, plan_state)
    try:
        create_session(title, text)
    except Exception:
        return False
    write_announcement_marker(root, state)
    return True


# ── Shell marker ──────────────────────────────────────────────────────────────

def mark_shell(html: str) -> str:
    """Stamp `data-client-mode="1"` on `<html ...>` when client mode is on."""
    if not is_client_mode():
        return html
    return html.replace("<html ", '<html data-client-mode="1" ', 1)
