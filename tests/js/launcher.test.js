/* 漂浮球脚本的行为自测：用最小 DOM 桩在 node 里真跑 launcher.js 并断言关键行为。
 *
 * 为什么需要它：这个脚本一旦有运行时错误就"静默失败"（我们刻意吞掉了异常，
 * 以免影响 Zabbix 页面），浏览器里只会看到"球没出来"，排查成本高。
 * 实测已抓到过一次致命问题：关闭按钮变量与关闭函数重名（严格模式下直接整段加载失败）。
 *
 * 不依赖 jsdom，任何装了 node 的机器都能跑：
 *     node tests/js/launcher.test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'frontend', 'launcher.js'), 'utf8');

let failures = 0;
function check(label, cond, extra) {
  console.log('  [' + (cond ? 'OK' : 'FAIL') + '] ' + label + (extra ? '  ' + extra : ''));
  if (!cond) failures++;
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.style = {};
    this.attrs = {};
    this._classes = new Set();
    this._handlers = {};
    this.textContent = '';
    this.innerHTML = '';
    this.id = '';
    const self = this;
    this.classList = {
      add: (c) => { self._classes.add(c); },
      remove: (c) => { self._classes.delete(c); },
      contains: (c) => self._classes.has(c),
    };
  }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this._classes.add(c)); }
  get className() { return Array.from(this._classes).join(' '); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); }
  removeEventListener() {}
  focus() {}
  setPointerCapture() {}
  releasePointerCapture() {}
  fire(t, ev) { (this._handlers[t] || []).forEach((fn) => fn(ev || {})); }
  // 供 launcher.js 里「注销时清空对话」的选择器使用（closest 的最小实现）
  closest(sel) {
    const parts = String(sel).split(',').map((s) => s.trim());
    let node = this;
    while (node) {
      const tag = String(node.tagName || '').toUpperCase();   // 真实 DOM 的 tagName 是大写
      for (const p of parts) {
        if (p === 'a.icon-signout' && tag === 'A' && node._classes.has('icon-signout')) {
          return node;
        }
        if (p === 'a[href="#signout"]' && tag === 'A' && node.attrs.href === '#signout') {
          return node;
        }
        if (p.indexOf('li[onclick*=') === 0 && tag === 'LI'
            && String(node.attrs.onclick || '').indexOf('ZABBIX.logout') >= 0) {
          return node;
        }
      }
      node = node.parent;
    }
    return null;
  }
}

function makeMemoryStore() {
  const m = {};
  return {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
    setItem: (k, v) => { m[k] = String(v); },
    removeItem: (k) => { delete m[k]; },
  };
}

let env_beacons = [];

function makeEnv(opts) {
  opts = opts || {};
  const body = new El('body');
  const html = new El('html');
  const userLink = opts.loggedIn === false ? null : new El('a');
  if (userLink) {
    userLink.setAttribute('href', '/zabbix.php?action=userprofile.edit');
  }
  const links = (opts.themes || ['assets/styles/blue-theme.css']).map((href) => {
    const l = new El('link');
    l.setAttribute('href', href);
    return l;
  });
  const docHandlers = {};

  const document = {
    readyState: 'complete',
    body: body,
    documentElement: html,
    createElement: (t) => new El(t),
    addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); },
    removeEventListener: () => {},
    querySelector: (sel) => (userLink && String(sel).indexOf('userprofile.edit') >= 0 ? userLink : null),
    querySelectorAll: (sel) => (String(sel).indexOf('stylesheet') >= 0 ? links : []),
    fire: (t, ev) => { (docHandlers[t] || []).forEach((fn) => fn(ev || {})); },
  };

  const location = { search: opts.search === undefined ? '?action=dashboard.view' : opts.search };
  const win = {
    location: location,
    innerWidth: 1400,
    innerHeight: 900,
    matchMedia: () => ({ matches: !!opts.prefersDark, addEventListener() {} }),
    addEventListener: () => {},
    localStorage: makeMemoryStore(),
    sessionStorage: makeMemoryStore(),
  };

  const beacons = [];
  const sandbox = {
    window: win,
    document: document,
    location: location,
    localStorage: win.localStorage,
    sessionStorage: win.sessionStorage,
    console: { warn: () => {}, log: () => {} },
    setTimeout: setTimeout,
    navigator: {
      sendBeacon: (url, blob) => { beacons.push({url: url, body: blob && blob.body}); return true; },
    },
    Blob: function (parts) { this.body = parts && parts.join(''); },
  };
  env_beacons = beacons;
  sandbox.window.document = document;
  return { sandbox: sandbox, document: document, body: body, win: win };
}

function findByTitle(node, id) {
  if (node.id === id) return node;
  for (const c of node.children) {
    const hit = findByTitle(c, id);
    if (hit) return hit;
  }
  return null;
}

function run(src, env) {
  vm.runInNewContext(src, env.sandbox, { filename: 'launcher.js' });
  return env;
}

function freshRun(name, opts) {
  console.log('\n== ' + name + ' ==');
  const env = makeEnv(opts);
  run(SRC, env);
  return env;
}

// 1) dashboard + 已登录：小球与面板被创建，初始关闭
let env = freshRun('dashboard 已登录');
let fab = findByTitle(env.body, 'zbxai-fab');
let panel = findByTitle(env.body, 'zbxai-panel');
let style = findByTitle(env.body, 'zbxai-style');
check('创建了小球', !!fab);
check('创建了面板', !!panel);
check('注入了样式', !!style);
check('初始是关闭状态', panel && !panel.classList.contains('zbxai-open'));

// 2) 点击（未拖动）应打开面板并懒加载 iframe
fab.fire('pointerdown', { clientX: 100, clientY: 100, button: 0, pointerId: 1 });
fab.fire('pointerup', {});
const frame = findByTitle(env.body, 'zbxai-frame');
check('点击后打开面板', panel.classList.contains('zbxai-open'));
check('iframe 懒加载了内嵌地址',
  frame && /\/ai\/\?embed=bubble&theme=light/.test(frame.getAttribute('src') || ''),
  frame ? frame.getAttribute('src') : '(无 iframe)');
check('打开状态写入 sessionStorage', env.win.sessionStorage.getItem('zbxai.launcher.open') === '1');

// 3) Esc 关闭
env.document.fire('keydown', { key: 'Escape' });
check('Esc 可关闭面板', !panel.classList.contains('zbxai-open'));
check('关闭后清掉 sessionStorage', env.win.sessionStorage.getItem('zbxai.launcher.open') === null);

// 4) 拖动不应误触开关
fab.fire('pointerdown', { clientX: 100, clientY: 100, button: 0, pointerId: 2 });
fab.fire('pointermove', { clientX: 160, clientY: 150 });
fab.fire('pointerup', {});
check('拖动后保持关闭（不把拖动当点击）', !panel.classList.contains('zbxai-open'));
check('拖动位置已保存', !!env.win.localStorage.getItem('zbxai.launcher.pos'),
  env.win.localStorage.getItem('zbxai.launcher.pos'));

// 5) 深色主题要传给 iframe
env = freshRun('深色主题', { themes: ['assets/styles/dark-theme.css'] });
fab = findByTitle(env.body, 'zbxai-fab');
fab.fire('pointerdown', { clientX: 10, clientY: 10, button: 0, pointerId: 3 });
fab.fire('pointerup', {});
check('识别 dark 主题并传给 iframe',
  /theme=dark/.test(findByTitle(env.body, 'zbxai-frame').getAttribute('src') || ''));

// 6) 非 dashboard 页面：一颗球都不该创建
env = freshRun('告警列表页（非 dashboard）', { search: '?action=problem.view' });
check('非 dashboard 页面不创建任何元素',
  findByTitle(env.body, 'zbxai-fab') === null && env.body.children.length === 0);

// 7) 未登录（无个人资料入口）：同样不创建
env = freshRun('未登录/会话失效页', { loggedIn: false });
check('未登录页面不创建小球', findByTitle(env.body, 'zbxai-fab') === null);

// 8) 重复注入要幂等
env = freshRun('重复注入', {});
run(SRC, env);
const fabs = env.body.children.filter((c) => c.id === 'zbxai-fab').length;
check('重复执行只保留一个小球', fabs === 1, '实际 ' + fabs + ' 个');
check('重复执行只保留一个面板',
  env.body.children.filter((c) => c.id === 'zbxai-panel').length === 1);

// 9) 注销时尽力清空对话（任何页面都要生效：这段注册在 dashboard 判断之前）
env = freshRun('注销时清空对话', { search: '?action=problem.view' });
const signOut = new El('A');
signOut.className = 'icon-signout';
signOut.setAttribute('href', '#signout');
env.body.appendChild(signOut);
env.document.fire('click', { target: signOut });
check('点注销会发 beacon 清空对话',
  env_beacons.length === 1 && env_beacons[0].url === '/ai/api/chat',
  JSON.stringify(env_beacons));
check('beacon 内容是 clear 请求',
  env_beacons.length === 1 && /"clear":true/.test(String(env_beacons[0].body)),
  env_beacons.length ? String(env_beacons[0].body) : '(无)');
check('非 dashboard 页面仍然不创建小球（只挂注销钩子）',
  findByTitle(env.body, 'zbxai-fab') === null);

console.log('\n结果: ' + failures + ' 项失败');
process.exit(failures === 0 ? 0 : 1);
