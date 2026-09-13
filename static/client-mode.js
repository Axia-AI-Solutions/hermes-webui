// Axia client mode — the Dashboard tab. Cosmetic layer; the boundary is the
// server gate (api/client_mode.py). The agent writes HTML files under
// <HERMES_HOME>/home/dashboard/; the server lists them at api/client-dashboard
// and serves each one, sandboxed, at dashboard/<name>. This file only decides
// which one the <iframe> shows.
(function(){
  'use strict';
  var _current = 'latest.html';

  function _label(page){
    if(page.name === 'latest.html') return 'This week';
    return page.name.replace(/\.html$/i, '');
  }

  function _when(page){
    if(!page.mtime) return '';
    try{ return new Date(page.mtime * 1000).toLocaleDateString(undefined, {year:'numeric', month:'short', day:'numeric'}); }
    catch(e){ return ''; }
  }

  function _show(name){
    _current = name;
    var frame = document.getElementById('clientdashFrame');
    if(frame) frame.src = 'dashboard/' + encodeURIComponent(name);
    var open = document.getElementById('clientdashOpen');
    if(open) open.href = 'dashboard/' + encodeURIComponent(name);
    var title = document.getElementById('clientdashTitle');
    if(title) title.textContent = name === 'latest.html' ? 'Dashboard' : 'Dashboard · ' + name.replace(/\.html$/i, '');
    document.querySelectorAll('.clientdash-item').forEach(function(el){
      el.classList.toggle('active', el.getAttribute('data-page') === name);
    });
  }

  function _render(list, pages){
    list.textContent = '';
    if(!pages.length){
      var empty = document.createElement('div');
      empty.className = 'clientdash-empty';
      empty.textContent = 'No dashboard yet. Ask your agent for this week’s dashboard and it will appear here.';
      list.appendChild(empty);
      return;
    }
    pages.forEach(function(page){
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'clientdash-item' + (page.name === _current ? ' active' : '');
      btn.setAttribute('data-page', page.name);
      var label = document.createElement('span');
      label.textContent = _label(page);
      btn.appendChild(label);
      var meta = document.createElement('span');
      meta.className = 'clientdash-meta';
      meta.textContent = _when(page);
      btn.appendChild(meta);
      btn.onclick = function(){ _show(page.name); };
      list.appendChild(btn);
    });
  }

  async function loadClientDashboard(force){
    var frame = document.getElementById('clientdashFrame');
    if(frame && (force || !frame.getAttribute('src'))) _show(_current);
    var list = document.getElementById('clientdashList');
    if(!list) return;
    try{
      var r = await fetch('api/client-dashboard', {credentials: 'same-origin', cache: 'no-store'});
      var data = r.ok ? await r.json() : {pages: []};
      _render(list, Array.isArray(data.pages) ? data.pages : []);
    }catch(e){
      _render(list, []);
    }
  }

  window.loadClientDashboard = loadClientDashboard;
  window.openClientDashboardPage = _show;
})();
