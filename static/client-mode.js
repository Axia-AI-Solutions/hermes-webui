// Axia client mode — the shell around the agent for a client-facing box.
//
// Cosmetic + navigation layer; the boundary is the server gate
// (api/client_mode.py). Four things live here, all keyed on
// <html data-client-mode> (stamped by the server):
//
//   1. DASHBOARD VIEW — the default view. The agent writes HTML files under
//      <HERMES_HOME>/home/dashboard/; the server lists them at
//      api/client-dashboard and serves each one, sandboxed, at dashboard/<name>.
//      Week navigation lives in the view's header (prev · select · next).
//   2. AGENT DRAWER — on the Dashboard view, a right-edge drawer showing the
//      conversation. It NEVER renders a chat of its own: #mainChat (transcript +
//      composer, one DOM subtree) is MOVED into the drawer while it is open on
//      the dashboard and moved back into <main> otherwise. Same sessions, same
//      active conversation, same state as the Chat view, by construction.
//   3. DASHBOARD → AGENT BRIDGE — the dashboard page posts a message to this
//      window when one of its "Ask the agent" buttons is clicked. The iframe is
//      served with a CSP `sandbox`, so its origin is OPAQUE: `event.origin` is
//      the string "null" and the only honest check is window identity
//      (`event.source === frame.contentWindow`). An accepted message opens the
//      drawer, creates a new session, titles it from the item, and sends the
//      prompt as the user's first message with the structured context riding
//      out-of-band (`dashboard_context` on /api/chat/start).
//   4. PRESENCE — the agent's name (bot_name from /api/settings) and avatar
//      (static/favicon.svg, brand-mounted on the box) on the tab, the drawer
//      header and the empty state; the weekly announcement session is badged
//      until it has been opened.
//
// Hooks into upstream files are one-liners commented "Axia client mode":
// panels.js switchPanel → _clientModeOnPanel; boot.js settings → mode overrides;
// messages.js send → dashboard_context on chat/start.
(function(){
  'use strict';
  var CLIENT = !!(document.documentElement && document.documentElement.hasAttribute('data-client-mode'));
  var el = function(id){ return document.getElementById(id); };

  // ── 1. Dashboard view: weeks ────────────────────────────────────────────────
  var DATED_RE = /^(\d{4})-(\d{2})-(\d{2})\.html$/i;
  var _weeks = [];           // [{name, label}] newest first
  var _currentName = null;   // the page the iframe shows

  function _weekLabel(name){
    var m = DATED_RE.exec(name);
    if(!m) return name.replace(/\.html$/i, '');
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], 12));
    try{
      return 'Week of ' + d.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric', timeZone:'UTC'});
    }catch(e){
      return 'Week of ' + m[1] + '-' + m[2] + '-' + m[3];
    }
  }

  // The dated pages ARE the weeks (the skill writes the same bytes to latest.html
  // and <week>.html, so the newest dated file is exact for the current week).
  // Only a box that has never had a dated page falls back to latest.html.
  function deriveWeeks(pages){
    var dated = (pages || []).filter(function(p){ return p && DATED_RE.test(String(p.name || '')); })
      .sort(function(a, b){ return a.name < b.name ? 1 : (a.name > b.name ? -1 : 0); });
    if(dated.length) return dated.map(function(p){ return {name: p.name, label: _weekLabel(p.name)}; });
    var latest = (pages || []).some(function(p){ return p && p.name === 'latest.html'; });
    return latest ? [{name: 'latest.html', label: 'This week'}] : [];
  }

  function _weekIndex(name){
    for(var i = 0; i < _weeks.length; i++) if(_weeks[i].name === name) return i;
    return -1;
  }

  function _show(name){
    _currentName = name;
    var href = 'dashboard/' + encodeURIComponent(name);
    var frame = el('clientdashFrame');
    if(frame && frame.getAttribute('src') !== href) frame.setAttribute('src', href);
    var open = el('clientdashOpen');
    if(open) open.href = href;
    var sel = el('clientdashWeek');
    if(sel && sel.value !== name) sel.value = name;
    var i = _weekIndex(name);
    var prev = el('clientdashPrev'), next = el('clientdashNext');
    if(prev) prev.disabled = !(i >= 0 && i + 1 < _weeks.length);   // older
    if(next) next.disabled = !(i > 0);                               // newer
  }

  function _renderWeekSelect(){
    var sel = el('clientdashWeek');
    if(!sel) return;
    sel.textContent = '';
    if(!_weeks.length){
      var o = document.createElement('option');
      o.value = 'latest.html'; o.textContent = 'No dashboard yet';
      sel.appendChild(o);
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    _weeks.forEach(function(w, i){
      var o = document.createElement('option');
      o.value = w.name;
      o.textContent = i === 0 ? w.label + ' (current)' : w.label;
      sel.appendChild(o);
    });
  }

  async function loadClientDashboard(force){
    var pages = [];
    try{
      var r = await fetch('api/client-dashboard', {credentials: 'same-origin', cache: 'no-store'});
      var data = r.ok ? await r.json() : {pages: []};
      pages = Array.isArray(data.pages) ? data.pages : [];
    }catch(e){ pages = []; }
    _weeks = deriveWeeks(pages);
    _renderWeekSelect();
    if(!_currentName || _weekIndex(_currentName) < 0) _currentName = _weeks.length ? _weeks[0].name : 'latest.html';
    var frame = el('clientdashFrame');
    if(force || !frame || !frame.getAttribute('src')) _show(_currentName);
    else _show(_currentName);  // idempotent: only re-sets src when it differs
  }

  function _stepWeek(delta){
    var i = _weekIndex(_currentName);
    var j = i + delta;
    if(j < 0 || j >= _weeks.length) return;
    _show(_weeks[j].name);
  }

  // ── 2. View sync + the one chat DOM ─────────────────────────────────────────
  function _view(){ return document.documentElement.dataset.clientView || 'chat'; }

  function _clientModeOnPanel(name){
    // The Plan view sits beside the dashboard: same chrome, same drawer behaviour.
    document.documentElement.dataset.clientView = (name === 'clientdash' || name === 'clientplan') ? 'dashboard' : 'chat';
    _placeChat();
    refreshDrawer();
  }

  function _afterMove(){
    // The composer-fit and scroll-pin observers watch NODES, so they survive the
    // move; a resize nudges them to re-measure in the new container.
    try{ window.dispatchEvent(new Event('resize')); }catch(e){}
    if(typeof scrollToBottom === 'function'){ try{ scrollToBottom(); }catch(e){} }
  }

  // Where #mainChat lives right now. Idempotent.
  //   dashboard + drawer open|wide  → inside #agentDrawerBody
  //   otherwise                     → inside main.main (hidden there on the dashboard
  //                                   by the existing `showing-clientdash` rule)
  function _placeChat(){
    var chat = el('mainChat'), main = document.querySelector('main.main'), body = el('agentDrawerBody');
    if(!chat || !main || !body) return;
    var wantDrawer = _view() === 'dashboard' && _drawerState !== 'closed';
    if(wantDrawer){
      if(chat.parentElement !== body){ body.appendChild(chat); _afterMove(); }
    }else if(chat.parentElement !== main){
      var anchor = el('mainClientdash');
      if(anchor && anchor.parentElement === main) main.insertBefore(chat, anchor);
      else main.appendChild(chat);
      _afterMove();
    }
  }

  // ── Drawer state ────────────────────────────────────────────────────────────
  var DRAWER_KEY = 'axia-agent-drawer';
  var _drawerState = (function(){
    try{ var v = localStorage.getItem(DRAWER_KEY); return (v === 'open' || v === 'wide') ? v : 'closed'; }
    catch(e){ return 'closed'; }
  })();

  function setDrawerState(state){
    _drawerState = (state === 'open' || state === 'wide') ? state : 'closed';
    var d = el('agentDrawer');
    if(d) d.setAttribute('data-state', _drawerState);
    var tab = el('agentDrawerTab');
    if(tab) tab.setAttribute('aria-expanded', _drawerState === 'closed' ? 'false' : 'true');
    var wide = el('agentDrawerWide');
    if(wide){ wide.title = _drawerState === 'wide' ? 'Narrower' : 'Wider'; wide.setAttribute('aria-label', wide.title); }
    try{ localStorage.setItem(DRAWER_KEY, _drawerState); }catch(e){}
    _placeChat();
    refreshDrawer();
  }

  function openDrawer(){
    if(_drawerState === 'closed') setDrawerState('open');
  }

  // ── Switcher + badge ────────────────────────────────────────────────────────
  var SEEN_KEY = 'axia-seen-announcements';
  var ANNOUNCEMENT_TAG = 'dashboard_announcement';

  function _seen(){
    try{ var a = JSON.parse(localStorage.getItem(SEEN_KEY) || '[]'); return Array.isArray(a) ? a : []; }
    catch(e){ return []; }
  }
  function _markSeen(sid){
    var a = _seen();
    if(a.indexOf(sid) >= 0) return;
    a.push(sid);
    if(a.length > 200) a = a.slice(-200);
    try{ localStorage.setItem(SEEN_KEY, JSON.stringify(a)); }catch(e){}
  }
  function _isAnnouncement(s){
    return !!(s && String(s.source_tag || '').toLowerCase() === ANNOUNCEMENT_TAG);
  }
  function _activeSid(){
    return (typeof S !== 'undefined' && S && S.session && S.session.session_id) || null;
  }
  function _ts(s){
    try{ if(typeof _sessionTimestampMs === 'function'){ var v = Number(_sessionTimestampMs(s)); if(Number.isFinite(v)) return v; } }catch(e){}
    return (Number(s.last_message_at || s.updated_at || 0) || 0) * 1000;
  }
  function _title(s){
    try{ if(typeof _sessionDisplayTitle === 'function'){ var t = _sessionDisplayTitle(s); if(t) return String(t); } }catch(e){}
    return String(s.title || 'Untitled');
  }
  function _when(s){
    try{ if(typeof _formatRelativeSessionTime === 'function') return String(_formatRelativeSessionTime(_ts(s)) || ''); }catch(e){}
    return '';
  }
  // Unread = what the sidebar computes for the row (own + completion unread),
  // plus one rule the generic logic cannot express: a weekly announcement the
  // client has never opened. (First sight of a session is never unread upstream —
  // sessions.js registers the viewed count on first encounter — so the
  // announcement needs its own "seen" set.)
  function _rowUnread(s, seen){
    if(!s || !s.session_id || s.session_id === _activeSid()) return false;
    if(_isAnnouncement(s) && seen.indexOf(s.session_id) < 0) return true;
    try{ return typeof _hasUnreadForSession === 'function' && !!_hasUnreadForSession(s); }catch(e){ return false; }
  }

  function refreshDrawer(){
    if(!CLIENT) return;
    var all = (typeof _allSessions !== 'undefined' && Array.isArray(_allSessions)) ? _allSessions : [];
    var active = _activeSid();
    var seen = _seen();
    if(active){
      var cur = null;
      for(var k = 0; k < all.length; k++){ if(all[k] && all[k].session_id === active){ cur = all[k]; break; } }
      if(cur && _isAnnouncement(cur) && seen.indexOf(active) < 0){ _markSeen(active); seen = _seen(); }
    }
    var live = all.filter(function(s){ return s && s.session_id && !s.archived; });
    var unread = 0;
    live.forEach(function(s){ if(_rowUnread(s, seen)) unread++; });
    var badge = el('agentDrawerBadge');
    if(badge){ badge.textContent = String(unread); badge.hidden = unread === 0; }
    var tab = el('agentDrawerTab');
    if(tab) tab.classList.toggle('has-unread', unread > 0);

    var nav = el('agentDrawerSessions');
    if(!nav) return;
    var rows = live.slice().sort(function(a, b){ return _ts(b) - _ts(a); }).slice(0, 6);
    nav.textContent = '';
    rows.forEach(function(s){
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'agent-drawer-session' + (s.session_id === active ? ' active' : '') + (_rowUnread(s, seen) ? ' unread' : '');
      var t = document.createElement('span'); t.className = 't'; t.textContent = _title(s); b.appendChild(t);
      var m = document.createElement('span'); m.className = 'm'; m.textContent = _when(s); b.appendChild(m);
      b.title = _title(s);
      b.onclick = function(){
        if(typeof _openSidebarSession === 'function') _openSidebarSession(s);
        else if(typeof loadSession === 'function') loadSession(s.session_id);
      };
      nav.appendChild(b);
    });
  }

  var _refreshQueued = false;
  function _scheduleRefresh(){
    if(_refreshQueued) return;
    _refreshQueued = true;
    var run = function(){ _refreshQueued = false; refreshDrawer(); };
    if(typeof requestAnimationFrame === 'function') requestAnimationFrame(run); else setTimeout(run, 16);
  }

  // ── 3. Bridge: dashboard → agent ────────────────────────────────────────────
  var MSG_TYPE = 'axia.dashboard.action';
  var CONTEXT_CAPS = {item_id: 200, title: 200, section: 100};
  var CONTEXT_KINDS = {finding: 1, action: 1, cta: 1, task: 1};
  var TASK_MSG_TYPE = 'axia.dashboard.task';
  var CONNECT_MSG_TYPE = 'axia.dashboard.connect';
  // The SAME literal as api/client_mode.py TASK_ID_RE; a test asserts the two match,
  // so the bridge and the endpoint cannot drift into accepting different ids.
  var TASK_ID_RE = /^task\.[a-z0-9_.-]{1,120}$/;
  var TASK_STATUSES = {done: 1, open: 1};
  var CONNECT_PROVIDERS = {ga4: 'Google Analytics', gsc: 'Search Console'};
  var WEEK_RE = /^\d{4}-\d{2}-\d{2}$/;
  var PROMPT_MAX = 4000;

  // Pure decision. `frame` is the dashboard <iframe>; `ownOrigin` is location.origin.
  // The sandboxed frame reports origin "null"; a same-origin frame (were the CSP
  // ever relaxed) reports ours. Everything else, or any other window, is ignored.
  function acceptDashboardMessage(ev, frame, ownOrigin){
    if(!ev || !frame || !frame.contentWindow) return {ok: false, reason: 'no-frame'};
    if(ev.source !== frame.contentWindow) return {ok: false, reason: 'source'};
    if(ev.origin !== 'null' && ev.origin !== ownOrigin) return {ok: false, reason: 'origin'};
    var d = ev.data;
    if(!d || typeof d !== 'object') return {ok: false, reason: 'shape'};
    if(d.v !== 1) return {ok: false, reason: 'version'};
    if(d.type === TASK_MSG_TYPE){
      var tid = typeof d.task_id === 'string' ? d.task_id : '';
      if(!TASK_ID_RE.test(tid)) return {ok: false, reason: 'task_id'};
      if(!TASK_STATUSES[d.status]) return {ok: false, reason: 'status'};
      return {ok: true, kind: 'task', task_id: tid, status: d.status};
    }
    if(d.type === CONNECT_MSG_TYPE){
      if(!CONNECT_PROVIDERS[d.provider]) return {ok: false, reason: 'provider'};
      return {ok: true, kind: 'connect', provider: d.provider};
    }
    if(d.type !== MSG_TYPE) return {ok: false, reason: 'type'};
    var prompt = typeof d.prompt === 'string' ? d.prompt.trim() : '';
    if(!prompt || prompt.length > PROMPT_MAX) return {ok: false, reason: 'prompt'};
    var raw = (d.context && typeof d.context === 'object') ? d.context : {};
    var ctx = {};
    Object.keys(CONTEXT_CAPS).forEach(function(k){
      if(typeof raw[k] === 'string' && raw[k].trim()) ctx[k] = raw[k].trim().slice(0, CONTEXT_CAPS[k]);
    });
    if(typeof raw.kind === 'string' && CONTEXT_KINDS[raw.kind]) ctx.kind = raw.kind;
    if(typeof raw.week === 'string' && WEEK_RE.test(raw.week)) ctx.week = raw.week;
    return {ok: true, prompt: prompt, context: ctx};
  }

  // One click = one session. Clicks are serialised (newSession is not re-entrant);
  // two clicks make two sessions, on purpose — no dedup layer.
  var _actionChain = Promise.resolve();
  function runDashboardAction(prompt, context){
    _actionChain = _actionChain.then(function(){ return _runAction(prompt, context); }).catch(function(e){
      try{ console.warn('[axia-bridge] action failed', e); }catch(_){}
      if(typeof showToast === 'function') showToast('Could not start the conversation. Try again.', 3000);
    });
    return _actionChain;
  }

  async function _runAction(prompt, context){
    if(_view() === 'dashboard') openDrawer();
    await newSession(false, {worktree: false});
    var sid = _activeSid();
    if(!sid) throw new Error('no session after newSession');
    var title = String(context.title || prompt).replace(/\s+/g, ' ').trim().slice(0, 64);
    try{
      await api('/api/session/rename', {method: 'POST', body: JSON.stringify({session_id: sid, title: title})});
      if(typeof S !== 'undefined' && S.session && S.session.session_id === sid) S.session.title = title;
      if(typeof applySessionTitleUpdate === 'function') applySessionTitleUpdate(sid, title, {force: true});
    }catch(e){
      // The provisional title (first 64 chars of the prompt) still applies server-side.
      try{ console.warn('[axia-bridge] rename failed', e); }catch(_){}
    }
    if(typeof S !== 'undefined') S._pendingDashboardContext = context;   // consumed once by send()
    var input = el('msg');
    if(!input) throw new Error('no composer');
    input.value = prompt;
    if(typeof updateSendBtn === 'function'){ try{ updateSendBtn(); }catch(e){} }
    await send();
    try{ input.focus(); }catch(e){}
  }

  function _onWindowMessage(ev){
    var frame = el('clientdashFrame');
    var r = acceptDashboardMessage(ev, frame, location.origin);
    if(!r.ok){
      // Only mention messages that look like ours; the window receives plenty
      // of unrelated postMessage traffic (extensions, devtools).
      if(ev && ev.data && typeof ev.data === 'object' && typeof ev.data.type === 'string' && ev.data.type.indexOf('axia.') === 0){
        try{ console.debug('[axia-bridge] ignored:', r.reason); }catch(_){}
      }
      return;
    }
    if(r.kind === 'task'){
      postPlanEvent(r.task_id, r.status).then(function(){
        if(el('mainClientplan')) loadClientPlan();
        postToDashboard({type: 'axia.shell.task-updated', v: 1, task_id: r.task_id, status: r.status});
      });
      return;
    }
    if(r.kind === 'connect'){ openConnectModal(r.provider); return; }
    runDashboardAction(r.prompt, r.context);
  }

  // Reserved: shell → dashboard. The frame's origin is opaque, so '*' is the
  // only valid target; nothing calls this yet (e.g. a future "highlight the
  // section the agent is talking about"). Messages use type 'axia.shell.<verb>'.
  function postToDashboard(msg){
    var frame = el('clientdashFrame');
    if(frame && frame.contentWindow){ try{ frame.contentWindow.postMessage(msg, '*'); }catch(e){} }
  }

  // ── 4. Presence + copy ──────────────────────────────────────────────────────
  function _agentName(){
    var n = (typeof window._botName === 'string') ? window._botName.trim() : '';
    return n || 'your agent';
  }

  function applyClientModeCopy(){
    var name = _agentName();
    ['agentDrawerTabName', 'agentDrawerName'].forEach(function(id){ var n = el(id); if(n) n.textContent = name; });
    var h = document.querySelector('#emptyState h2');
    if(h){ h.removeAttribute('data-i18n'); h.textContent = 'Ask ' + name; }
    var p = document.querySelector('#emptyState p');
    if(p){ p.removeAttribute('data-i18n'); p.textContent = "Anything about this week's dashboard, your reviews, your competitors, or what to do next."; }
  }

  // bot_name arrives with /api/settings, asynchronously, from boot.js.
  function _watchBotName(){
    var tries = 0;
    var last = null;
    var iv = setInterval(function(){
      tries++;
      var n = (typeof window._botName === 'string') ? window._botName : null;
      if(n && n !== last){ last = n; applyClientModeCopy(); }
      if(tries > 20) clearInterval(iv);   // 5 s is plenty; the fallback stays otherwise
    }, 250);
  }

  // ── Start ───────────────────────────────────────────────────────────────────
  function _wire(){
    var on = function(id, fn){ var n = el(id); if(n) n.addEventListener('click', fn); };
    on('agentDrawerTab', function(){ openDrawer(); });
    on('agentDrawerClose', function(){ setDrawerState('closed'); });
    on('agentDrawerWide', function(){ setDrawerState(_drawerState === 'wide' ? 'open' : 'wide'); });
    on('agentDrawerNew', function(){ if(typeof newSession === 'function') newSession(true, {worktree: false}); });
    on('clientdashPrev', function(){ _stepWeek(+1); });   // older
    on('clientdashNext', function(){ _stepWeek(-1); });   // newer
    var sel = el('clientdashWeek');
    if(sel) sel.addEventListener('change', function(){ if(sel.value) _show(sel.value); });
    var list = el('sessionList');
    if(list && typeof MutationObserver === 'function'){
      new MutationObserver(_scheduleRefresh).observe(list, {childList: true, subtree: true});
    }
    window.addEventListener('message', _onWindowMessage);
    _wirePlanTabs();
    _wireConnectModal();
  }

  function _start(){
    if(!CLIENT) return;
    var d = el('agentDrawer');
    if(d) d.setAttribute('data-state', _drawerState);
    applyClientModeCopy();
    _watchBotName();
    _wire();
    // Dashboard is the home view.
    if(typeof switchPanel === 'function') switchPanel('clientdash');
    else document.documentElement.dataset.clientView = 'dashboard';
  }

  // -- 5. The plan ------------------------------------------------------------
  //
  // `plan.state.json` is DERIVED by the plan skill's tick; this view renders it and
  // writes exactly one thing back: a person ticking a box, through POST /api/plan/events.
  // An inference the agent made shows as "inferred, confirm" and leaves the box unticked:
  // the shell never turns a guess into a completion.

  var _planTab = 'this_week';
  var _plan = null;

  async function postPlanEvent(taskId, status, note){
    var body = {task_id: taskId, status: status};
    if(note) body.note = note;
    try{
      await api('/api/plan/events', {method: 'POST', body: JSON.stringify(body)});
      return true;
    }catch(e){
      try{ console.warn('[axia-plan] event failed', e); }catch(_){}
      if(typeof showToast === 'function') showToast('Could not record that. Try again.', 3000);
      return false;
    }
  }

  async function loadClientPlan(){
    var empty = el('clientplanEmpty');
    var objectives = el('clientplanObjectives');
    var tasks = el('clientplanTasks');
    if(!objectives || !tasks) return;
    try{
      var r = await fetch('api/plan', {credentials: 'same-origin', cache: 'no-store'});
      if(!r.ok) throw new Error('no plan');
      _plan = await r.json();
    }catch(e){
      _plan = null;
      if(empty) empty.hidden = false;
      objectives.textContent = '';
      tasks.textContent = '';
      return;
    }
    if(empty) empty.hidden = true;
    var rev = el('clientplanRevision');
    if(rev) rev.textContent = _plan.revision ? ('Revised ' + _plan.revision) : '';
    _renderPlanObjectives(objectives);
    _renderPlanTasks(tasks);
  }

  function _chip(text, cls){
    var s = document.createElement('span');
    s.className = 'plan-chip' + (cls ? ' ' + cls : '');
    s.textContent = text;
    return s;
  }

  function _renderPlanObjectives(host){
    host.textContent = '';
    (_plan.objectives || []).forEach(function(o){
      var row = document.createElement('div');
      row.className = 'plan-objective';
      var h = document.createElement('h4');
      h.textContent = o.title || o.id;
      row.appendChild(h);
      var line = document.createElement('p');
      line.className = 'plan-series';
      var base = (o.baseline && o.baseline.value !== undefined && o.baseline.value !== null) ? o.baseline.value : '?';
      var cur = (o.current === null || o.current === undefined) ? 'not measured' : o.current;
      var tgt = (o.target && o.target.value !== undefined) ? o.target.value : '?';
      line.textContent = String(o.series || '') + ': ' + base + ' -> ' + cur + ' -> ' + tgt;
      row.appendChild(line);
      if(o.flag === 'revisit') row.appendChild(_chip('revisit', 'plan-chip-warn'));
      host.appendChild(row);
    });
  }

  function _taskInTab(t){
    if(_planTab === 'done') return t.resolved_status === 'done' || t.resolved_status === 'closed_by_data';
    if(t.resolved_status === 'done' || t.resolved_status === 'closed_by_data') return false;
    if(t.status === 'retired') return false;
    if(_planTab === 'all_open') return true;
    return (_plan.this_week || []).indexOf(t.id) >= 0;
  }

  function _renderPlanTasks(host){
    host.textContent = '';
    var rows = (_plan.tasks || []).filter(_taskInTab);
    if(!rows.length){
      var none = document.createElement('p');
      none.className = 'plan-none';
      none.textContent = _planTab === 'done' ? 'Nothing closed yet.' : 'Nothing on this list.';
      host.appendChild(none);
      return;
    }
    rows.forEach(function(t){
      var row = document.createElement('div');
      row.className = 'plan-task';

      var box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'plan-check';
      box.checked = t.resolved_status === 'done' || t.resolved_status === 'closed_by_data';
      box.disabled = t.resolved_status === 'closed_by_data';   // the data closed it; a click cannot reopen it
      box.setAttribute('aria-label', t.title || t.id);
      box.addEventListener('change', function(){
        postPlanEvent(t.id, box.checked ? 'done' : 'open').then(loadClientPlan);
      });
      row.appendChild(box);

      var mid = document.createElement('div');
      mid.className = 'plan-task-body';
      var h = document.createElement('h4');
      h.textContent = t.title || t.id;
      mid.appendChild(h);
      var chips = document.createElement('div');
      chips.className = 'plan-chips';
      if(t.overdue) chips.appendChild(_chip('overdue', 'plan-chip-warn'));
      if(t.age_weeks >= 1 && t.resolved_status === 'open'){
        chips.appendChild(_chip('open ' + t.age_weeks + (t.age_weeks === 1 ? ' week' : ' weeks')));
      }
      if(t.inferred) chips.appendChild(_chip('inferred, confirm', 'plan-chip-warn'));
      if(t.resolved_status === 'closed_by_data') chips.appendChild(_chip('closed by the data'));
      if(t.owner) chips.appendChild(_chip(t.owner));
      mid.appendChild(chips);
      row.appendChild(mid);

      var btn = document.createElement('button');
      btn.className = 'btn plan-cta';
      btn.textContent = t.cta || 'Ask the agent';
      btn.addEventListener('click', function(){
        runDashboardAction(t.prompt || t.title, {
          item_id: t.id, title: t.title, kind: 'task', section: 'plan', week: _plan.week
        });
      });
      row.appendChild(btn);
      host.appendChild(row);
    });
  }

  function _wirePlanTabs(){
    var tabs = el('clientplanTabs');
    if(!tabs) return;
    tabs.addEventListener('click', function(ev){
      var b = ev.target && ev.target.closest ? ev.target.closest('[data-plan-tab]') : null;
      if(!b) return;
      _planTab = b.getAttribute('data-plan-tab');
      Array.prototype.forEach.call(tabs.querySelectorAll('[data-plan-tab]'), function(x){
        x.classList.toggle('is-active', x === b);
      });
      var host = el('clientplanTasks');
      if(host && _plan) _renderPlanTasks(host);
    });
  }

  // -- 6. Connect: the steps, never the key ------------------------------------
  //
  // The service account lives on the HOST, outside this process and outside the
  // agent's container. All the app does is show the email and where to paste it.

  var CONNECT_STEPS = {
    ga4: ['Open Google Analytics and go to Admin, then Property access management.',
          'Click Add users, paste the email below, and choose the role Viewer.',
          'Uncheck "Notify new users by email", then click Add.'],
    gsc: ['Open Search Console and go to Settings, then Users and permissions.',
          'Click Add user and paste the email below.',
          'Choose the permission Full, then click Add.']
  };

  async function openConnectModal(provider){
    var modal = el('connectModal');
    if(!modal) return;
    var name = CONNECT_PROVIDERS[provider] || provider;
    var title = el('connectModalTitle');
    if(title) title.textContent = 'Connect ' + name;
    var body = el('connectModalBody');
    if(body) body.textContent = '';

    var status = null;
    try{
      var r = await fetch('api/connections', {credentials: 'same-origin', cache: 'no-store'});
      if(r.ok) status = await r.json();
    }catch(e){ status = null; }
    var prov = status && status.providers ? status.providers[provider] : null;
    var email = status && status.sa_email ? status.sa_email : '';

    if(!status || !prov || prov.state === 'not_provisioned' || !email){
      var p = document.createElement('p');
      p.textContent = 'Axia is preparing your connection. The steps appear here once it is ready.';
      if(body) body.appendChild(p);
      modal.hidden = false;
      return;
    }

    var ol = document.createElement('ol');
    (CONNECT_STEPS[provider] || []).forEach(function(s){
      var li = document.createElement('li');
      li.textContent = s;
      ol.appendChild(li);
    });
    if(body) body.appendChild(ol);

    var mail = document.createElement('div');
    mail.className = 'connect-email';
    var code = document.createElement('code');
    code.textContent = email;
    mail.appendChild(code);
    var copy = document.createElement('button');
    copy.className = 'btn ghost';
    copy.textContent = 'Copy';
    copy.addEventListener('click', function(){
      try{ navigator.clipboard.writeText(email); }catch(e){}
      copy.textContent = 'Copied';
    });
    mail.appendChild(copy);
    if(body) body.appendChild(mail);

    var done = document.createElement('button');
    done.className = 'btn primary';
    done.textContent = "I've added it";
    done.addEventListener('click', function(){
      modal.hidden = true;
      runDashboardAction('I added ' + email + ' to ' + name + '. Please confirm when you can see it.',
        {kind: 'cta', title: 'Connect ' + name, section: 'channels', week: (_plan && _plan.week) || ''});
    });
    if(body) body.appendChild(done);
    modal.hidden = false;
  }

  function _wireConnectModal(){
    var modal = el('connectModal');
    if(!modal) return;
    modal.addEventListener('click', function(ev){
      if(ev.target === modal || (ev.target && ev.target.hasAttribute && ev.target.hasAttribute('data-connect-close'))){
        modal.hidden = true;
      }
    });
  }

  window.loadClientDashboard = loadClientDashboard;
  window.loadClientPlan = loadClientPlan;
  window.postPlanEvent = postPlanEvent;
  window.openConnectModal = openConnectModal;
  window.openClientDashboardPage = _show;
  window._clientModeOnPanel = _clientModeOnPanel;
  window.setAgentDrawerState = setDrawerState;
  window.refreshAgentDrawer = refreshDrawer;
  window.acceptDashboardMessage = acceptDashboardMessage;
  window.runDashboardAction = runDashboardAction;
  window.postToDashboard = postToDashboard;
  window.deriveDashboardWeeks = deriveWeeks;

  if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _start);
  else _start();
})();
