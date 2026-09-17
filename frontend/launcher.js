/* Zabbix AI 助手 · 漂浮聊天球
 *
 * 由 nginx sub_filter 注入到每个页面末尾（见 deploy/nginx-launcher.conf 示例）。
 * 设计约束：
 *   1) 只在「dashboard 页面 + 已登录」时出现，其它页面零影响；
 *   2) 任何异常都必须被吞掉——绝不能因为我们的小球把 Zabbix 页面弄坏；
 *   3) iframe 不预设 sandbox，同源加载 /ai/，浏览器自动带上 zbx_session cookie，
 *      因此每个人看到的仍是自己权限范围内的数据（与 URL widget 完全一致）；
 *   4) 位置记忆在 localStorage，可拖动；打开状态记在 sessionStorage，
 *      dashboard 之间跳转（整页刷新）后自动恢复。
 */
(function () {
  'use strict';

  try {
    if (window.__ZBXAI_LAUNCHER__) return;   // 防止被注入两次
    window.__ZBXAI_LAUNCHER__ = true;

    // ---- 注销时尽力立刻清掉本轮对话（任何页面都要生效，所以放在 dashboard 判断之前）----
    // 真正的保证在服务端：对话绑定 Zabbix 登录会话（每次登录发新 sessionid），
    // 所以注销后重登必然看不到上一轮；这里只是让服务端记录也尽快消失。
    function clearConversationOnLogout() {
      try {
        var payload = JSON.stringify({message: '', clear: true});
        if (navigator.sendBeacon) {
          navigator.sendBeacon('/ai/api/chat', new Blob([payload], {type: 'application/json'}));
        } else {
          fetch('/ai/api/chat', {
            method: 'POST', keepalive: true, credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: payload
          });
        }
      } catch (e) { /* 清不掉也不影响"重登即新会话" */ }
    }

    document.addEventListener('click', function (e) {
      try {
        var el = e.target && e.target.closest
          ? e.target.closest('a.icon-signout, a[href="#signout"], li[onclick*="ZABBIX.logout"]')
          : null;
        if (el) clearConversationOnLogout();
      } catch (err) { /* 忽略 */ }
    }, true);

    // ---- 只在真正的 dashboard 页面启用 ----
    // （登录页、告警列表、图表页都不匹配 → 小球根本不创建）
    if (!/[?&]action=dashboard\.view(&|$)/.test(location.search || '')) return;

    // ---- 必须已登录：登录页/会话失效页没有个人资料入口 ----
    if (!document.querySelector('a[href*="userprofile.edit"]')) return;

    var POS_KEY = 'zbxai.launcher.pos';
    var OPEN_KEY = 'zbxai.launcher.open';
    var FAB_SIZE = 52;
    var GAP = 12;

    var pos = {right: 24, bottom: 24};
    try {
      var saved = JSON.parse(localStorage.getItem(POS_KEY) || 'null');
      if (saved && typeof saved.right === 'number' && typeof saved.bottom === 'number') pos = saved;
    } catch (e) { /* 存储不可用时用默认位置 */ }

    function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

    // ---- 主题：Zabbix 6.0 用 assets/styles/<主题>-theme.css ----
    function detectTheme() {
      var links = document.querySelectorAll('link[rel="stylesheet"]');
      for (var i = 0; i < links.length; i++) {
        var href = links[i].getAttribute('href') || '';
        if (/dark-theme\.css/i.test(href)) return 'dark';
        if (/(blue|light)-theme\.css/i.test(href)) return 'light';
      }
      try {
        if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) return 'dark';
      } catch (e) { /* 忽略 */ }
      return 'light';
    }

    var theme = detectTheme();
    var dark = theme === 'dark';

    // ---- 样式 ----
    var css = [
      '#zbxai-fab{position:fixed;width:' + FAB_SIZE + 'px;height:' + FAB_SIZE + 'px;border-radius:50%;',
      'border:none;padding:0;cursor:pointer;z-index:100000;display:flex;align-items:center;',
      'justify-content:center;background:#0275b8;color:#fff;box-shadow:0 4px 14px rgba(0,0,0,.30);',
      'transition:transform .15s ease;touch-action:none;user-select:none;-webkit-user-select:none}',
      '#zbxai-fab:hover{transform:scale(1.07)}',
      '#zbxai-fab.zbxai-drag{cursor:grabbing;transform:scale(1.02)}',
      '#zbxai-fab svg{width:26px;height:26px;display:block;pointer-events:none}',
      '#zbxai-panel{position:fixed;width:380px;max-width:calc(100vw - 24px);height:min(560px,74vh);',
      'z-index:100001;display:none;flex-direction:column;overflow:hidden;border-radius:10px;',
      'box-shadow:0 14px 44px rgba(0,0,0,.34);border:1px solid ' + (dark ? '#2e3842' : '#d9dee3') + ';',
      'background:' + (dark ? '#171d24' : '#ffffff') + '}',
      '#zbxai-panel.zbxai-open{display:flex}',
      '#zbxai-head{display:flex;align-items:center;gap:8px;padding:7px 10px;background:#0275b8;color:#fff;',
      'font:600 13px/1.5 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}',
      '#zbxai-head .zbxai-title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
      '#zbxai-head button,#zbxai-head a{background:transparent;border:0;color:#fff;cursor:pointer;',
      'font-size:15px;line-height:1;padding:3px 7px;border-radius:4px;text-decoration:none}',
      '#zbxai-head button:hover,#zbxai-head a:hover{background:rgba(255,255,255,.20)}',
      '#zbxai-frame{flex:1;width:100%;border:0;background:' + (dark ? '#171d24' : '#ffffff') + '}'
    ].join('');

    var style = document.createElement('style');
    style.id = 'zbxai-style';
    style.textContent = css;

    // ---- 元素 ----
    var fab = document.createElement('button');
    fab.id = 'zbxai-fab';
    fab.type = 'button';
    fab.title = 'Zabbix AI 助手（可拖动）';
    fab.setAttribute('aria-label', '打开 Zabbix AI 助手');
    fab.setAttribute('aria-expanded', 'false');
    fab.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">' +
      '<path d="M12 3C6.9 3 2.8 6.4 2.8 10.6c0 2.4 1.4 4.5 3.6 5.9-.1 1-.6 2.3-1.6 3.4 1.9-.2 3.4-.9 4.5-1.6 1 .2 2 .4 2.7.4 5.1 0 9.2-3.4 9.2-7.6S17.1 3 12 3z"/></svg>';

    var panel = document.createElement('div');
    panel.id = 'zbxai-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Zabbix AI 助手');

    var head = document.createElement('div');
    head.id = 'zbxai-head';
    var title = document.createElement('span');
    title.className = 'zbxai-title';
    title.textContent = 'Zabbix AI 助手';
    var pop = document.createElement('a');
    pop.href = '/ai/';
    pop.target = '_blank';
    pop.rel = 'noopener';
    pop.title = '在新标签页打开';
    pop.textContent = '⇗';
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.title = '关闭（Esc）';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.textContent = '✕';
    head.appendChild(title);
    head.appendChild(pop);
    head.appendChild(closeBtn);

    var frame = document.createElement('iframe');
    frame.id = 'zbxai-frame';
    frame.setAttribute('title', 'Zabbix AI 助手对话');
    // 刻意不设 sandbox：同源 iframe 才能带上 zbx_session，权限与 URL widget 一致
    panel.appendChild(head);
    panel.appendChild(frame);

    function applyPos() {
      var maxRight = Math.max(8, window.innerWidth - FAB_SIZE - 8);
      var maxBottom = Math.max(8, window.innerHeight - FAB_SIZE - 8);
      pos.right = clamp(pos.right, 8, maxRight);
      pos.bottom = clamp(pos.bottom, 8, maxBottom);
      fab.style.right = pos.right + 'px';
      fab.style.bottom = pos.bottom + 'px';
      var pw = Math.min(380, window.innerWidth - 24);
      panel.style.width = pw + 'px';
      panel.style.right = clamp(pos.right + FAB_SIZE - pw, 8, Math.max(8, window.innerWidth - pw - 8)) + 'px';
      panel.style.bottom = Math.min(pos.bottom + FAB_SIZE + GAP, window.innerHeight - 80) + 'px';
    }

    function savePos() {
      try { localStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch (e) { /* 忽略 */ }
    }

    function open() {
      if (!frame.getAttribute('src')) {
        // 懒加载：不打开就不请求对话接口
        frame.setAttribute('src', '/ai/?embed=bubble&theme=' + detectTheme());
      }
      applyPos();
      panel.classList.add('zbxai-open');
      fab.setAttribute('aria-expanded', 'true');
      try { sessionStorage.setItem(OPEN_KEY, '1'); } catch (e) { /* 忽略 */ }
    }

    function close() {
      panel.classList.remove('zbxai-open');
      fab.setAttribute('aria-expanded', 'false');
      try { sessionStorage.removeItem(OPEN_KEY); } catch (e) { /* 忽略 */ }
    }

    function toggle() {
      if (panel.classList.contains('zbxai-open')) close(); else open();
    }

    // ---- 拖动（pointer 事件同时覆盖鼠标/触摸）----
    var drag = null;
    fab.addEventListener('pointerdown', function (e) {
      if (e.button !== undefined && e.button !== 0) return;
      drag = {x: e.clientX, y: e.clientY, right: pos.right, bottom: pos.bottom, moved: false};
      fab.classList.add('zbxai-drag');
      try { fab.setPointerCapture(e.pointerId); } catch (err) { /* 忽略 */ }
    });
    fab.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
      if (!drag.moved) return;
      pos.right = drag.right - dx;
      pos.bottom = drag.bottom - dy;
      applyPos();
    });
    function endDrag() {
      if (!drag) return;
      var moved = drag.moved;
      drag = null;
      fab.classList.remove('zbxai-drag');
      if (moved) { applyPos(); savePos(); } else { toggle(); }
    }
    fab.addEventListener('pointerup', endDrag);
    fab.addEventListener('pointercancel', function () {
      drag = null;
      fab.classList.remove('zbxai-drag');
    });

    closeBtn.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && panel.classList.contains('zbxai-open')) close();
    });
    window.addEventListener('resize', function () { applyPos(); });

    // ---- 挂载 ----
    function mount() {
      document.body.appendChild(style);
      document.body.appendChild(fab);
      document.body.appendChild(panel);
      applyPos();
      try {
        if (sessionStorage.getItem(OPEN_KEY) === '1') open();   // 跨页刷新后恢复
      } catch (e) { /* 忽略 */ }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', mount);
    } else {
      mount();
    }
  } catch (e) {
    // 静默失败：小球出问题也绝不影响 Zabbix 页面
    try { console.warn('[zbx-ai-launcher]', e); } catch (e2) { /* 忽略 */ }
  }
})();
