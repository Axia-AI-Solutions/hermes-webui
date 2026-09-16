"""Axia fork — the client shell (dashboard home · agent drawer · iframe→shell bridge).

Structural assertions over static/*.js|css|html, the repo's convention for
front-end behaviour (no node/jsdom dependency — see TESTING.md). Each assertion
pins a decision from docs/superpowers/specs/2026-09-14-marketing-agent-shell-ux-design.md
so a rebase or a refactor that silently drops one goes red here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# ── shell markup ─────────────────────────────────────────────────────────────

def test_dashboard_view_has_no_sidebar_panel_and_a_header_week_nav():
    html = _read("index.html")
    assert 'id="panelClientdash"' not in html, "the week list sidebar panel was removed on purpose"
    assert 'id="clientdashList"' not in html
    for needle in ('id="clientdashWeek"', 'id="clientdashPrev"', 'id="clientdashNext"', 'id="clientdashFrame"', 'id="clientdashOpen"'):
        assert needle in html, needle


def test_drawer_markup_is_client_only_and_sits_beside_main():
    html = _read("index.html")
    m = re.search(r'<aside id="agentDrawer" class="client-only" data-state="closed"', html)
    assert m, "drawer aside missing or not client-only / not closed by default"
    assert html.index("</main>") < m.start(), "drawer must be a sibling AFTER <main> (a flex item of .layout)"
    for needle in ('id="agentDrawerTab"', 'id="agentDrawerBadge"', 'id="agentDrawerName"', 'id="agentDrawerNew"',
                   'id="agentDrawerWide"', 'id="agentDrawerClose"', 'id="agentDrawerSessions"', 'id="agentDrawerBody"'):
        assert needle in html, needle
    # the drawer never carries a transcript or composer of its own
    body = html[html.index('id="agentDrawerBody"'):html.index("</aside>", html.index('id="agentDrawerBody"'))]
    assert "composer" not in body and 'id="messages' not in body


def test_avatar_is_the_brand_mark_the_box_mounts():
    html = _read("index.html")
    assert html.count('class="agent-avatar" src="static/favicon.svg"') >= 2


# ── css ──────────────────────────────────────────────────────────────────────

def test_css_hides_sidebar_on_dashboard_and_hides_developer_vocabulary():
    css = _read("client-mode.css")
    assert 'html[data-client-mode][data-client-view="dashboard"] .sidebar' in css
    for cls in (".session-source-tabs", ".project-bar", ".msg-question-jump-btn", ".suggestion-grid"):
        assert f"html[data-client-mode] {cls}" in css or f",\nhtml[data-client-mode] {cls}" in css, cls
    assert ".clientdash-item" not in css, "sidebar week-list styles were removed with the panel"


def test_css_drawer_states_and_chat_placement():
    css = _read("client-mode.css")
    assert '#agentDrawer[data-state="open"]{width:420px' in css
    assert '#agentDrawer[data-state="wide"]{width:min(60vw,900px)' in css
    assert 'html[data-client-mode]:not([data-client-view="dashboard"]) #agentDrawer{display:none !important;}' in css
    assert ".agent-drawer-body > #mainChat{display:flex" in css
    # the upstream child-combinator rule is what keeps #mainChat hidden on the dashboard
    # while it lives in <main>; moving it into the drawer escapes that rule by design
    assert "main.main.showing-clientdash > #mainChat{display:none !important;}" in css


# ── upstream hooks (one-liners, all commented "Axia client mode") ────────────

def test_switch_panel_hook():
    js = _read("panels.js")
    assert "if (_CLIENT_MODE && typeof _clientModeOnPanel === 'function') _clientModeOnPanel(nextPanel);" in js
    fn = js[js.index("async function switchPanel("):]
    fn = fn[:fn.index("\n}\n")]
    assert "_clientModeOnPanel(nextPanel)" in fn
    assert fn.index("mainEl.classList.toggle('showing-'") < fn.index("_clientModeOnPanel(nextPanel)")


def test_boot_overrides_come_after_the_settings_they_override():
    js = _read("boot.js")
    block = ("if(typeof _CLIENT_MODE!=='undefined'&&_CLIENT_MODE){window._showCliSessions=false;"
             "window._chatActivityDisplayMode='hide_all_activity';window._transparentStream=false;"
             "window._hideEmptyStateSuggestions=true;}")
    assert block in js
    assert js.index("window._showCliSessions=s.show_cli_sessions!==false;") < js.index(block)
    assert js.index("window._chatActivityDisplayMode=s.chat_activity_display_mode") < js.index(block)
    assert js.index("window._transparentStream=window._chatActivityDisplayMode==='transparent_stream';") < js.index(block)


def test_send_ships_the_pending_dashboard_context_once():
    js = _read("messages.js")
    start = js.index("api('/api/chat/start'")
    body = js[start:js.index("})});", start)]
    assert "dashboard_context:(S._pendingDashboardContext&&typeof S._pendingDashboardContext==='object')?S._pendingDashboardContext:undefined" in body
    after = js[start:start + 1500]
    assert after.count("S._pendingDashboardContext=null;") == 2, "cleared on success AND on failure"


# ── client-mode.js: bridge, drawer, weeks, home view ─────────────────────────

def test_bridge_validates_by_window_identity_and_opaque_origin():
    js = _read("client-mode.js")
    fn = js[js.index("function acceptDashboardMessage("):js.index("function runDashboardAction(")]
    assert "ev.source !== frame.contentWindow" in fn
    assert "ev.origin !== 'null' && ev.origin !== ownOrigin" in fn
    assert "d.type !== MSG_TYPE" in fn and "var MSG_TYPE = 'axia.dashboard.action';" in js
    assert "d.v !== 1" in fn
    assert "prompt.length > PROMPT_MAX" in fn and "var PROMPT_MAX = 4000;" in js
    assert "var CONTEXT_CAPS = {item_id: 200, title: 200, section: 100};" in js
    assert "var CONTEXT_KINDS = {finding: 1, action: 1, cta: 1, task: 1};" in js


def test_bridge_action_is_new_session_then_rename_then_send_with_context():
    js = _read("client-mode.js")
    fn = js[js.index("async function _runAction("):js.index("function _onWindowMessage(")]
    order = [
        "openDrawer()",
        "await newSession(false, {worktree: false});",
        "api('/api/session/rename'",
        "applySessionTitleUpdate(sid, title, {force: true})",
        "S._pendingDashboardContext = context;",
        "input.value = prompt;",
        "await send();",
    ]
    idx = [fn.index(step) for step in order]
    assert idx == sorted(idx), "the bridge sequence must be: open → new session → rename → context → text → send"
    assert ".slice(0, 64)" in fn, "titles cap at 64 chars"
    assert "_actionChain = _actionChain.then(" in js, "clicks are serialised, never deduplicated"


def test_reverse_channel_echoes_a_task_update_and_nothing_else():
    """shell -> dashboard. Reserved until 2026-09-15; its first and only use is
    telling the page that a "Mark done" click was recorded, so the button can
    show it. Anything else added here should come with its own test."""
    js = _read("client-mode.js")
    assert "function postToDashboard(msg)" in js
    assert "frame.contentWindow.postMessage(msg, '*')" in js
    callers = [l for l in js.splitlines() if "postToDashboard(" in l
               and "function postToDashboard" not in l and "window.postToDashboard" not in l]
    assert len(callers) == 1, callers
    assert "axia.shell.task-updated" in callers[0]
    assert "window.postToDashboard = postToDashboard;" in js


def test_one_chat_dom_moved_between_main_and_drawer():
    js = _read("client-mode.js")
    fn = js[js.index("function _placeChat("):js.index("var DRAWER_KEY")]
    assert "body.appendChild(chat)" in fn
    assert "main.insertBefore(chat, anchor)" in fn
    assert "_view() === 'dashboard' && _drawerState !== 'closed'" in fn
    assert "cloneNode" not in js, "the drawer must never copy the chat"


def test_drawer_state_and_seen_set_live_in_local_storage():
    js = _read("client-mode.js")
    assert "var DRAWER_KEY = 'axia-agent-drawer';" in js
    assert "var SEEN_KEY = 'axia-seen-announcements';" in js
    assert "var ANNOUNCEMENT_TAG = 'dashboard_announcement';" in js


def test_dashboard_is_the_home_view_in_client_mode():
    js = _read("client-mode.js")
    start = js[js.index("function _start("):]
    assert "if(!CLIENT) return;" in start
    assert "switchPanel('clientdash')" in start


def test_weeks_are_the_dated_pages_newest_first():
    js = _read("client-mode.js")
    assert r"var DATED_RE = /^(\d{4})-(\d{2})-(\d{2})\.html$/i;" in js
    fn = js[js.index("function deriveWeeks("):js.index("function _weekIndex(")]
    assert "return a.name < b.name ? 1 : (a.name > b.name ? -1 : 0);" in fn
    assert "{name: 'latest.html', label: 'This week'}" in fn


def test_empty_state_copy_names_the_agent_and_drops_i18n_binding():
    js = _read("client-mode.js")
    assert "h.textContent = 'Ask ' + name;" in js
    assert "h.removeAttribute('data-i18n')" in js and "p.removeAttribute('data-i18n')" in js
    assert "window._botName" in js


def test_client_mode_js_is_registered_after_panels_js():
    html = _read("index.html")
    assert html.index('src="static/panels.js') < html.index('src="static/client-mode.js') < html.index('src="static/boot.js')


# ── The Plan view (2026-09-15) ───────────────────────────────────────────────
#
# Structural, like everything else in this file: the markup, the panel wiring and
# the bridge literals. What the view DOES with the data is covered by
# tests/test_axia_client_plan.py against the real handlers.

def test_plan_view_markup_exists_once():
    html = _read("index.html")
    assert html.count('id="mainClientplan"') == 1
    assert html.count('data-panel="clientplan"') == 2      # the rail and the sidebar nav
    for needle in ('id="clientplanTabs"', 'id="clientplanObjectives"', 'id="clientplanTasks"',
                   'id="clientplanEmpty"', 'id="clientplanRevision"'):
        assert html.count(needle) == 1, needle
    assert 'No plan yet. It appears after the first Monday run.' in html


def test_plan_panel_is_registered_and_always_visible_in_client_mode():
    js = _read("panels.js")
    panels = js[js.index("const MAIN_VIEW_PANELS = ["):js.index("const MAIN_VIEW_SIDEBAR_PANEL_FALLBACKS")]
    assert "'clientplan'" in panels
    always = next(l for l in js.splitlines() if "_ALWAYS_VISIBLE_TABS = new Set(" in l)
    assert "'clientplan'" in always
    assert "if (nextPanel === 'clientplan' && typeof loadClientPlan === 'function') await loadClientPlan();" in js
    assert "if (panel === 'clientplan') mainText = 'Plan';" in js


def test_plan_view_shares_the_dashboard_chrome():
    js = _read("client-mode.js")
    fn = js[js.index("function _clientModeOnPanel("):js.index("function _afterMove(")]
    assert "name === 'clientdash' || name === 'clientplan'" in fn


def test_client_mode_js_carries_the_plan_functions_and_endpoint():
    js = _read("client-mode.js")
    assert "function loadClientPlan" in js
    assert "function postPlanEvent" in js
    assert "'/api/plan/events'" in js


def test_the_task_bridge_is_named_in_both_directions():
    js = _read("client-mode.js")
    assert "axia.dashboard.task" in js
    assert "axia.shell.task-updated" in js


def test_connect_modal_markup_and_steps():
    html = _read("index.html")
    assert html.count('id="connectModal"') == 1
    js = _read("client-mode.js")
    assert "axia.dashboard.connect" in js
    assert "'api/connections'" in js
    assert "Notify new users by email" in js          # the GA4 step that saves the client a surprise email
    assert "Users and permissions" in js              # the GSC step


def test_the_shell_never_writes_an_inferred_status():
    """The app writes `done` or `open`; an inference is the agent's, in its own file."""
    js = _read("client-mode.js")
    assert "inferred_done" not in js and "inferred_not_started" not in js
    assert "var TASK_STATUSES = {done: 1, open: 1};" in js


