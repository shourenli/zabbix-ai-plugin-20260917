/* index.html 内联脚本的行为自测：斜杠命令建议必须能纯键盘操作。
 *
 * 背景（用户反馈）：打 `/` 会弹出供应商列表，但**只能用鼠标点**，很麻烦。
 * 现在要求：默认高亮第一项，↑↓ 切换，Enter/Tab 补全，Esc 收起，鼠标点击仍然可用；
 * 且**已经开始写正文（出现空格）后，Enter 必须恢复为"发送"**，不能被补全抢走。
 *
 * 为什么要写成自测：这段脚本没有浏览器就点不到，而它一旦写错，
 * 表现是"打 `/` 没反应"或"按回车发不出去"，很容易被忽略。
 *
 * 用法: node tests/js/suggest.test.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML = fs.readFileSync(
  path.join(__dirname, '..', '..', 'frontend', 'index.html'), 'utf8');
const MATCH = HTML.match(/<script>([\s\S]*?)<\/script>/);
if (!MATCH) {
  console.error('  [FAIL] 没在 index.html 里找到内联 <script>');
  process.exit(1);
}
const SRC = MATCH[1];

let failures = 0;
function check(label, cond, extra) {
  console.log('  [' + (cond ? 'OK' : 'FAIL') + '] ' + label + (extra ? '  ' + extra : ''));
  if (!cond) failures++;
}

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = {};
    this.attrs = {};
    this._classes = new Set();
    this._handlers = {};
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.id = '';
    this.tabIndex = 0;
    const self = this;
    this.classList = {
      add: (c) => { self._classes.add(c); },
      remove: (c) => { self._classes.delete(c); },
      contains: (c) => self._classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !self._classes.has(c) : !!on;
        if (want) self._classes.add(c); else self._classes.delete(c);
        return want;
      },
    };
  }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return Array.from(this._classes).join(' '); }
  // 真实 DOM 里 innerHTML='' 会清空子节点；桩必须照做，
  // 否则列表会不断累加（实测踩过：3+3+2=8 项）
  set innerHTML(v) { this._html = String(v); if (this._html === '') this.children = []; }
  get innerHTML() { return this._html || ''; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; }
  addEventListener(t, fn) { (this._handlers[t] = this._handlers[t] || []).push(fn); }
  removeEventListener() {}
  focus() { this.focused = true; }
  fire(t, ev) { (this._handlers[t] || []).forEach((fn) => fn(ev || {})); }
}

function makeEnv() {
  const ids = ['chat', 'input', 'send', 'clear', 'theme', 'suggest', 'hint'];
  const nodes = {};
  ids.forEach((id) => { nodes[id] = new El(id === 'input' ? 'textarea' : 'div'); nodes[id].id = id; });

  const docHandlers = {};
  const document = {
    readyState: 'complete',
    documentElement: new El('html'),
    body: new El('body'),
    createElement: (t) => new El(t),
    getElementById: (id) => nodes[id] || null,
    addEventListener: (t, fn) => { (docHandlers[t] = docHandlers[t] || []).push(fn); },
    removeEventListener: () => {},
    fire: (t, ev) => { (docHandlers[t] || []).forEach((fn) => fn(ev || {})); },
  };

  const posts = [];
  const calls = [];
  const fetchStub = (url, opts) => {
    calls.push({ url: url, body: opts && opts.body });
    if (String(url).indexOf('chat') >= 0 && opts && opts.method === 'POST') {
      let msg = '';
      try { msg = JSON.parse(opts.body || '{}').message || ''; } catch (e) { /* 忽略 */ }
      if (msg) posts.push(msg);
      return Promise.resolve({
        status: 200, ok: true,
        json: () => Promise.resolve({ reply: '收到：' + msg, context_reset: false }),
      });
    }
    return Promise.resolve({
      status: 200, ok: true,
      json: () => Promise.resolve({
        messages: [], suppliers: ['SUPPLIER_A', 'CARRIER_B', 'CARRIER_B-备用'], ttl_minutes: 15,
      }),
    });
  };

  const store = () => {
    const m = {};
    return {
      getItem: (k) => (Object.prototype.hasOwnProperty.call(m, k) ? m[k] : null),
      setItem: (k, v) => { m[k] = String(v); },
      removeItem: (k) => { delete m[k]; },
    };
  };

  const location = { search: '' };
  const sandbox = {
    document: document,
    location: location,
    localStorage: store(),
    sessionStorage: store(),
    fetch: fetchStub,
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: setTimeout,
    URLSearchParams: URLSearchParams,
    console: { log: () => {}, warn: () => {}, error: () => {} },
    Date: Date,
  };
  sandbox.window = sandbox;
  sandbox.window.matchMedia = () => ({ matches: false, addEventListener() {} });
  sandbox.window.parent = sandbox;

  vm.runInNewContext(SRC, sandbox, { filename: 'index.html<script>' });
  return { nodes: nodes, posts: posts, calls: calls, sandbox: sandbox };
}

