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
import mimetypes
import os
import re
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
    return j(handler, {"pages": list_dashboard_pages(root), "dir": str(root)}) or True


# ── Shell marker ──────────────────────────────────────────────────────────────

def mark_shell(html: str) -> str:
    """Stamp `data-client-mode="1"` on `<html ...>` when client mode is on."""
    if not is_client_mode():
        return html
    return html.replace("<html ", '<html data-client-mode="1" ', 1)