def test_every_main_view_is_hidden_by_default_and_has_a_showing_rule():
    """The defect this catches, measured 2026-09-15: `#mainClientplan` shipped with
    the div and NEITHER display rule, so it inherited `.main-view{display:flex}`,
    hung below whatever panel was active, and rendered empty because the loader only
    runs on a panel switch. It looked like an empty tab; it was a missing rule.

    `style.css` states the contract in a comment ("a #main<Name> sibling with
    class=main-view, inclusion in the hidden-by-default list, and a
    main.main.showing-<name> > #main<Name> { display:flex } rule"); this asserts it
    for every view in the tree instead of trusting the next person to read it.
    """
    html = _read("index.html")
    css = _read("style.css") + _read("client-mode.css")
    ids = set(re.findall(r'<div id="(main[A-Z][A-Za-z]*)"[^>]*class="[^"]*\bmain-view\b', html))
    assert "mainClientplan" in ids and "mainClientdash" in ids, ids
    missing = []
    for view in sorted(ids):
        if view == "mainChat":
            continue                      # chat is the default; it has the :not() chain instead
        panel = view[len("main"):].lower()
        hidden = (f"#{view}{{display:none;}}" in css.replace(" ", "")
                  or re.search(rf"main\.main\s*>\s*#{view}\s*\{{display:none", css)
                  or re.search(rf"#{view},", css) or re.search(rf"\n\s*#{view}\b[^{{]*\{{display:none", css))
        shown = re.search(rf"showing-{panel}\s*>\s*#{view}\s*\{{display:flex", css)
        if not hidden or not shown:
            missing.append(f"{view}: hidden={bool(hidden)} showing={bool(shown)}")
    assert not missing, "a main-view without both rules is visible under every panel: " + "; ".join(missing)