const el = (env, id) => env.nodes[id];
const suggestVisible = (env) => el(env, 'suggest').style.display === 'flex';
const items = (env) => el(env, 'suggest').children;
const selectedIndex = (env) => items(env).findIndex((b) => b.classList.contains('sel'));
const type = (env, text) => { el(env, 'input').value = text; el(env, 'input').fire('input', {}); };
// 返回 preventDefault 是否被调用：用来验证"方向键有没有被我们吞掉"
const key = (env, k, shift) => {
  let prevented = false;
  el(env, 'input').fire('keydown', {
    key: k, shiftKey: !!shift, preventDefault() { prevented = true; },
  });
  return prevented;
};

(async () => {
  const env = makeEnv();
  await new Promise((r) => setTimeout(r, 0));      // 等 loadHistory() 的 promise 落地

  console.log('== 初始状态 ==');
  check('未输入时不显示建议', !suggestVisible(env));

  console.log('\n== 打一个斜杠就应弹出并默认高亮第一项 ==');
  type(env, '/');
  check('弹出建议列表', suggestVisible(env));
  check('列出全部供应商', items(env).length === 3, '实际 ' + items(env).length + ' 项');
  check('默认高亮第一项（回车即可选中，无需鼠标）', selectedIndex(env) === 0);
  check('候选项带 role=option', items(env)[0].getAttribute('role') === 'option');
  check('候选项不抢 Tab 焦点', items(env)[0].tabIndex === -1);

  console.log('\n== ↑↓ 切换高亮（并循环）==');
  key(env, 'ArrowDown');
  check('↓ 移到第二项', selectedIndex(env) === 1);
  key(env, 'ArrowDown');
  key(env, 'ArrowDown');
  check('↓ 到底后回到第一项', selectedIndex(env) === 0);
  key(env, 'ArrowUp');
  check('↑ 从第一项绕到最后一项', selectedIndex(env) === 2);

  console.log('\n== Enter 补全（而不是发送）==');
  const postsBefore = env.posts.length;
  type(env, '/C');                            // 命中 CARRIER_B / CARRIER_B-备用，默认高亮第一项
  check('前缀过滤后默认高亮第一项', selectedIndex(env) === 0);
  key(env, 'Enter');
  check('补全为「/供应商名 + 空格」', el(env, 'input').value === '/CARRIER_B ', JSON.stringify(el(env, 'input').value));
  check('补全后收起列表', !suggestVisible(env));
  check('补全不会误发消息', env.posts.length === postsBefore);

  console.log('\n== Tab 也能补全 ==');
  type(env, '/');
  key(env, 'Tab');
  check('Tab 补全第一项', el(env, 'input').value === '/SUPPLIER_A ', JSON.stringify(el(env, 'input').value));

  console.log('\n== 已经开始写正文后，Enter 必须恢复为发送 ==');
  type(env, '/CARRIER_B 磁盘快满了');
  check('有空格后不再弹出建议', !suggestVisible(env));
  key(env, 'Enter');
  await new Promise((r) => setTimeout(r, 0));
  check('Enter 发出消息', env.posts.length === postsBefore + 1, JSON.stringify(env.posts));
  check('发出的是完整内容', env.posts[env.posts.length - 1] === '/CARRIER_B 磁盘快满了');

  console.log('\n== 鼠标点击仍然可用 ==');
  type(env, '/C');
  check('前缀过滤到 2 项', items(env).length === 2, '实际 ' + items(env).length);
  items(env)[1].onclick && items(env)[1].onclick();
  check('点第 2 项补全', el(env, 'input').value === '/CARRIER_B-备用 ', JSON.stringify(el(env, 'input').value));

  console.log('\n== Esc 收起 ==');
  type(env, '/');
  key(env, 'Escape');
  check('Esc 收起列表', !suggestVisible(env));

  console.log('\n== 完全匹配后不再提示（避免挡视线）==');
  type(env, '/SUPPLIER_A');
  check('恰好命中一项时仍可补全', suggestVisible(env));

  console.log('\n== 只有一个候选：方向键不该被吞掉 ==');
  check('单候选时列表仍显示', suggestVisible(env) && items(env).length === 1);
  check('单候选时方向键不被劫持（光标可正常上下移动）', key(env, 'ArrowDown') === false);
  check('单候选时高亮仍在第 1 项', selectedIndex(env) === 0);
  key(env, 'Enter');
  check('单候选时回车直接补全', el(env, 'input').value === '/SUPPLIER_A ',
    JSON.stringify(el(env, 'input').value));

  console.log('\n== 多个候选：方向键必须由我们接管 ==');
  type(env, '/');                             // 3 个候选
  check('多候选时方向键被接管（避免光标乱跑）', key(env, 'ArrowDown') === true);
  check('高亮随之移动到第 2 项', selectedIndex(env) === 1);

  console.log('\n结果: ' + failures + ' 项失败');
  process.exit(failures === 0 ? 0 : 1);
})();
