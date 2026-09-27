/* Behaviour tests for complaints_ui.js, run by test_complaints_ui.py (or on
   their own: `node test_complaints_ui.js`). No browser: the script runs in a
   vm context over a small fake DOM built from the #complaints-workspace markup
   in ui.html, with fake timers and a fake fetch that plays the server. Only
   what the script uses is modelled — enough to click, type and read back. */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert/strict');

const HERE = __dirname;
// CMS_UI_SOURCE runs the same tests against another copy of the script (e.g. an older one).
const SOURCE = fs.readFileSync(process.env.CMS_UI_SOURCE || path.join(HERE, 'complaints_ui.js'), 'utf8');
const HTML = fs.readFileSync(path.join(HERE, 'ui.html'), 'utf8');
const VOID = new Set(['input', 'br', 'img', 'meta', 'link', 'hr', 'source', 'wbr']);
const ENTITIES = {'&nbsp;': '\u00a0', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'"};
const decode = text => text.replace(/&(?:nbsp|amp|lt|gt|quot|#39);/g, e => ENTITIES[e]);

/* --------------------------------------------------------------- fake DOM */
class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.detail = init.detail;
    this.key = init.key;
    this.bubbles = init.bubbles !== false;
    this.defaultPrevented = false;
  }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
}
class FakeNode {
  constructor(doc) { this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; this.listeners = new Map(); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] || null;
  }
  get isConnected() { let n = this; while (n.parentNode) n = n.parentNode; return n === this.ownerDocument; }
  get textContent() { return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(value) { this.replaceChildren(...(value === '' || value === null || value === undefined ? [] : [String(value)])); }
  contains(other) { for (let n = other; n; n = n.parentNode) if (n === this) return true; return false; }
  toNodes(items) {
    const out = [];
    for (const item of items) {
      if (item instanceof FakeFragment) out.push(...item.childNodes);
      else if (item instanceof FakeNode) out.push(item);
      else out.push(this.ownerDocument.createTextNode(String(item)));
    }
    return out;
  }
  adopt(node, index) {
    if (node.parentNode) node.parentNode.detach(node);
    node.parentNode = this;
    this.childNodes.splice(index, 0, node);
  }
  detach(node) {
    const i = this.childNodes.indexOf(node);
    if (i >= 0) this.childNodes.splice(i, 1);
    node.parentNode = null;
  }
  changed() {}
  append(...items) { for (const n of this.toNodes(items)) this.adopt(n, this.childNodes.length); this.changed(); }
  prepend(...items) { this.toNodes(items).reverse().forEach(n => this.adopt(n, 0)); this.changed(); }
  insertBefore(node, ref) {
    for (const n of this.toNodes([node])) {
      if (n.parentNode) n.parentNode.detach(n);
      const i = ref ? this.childNodes.indexOf(ref) : -1;
      this.adopt(n, i < 0 ? this.childNodes.length : i);
    }
    this.changed();
    return node;
  }
  replaceChild(node, old) {
    const i = this.childNodes.indexOf(old);
    this.detach(old);
    this.adopt(node, i);
    this.changed();
    return old;
  }
  replaceChildren(...items) {
    const nodes = this.toNodes(items);
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    for (const n of nodes) this.adopt(n, this.childNodes.length);
    this.changed();
  }
  remove() { if (this.parentNode) this.parentNode.detach(this); }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(fn); }
  removeEventListener(type, fn) { const list = this.listeners.get(type) || []; const i = list.indexOf(fn); if (i >= 0) list.splice(i, 1); }
  dispatchEvent(event) {
    event.target = this;
    for (let n = this; n; n = n.parentNode) {
      event.currentTarget = n;
      for (const fn of [...(n.listeners.get(event.type) || [])]) fn.call(n, event);
      if (!event.bubbles || event.stopped) break;
    }
    return !event.defaultPrevented;
  }
  descendants() {
    const out = [];
    const walk = node => { for (const c of node.childNodes) if (c.nodeType === 1) { out.push(c); walk(c); } };
    walk(this);
    return out;
  }
  querySelectorAll(selector) { const list = parseSelector(selector); return this.descendants().filter(e => list.some(c => matchComplex(e, c))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
class FakeText extends FakeNode {
  constructor(doc, data) { super(doc); this.nodeType = 3; this.data = data; }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}
class FakeFragment extends FakeNode {
  constructor(doc) { super(doc); this.nodeType = 11; }
}
const kebab = key => key.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
class FakeElement extends FakeNode {
  constructor(doc, tag) {
    super(doc);
    this.nodeType = 1;
    this.localName = tag.toLowerCase();
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.style = {setProperty(key, value) { this[key] = String(value); }};
    this.scrollTop = 0;
    this.ownValue = '';
    this.chosen = false;              // <option> selectedness
    this.noneChosen = false;          // <select> set to a value it does not list
    const self = this;
    this.dataset = new Proxy({}, {
      get: (_, key) => (typeof key === 'string' && self.hasAttribute('data-' + kebab(key)) ? self.getAttribute('data-' + kebab(key)) : undefined),
      set: (_, key, value) => { self.setAttribute('data-' + kebab(key), value); return true; },
      has: (_, key) => self.hasAttribute('data-' + kebab(key)),
      deleteProperty: (_, key) => { self.removeAttribute('data-' + kebab(key)); return true; },
    });
    this.classList = {
      contains: name => self.className.split(/\s+/).includes(name),
      add: (...names) => { const set = new Set(self.className.split(/\s+/).filter(Boolean)); names.forEach(n => set.add(n)); self.className = [...set].join(' '); },
      remove: (...names) => { self.className = self.className.split(/\s+/).filter(n => n && !names.includes(n)).join(' '); },
      toggle: (name, force) => {
        const on = force === undefined ? !self.classList.contains(name) : !!force;
        if (on) self.classList.add(name); else self.classList.remove(name);
        return on;
      },
    };
  }
  get children() { return this.childNodes.filter(n => n.nodeType === 1); }
  setAttribute(name, value) { this.attrs.set(String(name).toLowerCase(), String(value)); }
  getAttribute(name) { const v = this.attrs.get(String(name).toLowerCase()); return v === undefined ? null : v; }
  hasAttribute(name) { return this.attrs.has(String(name).toLowerCase()); }
  removeAttribute(name) { this.attrs.delete(String(name).toLowerCase()); }
  get tabIndex() { const v = this.getAttribute('tabindex'); return v === null ? (['button', 'select', 'input', 'textarea', 'a'].includes(this.localName) ? 0 : -1) : Number(v); }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get options() { return this.localName === 'select' ? this.querySelectorAll('option') : undefined; }
  add(option) { this.append(option); }
  changed() { if (this.localName === 'select') this.noneChosen = false; }
  get value() {
    if (this.localName === 'select') {
      if (this.noneChosen) return '';
      const opts = this.options, chosen = opts.find(o => o.chosen);
      return chosen ? chosen.value : (opts[0] ? opts[0].value : '');
    }
    if (this.localName === 'option') return this.hasAttribute('value') ? this.getAttribute('value') : this.textContent;
    if (this.localName === 'button') return this.getAttribute('value') || '';
    return this.ownValue;
  }
  set value(value) {
    const v = String(value);
    if (this.localName === 'select') {
      let found = false;
      for (const o of this.options) { o.chosen = !found && o.value === v; found = found || o.chosen; }
      this.noneChosen = !found;
    } else if (this.localName === 'option') this.setAttribute('value', v);
    else this.ownValue = v;
  }
  get selected() { return this.chosen; }
  focus() { if (!this.disabled) this.ownerDocument.focused = this; }
  blur() { if (this.ownerDocument.focused === this) this.ownerDocument.focused = null; }
  select() {}
  click() { if (!this.disabled) this.dispatchEvent(new FakeEvent('click')); }
  getBoundingClientRect() { return {left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0}; }
  get clientWidth() { return 0; }
  scrollIntoView() {}
  matches(selector) { return parseSelector(selector).some(c => matchComplex(this, c)); }
  closest(selector) { for (let n = this; n && n.nodeType === 1; n = n.parentNode) if (n.matches(selector)) return n; return null; }
}
for (const [prop, attr] of [['id', 'id'], ['className', 'class'], ['title', 'title'], ['dir', 'dir'], ['type', 'type'], ['name', 'name'],
  ['href', 'href'], ['download', 'download'], ['src', 'src'], ['alt', 'alt'], ['scope', 'scope']]) {
  Object.defineProperty(FakeElement.prototype, prop, {
    get() { return this.getAttribute(attr) || ''; },
    set(value) { this.setAttribute(attr, value); },
  });
}
for (const prop of ['hidden', 'disabled', 'open', 'checked']) {
  Object.defineProperty(FakeElement.prototype, prop, {
    get() { return this.hasAttribute(prop); },
    set(value) {
      if (value) this.setAttribute(prop, ''); else this.removeAttribute(prop);
      // As in Chrome: a focused control that gets disabled drops the focus to <body>.
      if (value && prop === 'disabled' && this.ownerDocument.focused === this) this.ownerDocument.focused = null;
    },
  });
}
class FakeDocument extends FakeNode {
  constructor() {
    super(null);
    this.ownerDocument = this;
    this.nodeType = 9;
    this.focused = null;
    this.hidden = false;
    this.documentElement = this.createElement('html');
    this.body = this.createElement('body');
    this.documentElement.append(this.body);
    this.adopt(this.documentElement, 0);
  }
  get activeElement() { return this.focused && this.focused.isConnected ? this.focused : this.body; }
  createElement(tag) { return new FakeElement(this, tag); }
  createElementNS(_, tag) { return new FakeElement(this, tag); }
  createTextNode(text) { return new FakeText(this, String(text)); }
  createDocumentFragment() { return new FakeFragment(this); }
  getElementById(id) { return this.descendants().find(e => e.getAttribute('id') === id) || null; }
  execCommand() { return false; }
}

/* Selectors: compound parts (tag, #id, .class, [attr], [attr="v"]) joined by
   the descendant combinator, in comma lists. Anything else throws, so a test
   never passes because a selector silently matched nothing. */
function parseSelector(selector) {
  return selector.split(',').map(part => part.trim().split(/\s+/).map(text => {
    const out = {tag: null, id: null, classes: [], attrs: []};
    const re = /([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/y;
    let pos = 0;
    while (pos < text.length) {
      re.lastIndex = pos;
      const m = re.exec(text);
      if (!m) throw new Error('unsupported selector: ' + selector);
      if (m[1]) { if (pos) throw new Error('unsupported selector: ' + selector); out.tag = m[1].toLowerCase(); }
      else if (m[2]) out.id = m[2];
      else if (m[3]) out.classes.push(m[3]);
      else out.attrs.push([m[4], m[5]]);
      pos = re.lastIndex;
    }
    return out;
  }));
}
function matchCompound(el, c) {
  if (c.tag && el.localName !== c.tag) return false;
  if (c.id && el.getAttribute('id') !== c.id) return false;
  if (!c.classes.every(name => el.classList.contains(name))) return false;
  return c.attrs.every(([name, value]) => el.hasAttribute(name) && (value === undefined || el.getAttribute(name) === value));
}
function matchComplex(el, compounds) {
  if (!matchCompound(el, compounds[compounds.length - 1])) return false;
  let i = compounds.length - 2;
  for (let n = el.parentNode; i >= 0 && n; n = n.parentNode) if (n.nodeType === 1 && matchCompound(n, compounds[i])) i--;
  return i < 0;
}

/* The #complaints-workspace section of ui.html, parsed into the fake body. */
function buildMarkup(doc) {
  const start = HTML.indexOf('<section id="complaints-workspace"');
  assert.ok(start >= 0, 'ui.html has no #complaints-workspace');
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*"[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  re.lastIndex = start;
  const stack = [doc.body];
  for (let m = re.exec(HTML); m; m = re.exec(HTML)) {
    if (m[0].startsWith('<!--')) continue;
    const top = stack[stack.length - 1];
    if (m[5] !== undefined) { top.append(decode(m[5])); continue; }
    const tag = m[2].toLowerCase();
    if (m[1]) {
      while (stack.length > 1 && stack.pop().localName !== tag) { /* unwind to the match */ }
      if (stack.length === 1) return;
      continue;
    }
    const node = doc.createElement(tag);
    for (const a of m[3].matchAll(/([^\s=>/]+)(?:\s*=\s*"([^"]*)")?/g)) node.setAttribute(a[1], a[2] === undefined ? '' : decode(a[2]));
    top.append(node);
    if (!VOID.has(tag) && !m[4]) stack.push(node);
  }
}

/* ------------------------------------------------------ timers, storage */
const settle = async () => { for (let i = 0; i < 25; i++) await new Promise(resolve => setImmediate(resolve)); };
function makeClock() {
  let now = 0, seq = 0;
  const timers = new Map();
  return {
    setTimeout(fn, ms, ...args) { const id = ++seq; timers.set(id, {id, at: now + Math.max(0, Number(ms) || 0), fn, args}); return id; },
    clearTimeout(id) { timers.delete(id); },
    pending() { return timers.size; },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await settle();
        const due = [...timers.values()].filter(t => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        timers.delete(due.id);
        now = due.at;
        due.fn(...due.args);
      }
      now = end;
      await settle();
    },
  };
}
class FakeStorage {
  constructor(entries = {}) { this.map = new Map(Object.entries(entries)); }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null; }
  setItem(key, value) { this.map.set(key, String(value)); }
  removeItem(key) { this.map.delete(key); }
}

/* ------------------------------------------------------------ fixtures */
const DAY = 24 * 3600e3;
const iso = ms => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
const TAXONOMY = {
  receiving_entity: {id: 'riyadh_emirate', label_ar: 'إمارة منطقة الرياض', label_en: 'Riyadh Region Emirate', region: 'riyadh', desk_ar: 'إدارة الشكاوى'},
  categories: [
    {id: 'municipal_services', label_ar: 'الخدمات البلدية', ministry: 'municipal', subcategories: [{id: 'roads', label_ar: 'الطرق'}]},
    {id: 'health', label_ar: 'الخدمات الصحية', ministry: 'health', subcategories: []},
  ],
  ministries: [{id: 'municipal', label_ar: 'وزارة البلديات والإسكان'}, {id: 'health', label_ar: 'وزارة الصحة'}],
  priorities: [{id: 'critical', label_ar: 'حرجة'}, {id: 'high', label_ar: 'عالية'}, {id: 'medium', label_ar: 'متوسطة'}, {id: 'low', label_ar: 'منخفضة'}],
  statuses: [{id: 'new', label_ar: 'جديدة', open: true}, {id: 'in_review', label_ar: 'قيد الدراسة', open: true}, {id: 'resolved', label_ar: 'تم الحل', open: false}],
  governorates: [{id: 'riyadh_city', label_ar: 'مدينة الرياض'}, {id: 'kharj', label_ar: 'الخرج'}, {id: 'unknown', label_ar: 'غير محددة'}],
  regions: [{id: 'riyadh', label_ar: 'منطقة الرياض'}, {id: 'madinah', label_ar: 'منطقة المدينة المنورة'}, {id: 'unknown', label_ar: 'غير محددة'}],
  review_reasons: {outside_jurisdiction: 'موقع الشكوى خارج نطاق منطقة الرياض'},
  signals: [], factors: [], scopes: [], tones: [],
};
const CONFIG = {taxonomy: TAXONOMY, providers: [{id: 'qwen', label: 'Qwen3-4B', status: 'ready', local: true, model: 'q', n_ctx: 8192}],
  active_provider: 'qwen', limits: {max_files: 20, max_bytes: 104857600, max_text_chars: 40000}};
const IDLE = {queued: 0, processing: null, errors: 0, worker: 'running', active_provider: 'qwen'};
function summary(over = {}) {
  const id = over.id || 28;
  return {id, ref: 'CMP-2026-' + String(id).padStart(6, '0'), created_at: iso(Date.now() - DAY), updated_at: iso(Date.now() - DAY),
    source: 'text', filename: 'شكوى.txt', file_kind: null, page_count: 0, stage: 'done', error: null, status: 'new',
    subject: 'حفرة في طريق الملك فهد', summary: 'ملخص', complainant_name: 'سالم', category: 'municipal_services', subcategory: 'roads',
    ministry: 'municipal', priority: 'critical', region: 'riyadh', governorate: 'riyadh_city', model_category: 'municipal_services',
    model_ministry: 'municipal', model_priority: 'critical', model_governorate: 'riyadh_city', needs_review: false, review_reasons: [],
    reviewed: false, due_at: iso(Date.now() + 3 * DAY), provider: 'qwen', model: 'q', ...over};
}
function detail(over = {}) {
  const base = summary(over);
  return {...base, text: 'سطر أول من الشكوى', timings: {}, national_id: '', model_region: 'riyadh', processed_at: base.created_at,
    attempts: 1, file_ext: 'txt', file_available: false, feedback: [], events: [], acknowledgment: 'نص الرد المقترح',
    analysis: {structured: {fields: [], region: {id: 'riyadh', source: 'place_map'}, governorate: {id: 'riyadh_city', source: 'place_map'}},
      classification: {category: base.category, ministry: base.ministry, priority: base.priority}, review_reasons: base.review_reasons},
    ...over};
}

/* One page load: the script over a fresh fake DOM, a fake server `routes`
   (method, path, body) → {status?, json?, blob?, type?} (undefined → 404),
   and the workspace tab made visible the way comparison_ui.js does it. */
async function boot({routes = () => undefined, session = null, local = {}} = {}) {
  const doc = new FakeDocument();
  buildMarkup(doc);
  const clock = makeClock();
  const calls = [], confirms = [];
  const server = {confirmAnswer: true};
  const respond = (method, url, body) => {
    const pathOnly = url.split('?')[0];
    const custom = routes(method, url, body);
    if (custom !== undefined) return custom;
    if (pathOnly === '/complaints/config') return {json: CONFIG};
    if (pathOnly === '/complaints/queue') return {json: IDLE};
    if (pathOnly === '/complaints/items') return {json: {items: [], total: 0}};
    if (pathOnly === '/complaints/analytics') return {json: {totals: {}, model_quality: {}}};
    return {status: 404, json: {error: 'غير موجود'}};
  };
  async function fetch(url, init = {}) {
    const method = (init.method || 'GET').toUpperCase();
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({method, url, body});
    const r = respond(method, url, body);
    const status = r.status || 200;
    return {ok: status >= 200 && status < 300, status,
      json: async () => (r.json === undefined ? null : JSON.parse(JSON.stringify(r.json))),
      blob: async () => new Blob([r.blob || ''], {type: r.type || ''}),
      headers: {get: name => (name.toLowerCase() === 'content-type' ? r.type || 'application/json' : null)}};
  }
  class FakeXHR {
    constructor() { this.listeners = {}; this.upload = {addEventListener() {}}; }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader() {}
    addEventListener(type, fn) { this.listeners[type] = fn; }
    send(form) {
      calls.push({method: this.method, url: this.url, files: form.entries.map(e => e[2])});
      setImmediate(() => {
        const r = respond(this.method, this.url, form);
        this.status = r.status || 200;
        this.responseText = JSON.stringify(r.json);
        this.listeners.load();
      });
    }
  }
  class FakeFormData { constructor() { this.entries = []; } append(key, value, name) { this.entries.push([key, value, name]); } }
  const windowListeners = new Map();
  let blobs = 0;
  const revoked = [], observers = [];
  /* No CSS container units here (window.CSS is absent), so the script sizes
     the panel itself: every ResizeObserver it makes is kept for the tests. */
  class FakeResizeObserver {
    constructor(callback) { this.callback = callback; this.targets = []; this.disconnected = false; observers.push(this); }
    observe(target) { this.targets.push(target); }
    disconnect() { this.disconnected = true; this.targets = []; }
    fire() { this.callback([]); }
  }
  const win = {
    document: doc, console, Blob, TextDecoder, AbortController,
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    fetch, XMLHttpRequest: FakeXHR, FormData: FakeFormData,
    localStorage: new FakeStorage(local),
    sessionStorage: new FakeStorage(session ? {'cms.session': JSON.stringify(session)} : {}),
    URL: {createObjectURL: () => 'blob:test/' + (++blobs), revokeObjectURL: url => revoked.push(url)},
    MutationObserver: class { observe() {} }, ResizeObserver: FakeResizeObserver,
    Event: FakeEvent, CustomEvent: FakeEvent, Node: FakeNode,
    Option: function Option(text, value) {
      const o = doc.createElement('option');
      o.textContent = text;
      if (value !== undefined) o.setAttribute('value', value);
      return o;
    },
    getComputedStyle: () => ({direction: 'rtl'}),
    matchMedia: () => ({matches: false}),
    innerWidth: 1280, isSecureContext: false, navigator: {},
    confirm: message => { confirms.push(message); return server.confirmAnswer; },
    addEventListener: (type, fn) => { if (!windowListeners.has(type)) windowListeners.set(type, []); windowListeners.get(type).push(fn); },
    dispatchEvent: event => { for (const fn of windowListeners.get(event.type) || []) fn(event); return true; },
  };
  win.window = win;
  vm.createContext(win);
  vm.runInContext(SOURCE, win, {filename: 'complaints_ui.js'});
  const $ = id => doc.getElementById(id);
  $('complaints-workspace').hidden = false;
  win.dispatchEvent(new FakeEvent('workspace:change'));
  await clock.advance(0);
  const h = {
    doc, win, clock, calls, confirms, server, $, revoked, observers,
    q: selector => doc.querySelector(selector),
    qa: selector => doc.querySelectorAll(selector),
    settle: () => clock.advance(0),
    buttonIn(scope, text) {
      const found = scope.querySelectorAll('button').find(b => b.textContent.trim() === text);
      assert.ok(found, `no button «${text}»`);
      return found;
    },
    press(scope, text) { const b = h.buttonIn(scope, text); b.focus(); b.click(); return b; },
    block(title) {
      const section = $('cms-d-info').querySelectorAll('section').find(s => s.firstChild && s.firstChild.textContent === title);
      assert.ok(section, `no detail block «${title}»`);
      return section;
    },
    change(node, value) { node.value = value; node.dispatchEvent(new FakeEvent('change')); },
    async view(name) { $('cms-tab-' + name).click(); await clock.advance(0); },
    row: id => doc.querySelector(`#cms-rows tr.cms-rrow[data-id="${id}"]`),
    toggle: id => h.row(id).querySelector('.cms-rtoggle'),
    panelRow: () => $('cms-detail').closest('tr.cms-detail-row'),
    /* The panel is open under the row of complaint `id` (or detached on top when the row is not listed). */
    assertExpanded(id, message = '') {
      const panel = $('cms-detail'), holder = h.panelRow(), row = h.row(id);
      assert.ok(!panel.hidden && holder, `the panel is not expanded ${message}`);
      assert.equal(doc.querySelectorAll('#cms-rows tr.cms-detail-row').length, 1, 'one panel at a time');
      if (row) {
        assert.ok(row.nextSibling === holder, `the panel is not right under row ${id} ${message}`);
        assert.equal(row.querySelector('.cms-rtoggle').getAttribute('aria-expanded'), 'true');
        assert.ok(row.classList.contains('is-expanded'));
      }
      for (const other of doc.querySelectorAll('#cms-rows tr.cms-rrow')) {
        if (other !== row) assert.equal(other.querySelector('.cms-rtoggle').getAttribute('aria-expanded'), 'false', 'another row says expanded');
      }
    },
    assertCollapsed() {
      assert.ok($('cms-detail').hidden, 'the panel is still shown');
      assert.ok(!h.panelRow(), 'the panel is still inside a register row');
      assert.equal(doc.querySelectorAll('#cms-rows tr.cms-detail-row').length, 0);
      for (const row of doc.querySelectorAll('#cms-rows tr.cms-rrow')) assert.equal(row.querySelector('.cms-rtoggle').getAttribute('aria-expanded'), 'false');
    },
    async openDetail(id) {
      await h.view('register');
      assert.ok(h.row(id), `register row ${id} missing`);
      h.toggle(id).click();
      await clock.advance(0);
      h.assertExpanded(id);
    },
    async collapse() { $('cms-d-collapse').click(); await clock.advance(0); h.assertCollapsed(); },
    posts: suffix => calls.filter(c => c.method === 'POST' && c.url.endsWith(suffix)),
    count: prefix => calls.filter(c => c.url.split('?')[0] === prefix).length,
  };
  return h;
}
/* A register with these rows, and GET /items/{id} answered from `details`. */
function registerRoutes(rows, details = {}, extra = () => undefined) {
  return (method, url, body) => {
    const custom = extra(method, url, body);
    if (custom !== undefined) return custom;
    const pathOnly = url.split('?')[0];
    if (method === 'GET' && pathOnly === '/complaints/items') return {json: {items: rows, total: rows.length}};
    const m = /^\/complaints\/items\/(\d+)$/.exec(pathOnly);
    if (method === 'GET' && m && details[m[1]]) return {json: typeof details[m[1]] === 'function' ? details[m[1]]() : details[m[1]]};
    return undefined;
  };
}

/* ---------------------------------------------------------------- tests */
const tests = [];
const test = (name, fn) => tests.push({name, fn});

test('confirm_with_edits_saves_correction', async () => {
  // F1: «تأكيد التصنيف» after editing a select must not post the model's values as confirmed.
  const h = await boot({routes: registerRoutes([summary()], {28: detail()},
    (method, url) => (method === 'POST' && url.endsWith('/feedback') ? {json: detail({priority: 'low', reviewed: true})} : undefined))});
  await h.openDetail(28);
  const form = h.block('تقييم المراجع');
  h.change(form.querySelector('select[name="priority"]'), 'low');
  h.press(form, 'تأكيد التصنيف');
  await h.settle();
  assert.equal(h.confirms.length, 1, 'the reviewer is asked before the edits are saved');
  assert.match(h.confirms[0], /الأولوية/);
  const [post] = h.posts('/items/28/feedback');
  assert.ok(post, 'feedback posted');
  assert.equal(post.body.verdict, 'correct');
  assert.deepEqual(post.body.changes, {priority: 'low'});
});

test('confirm_with_edits_cancelled_posts_nothing', async () => {
  const h = await boot({routes: registerRoutes([summary()], {28: detail()})});
  await h.openDetail(28);
  h.server.confirmAnswer = false;
  const form = h.block('تقييم المراجع');
  const select = form.querySelector('select[name="priority"]');
  h.change(select, 'low');
  h.press(form, 'تأكيد التصنيف');
  await h.settle();
  assert.equal(h.confirms.length, 1);
  assert.equal(h.posts('/feedback').length, 0, 'nothing is saved when the reviewer cancels');
  assert.equal(select.value, 'low', 'the edits stay in the form');
  assert.match(form.textContent, /لم يُحفظ شيء/);
});

test('clean_confirm_posts_confirm', async () => {
  const h = await boot({routes: registerRoutes([summary()], {28: detail()},
    (method, url) => (method === 'POST' && url.endsWith('/feedback') ? {json: detail({reviewed: true})} : undefined))});
  await h.openDetail(28);
  h.press(h.block('تقييم المراجع'), 'تأكيد التصنيف');
  await h.settle();
  assert.equal(h.confirms.length, 0);
  const [post] = h.posts('/items/28/feedback');
  assert.equal(post.body.verdict, 'confirm');
  assert.deepEqual(post.body.changes, {});
});

test('offlist_session_item_is_followed_and_polling_stops', async () => {
  // F2: a session job older than the newest 200 rows gets its stage by id; the idle queue ends polling.
  const newer = Array.from({length: 200}, (_, i) => summary({id: 1001 + i}));
  const h = await boot({session: [{id: 28, duplicate: false, filename: 'قديمة.txt'}],
    routes: registerRoutes(newer, {28: detail({stage: 'done', subject: 'شكوى قديمة'})})});
  const row = h.q('#cms-intake-list [data-key="i28"]');
  assert.ok(row, 'the session job is listed');
  assert.equal(row.dataset.stage, 'done', 'its stage came from GET /items/28');
  assert.ok(h.calls.some(c => c.url === '/complaints/items/28'));
  const polls = h.count('/complaints/queue');
  await h.clock.advance(10000);
  assert.equal(h.count('/complaints/queue'), polls, 'no polling once the queue is idle');
});

test('offlist_detail_follows_processing', async () => {
  // F2: an open panel on a processing item outside the intake window still updates.
  let stage = 'structuring';
  const newer = Array.from({length: 200}, (_, i) => summary({id: 1001 + i}));
  const rows = [summary({id: 28, stage})].concat(newer);
  const h = await boot({routes: registerRoutes(rows, {28: () => detail({stage})},
    (method, url) => {
      if (url.split('?')[0] === '/complaints/queue') return {json: stage === 'done' ? IDLE : {...IDLE, processing: {id: 28, ref: 'CMP-2026-000028', stage}}};
      if (url.includes('limit=200')) return {json: {items: newer, total: 201}};      // the intake window misses #28
      return undefined;
    })});
  await h.openDetail(28);
  assert.match(h.$('cms-d-info').textContent, /قيد المعالجة/);
  stage = 'done';
  await h.clock.advance(2100);
  assert.match(h.$('cms-d-info').textContent, /تقييم المراجع/, 'the panel shows the finished complaint');
});

test('outside_badge_follows_server_flag', async () => {
  // F3 / (a): the badge comes from summary.outside_jurisdiction; review_reasons only when it is absent.
  const rows = [
    summary({id: 1, region: 'madinah', governorate: 'unknown', outside_jurisdiction: true, review_reasons: []}),
    summary({id: 2, region: 'riyadh', outside_jurisdiction: false, review_reasons: ['outside_jurisdiction']}),
    summary({id: 3, region: 'madinah', review_reasons: ['outside_jurisdiction']}),
  ];
  const h = await boot({routes: registerRoutes(rows, {
    1: detail(rows[0]), 2: detail({...rows[1], analysis: {structured: {fields: []}, classification: {}, review_reasons: ['outside_jurisdiction']}})})});
  await h.view('register');
  const badge = id => !!h.q(`#cms-rows tr[data-id="${id}"] .cms-tag.is-out`);
  assert.equal(badge(1), true, 'reviewer moved it to another region: outside');
  assert.equal(badge(2), false, 'reviewer moved it back home: not outside');
  assert.equal(badge(3), true, 'no flag from the server: the review reason decides');
  await h.openDetail(1);
  assert.ok(h.q('#cms-d-info .cms-tag.is-out'), 'the region card carries the badge');
  await h.collapse();
  await h.openDetail(2);
  assert.ok(!h.q('#cms-d-info .cms-tag.is-out'), 'no badge once the reviewer moved it home');
});

test('saving_keeps_other_form_draft_and_focus', async () => {
  // F4: a save rebuilds the panel; the other form's unsaved input and the focus survive.
  let current = detail();
  const h = await boot({routes: registerRoutes([summary()], {28: () => current}, (method, url, body) => {
    if (method === 'POST' && url.endsWith('/feedback')) { current = detail({...body.changes, reviewed: true}); return {json: current}; }
    if (method === 'POST' && url.endsWith('/status')) { current = {...current, status: body.status}; return {json: current}; }
    return undefined;
  })});
  await h.openDetail(28);
  h.block('حالة المتابعة').querySelector('input').value = 'ملاحظة لم تُحفظ بعد';
  const form = h.block('تقييم المراجع');
  h.change(form.querySelector('select[name="priority"]'), 'low');
  h.press(form, 'حفظ التصحيح');
  await h.settle();
  assert.equal(h.posts('/feedback').length, 1);
  assert.equal(h.block('حالة المتابعة').querySelector('input').value, 'ملاحظة لم تُحفظ بعد', 'the status note survives');
  let active = h.doc.activeElement;
  assert.equal(active.localName, 'button', 'focus did not fall to <body>');
  assert.equal(active.textContent, 'حفظ التصحيح');
  assert.ok(h.block('تقييم المراجع').contains(active));
  assert.equal(h.block('تقييم المراجع').querySelector('select[name="priority"]').value, 'low', 'the saved form shows the stored value');

  h.block('تقييم المراجع').querySelector('textarea').value = 'ملاحظة المراجع';
  const status = h.block('حالة المتابعة');
  h.change(status.querySelector('select[name="status"]'), 'in_review');
  h.press(status, 'تحديث الحالة');
  await h.settle();
  assert.equal(h.posts('/status').length, 1);
  assert.equal(h.block('تقييم المراجع').querySelector('textarea').value, 'ملاحظة المراجع', 'the feedback note survives');
  assert.equal(h.block('حالة المتابعة').querySelector('input').value, '', 'the saved form starts clean');
  active = h.doc.activeElement;
  assert.equal(active.textContent, 'تحديث الحالة');
  assert.ok(active.isConnected);
});

test('queue_chip_untouched_when_unchanged', async () => {
  // F5: the polite live chip is re-rendered only when its words change.
  const busy = {...IDLE, queued: 1, processing: {id: 5, ref: 'CMP-2026-000005', stage: 'structuring'}};
  const h = await boot({routes: (method, url) => (url === '/complaints/queue' ? {json: busy} : undefined)});
  const chip = h.$('cms-queue'), first = chip.firstChild;
  assert.match(chip.textContent, /الهيكلة/);
  const polls = h.count('/complaints/queue');
  await h.clock.advance(4100);
  assert.ok(h.count('/complaints/queue') >= polls + 2, 'still polling');
  // (assert.ok, not equal: a failing equal would print the whole fake document)
  assert.ok(chip.firstChild === first, 'unchanged text: the chip was not rebuilt');
  busy.processing = {id: 5, ref: 'CMP-2026-000005', stage: 'classifying'};
  await h.clock.advance(2100);
  assert.ok(chip.firstChild !== first, 'new text: the chip was rebuilt');
  assert.match(chip.textContent, /التصنيف والأولوية/);
});

test('empty_search_is_announced', async () => {
  // F5: the live count says there are no matches.
  const h = await boot({routes: registerRoutes([summary()], {}, (method, url) => (url.includes('q=') ? {json: {items: [], total: 0}} : undefined))});
  await h.view('register');
  h.$('cms-f-q').value = 'zzzqqq';
  h.$('cms-filters').dispatchEvent(new FakeEvent('submit'));
  await h.settle();
  assert.equal(h.$('cms-register-count').textContent, 'لا توجد شكاوى مطابقة.');
  assert.ok(h.$('cms-register-count').classList.contains('cms-sr-only'), 'shown once on screen, by the empty state');
});

test('upload_status_counts_browser_rejections', async () => {
  // F6: refusals made in the browser are counted and always announced.
  const h = await boot({routes: (method, url) => (method === 'POST' && url === '/complaints/upload'
    ? {json: {items: [{id: 40, ref: 'CMP-2026-000040', filename: 'a.pdf', stage: 'queued'}]}} : undefined)});
  const picker = h.$('cms-files'), status = h.$('cms-upload-status');
  const pick = async files => { picker.files = files; picker.dispatchEvent(new FakeEvent('change')); await h.settle(); };
  await pick([{name: 'notes.docx', size: 10, type: ''}, {name: 'empty.txt', size: 0, type: 'text/plain'}]);
  assert.equal(status.textContent, 'لم يُستلم أي ملف · رُفض ملفان؛ الأسباب في القائمة أدناه.');
  await pick([{name: 'notes.docx', size: 10, type: ''}]);
  assert.equal(status.textContent, 'لم يُستلم أي ملف · رُفض ملف واحد: نوع الملف غير مدعوم؛ اختر PDF أو صورة أو ملف TXT.');
  await pick([{name: 'a.pdf', size: 5, type: 'application/pdf'}, {name: 'b.docx', size: 5, type: ''}]);
  assert.equal(status.textContent, 'استُلم ملف واحد — تجري معالجته · رُفض ملف واحد: نوع الملف غير مدعوم؛ اختر PDF أو صورة أو ملف TXT.');
});

test('txt_upload_hides_reextraction', async () => {
  // F9
  const h = await boot({routes: registerRoutes([summary({id: 7}), summary({id: 8})], {
    7: detail({id: 7, file_available: true, file_kind: 'txt', filename: 'a.txt'}),
    8: detail({id: 8, file_available: true, file_kind: 'pdf', filename: 'a.pdf', source: 'upload'})},
  (method, url) => (url.endsWith('/file') ? {blob: 'x', type: 'application/pdf'} : undefined))});
  await h.openDetail(7);
  assert.equal(h.$('cms-d-reocr').hidden, true, 'TXT: nothing to re-extract');
  await h.openDetail(8);                 // straight from one row to the next: the first one folds
  assert.equal(h.$('cms-d-reocr').hidden, false);
});

test('page_one_citation_moves_pdf_back', async () => {
  // F10
  const cite = (page, key) => ({key, label_ar: key, value: 'قيمة ' + page, verified: true, source: {page, line: 1, quote: 'q'}});
  const d = detail({file_available: true, file_kind: 'pdf', filename: 'a.pdf', source: 'upload', page_count: 3,
    text: '--- Page 1 ---\nقيمة 1\n--- Page 2 ---\nوسط\n--- Page 3 ---\nقيمة 3',
    analysis: {structured: {fields: [cite(3, 'phone'), cite(1, 'email')]}, classification: {}, review_reasons: []}});
  const h = await boot({routes: registerRoutes([summary()], {28: d},
    (method, url) => (url.endsWith('/file') ? {blob: '%PDF', type: 'application/pdf'} : undefined))});
  await h.openDetail(28);
  const frame = () => h.q('#cms-v-file iframe');
  assert.ok(frame(), 'the PDF is shown');
  const citeButton = page => h.qa('#cms-d-info .cms-cite').find(b => b.textContent.includes('صفحة ' + page.toLocaleString('ar-u-nu-arab')));
  citeButton(3).click();
  h.$('cms-v-file-tab').click();
  await h.settle();
  assert.match(frame().src, /#page=3$/);
  citeButton(1).click();
  h.$('cms-v-file-tab').click();
  await h.settle();
  assert.match(frame().src, /#page=1$/, 'back to the page the citation is on');
});

test('counted_phrases_agree', async () => {
  // F11
  const now = Date.now();
  const rows = [
    summary({id: 1, due_at: iso(now + 2.5 * DAY)}), summary({id: 2, due_at: iso(now + 5.5 * DAY)}),
    summary({id: 3, due_at: iso(now - 2.5 * DAY)}), summary({id: 4, due_at: iso(now + 1.5 * DAY)}),
  ];
  const h = await boot({routes: registerRoutes(rows, {}, (method, url) => (url === '/complaints/analytics'
    ? {json: {totals: {all: 4}, model_quality: {reviewed: 4, category_agreement: 1}}} : undefined))});
  await h.view('register');
  const slaText = id => h.q(`#cms-rows tr[data-id="${id}"]`).children[7].textContent;
  assert.equal(slaText(1), 'متبقٍ يومان');
  assert.equal(slaText(2), 'متبقٍ ٥ أيام');
  assert.equal(slaText(3), 'متأخرة يومين');
  assert.equal(slaText(4), 'متبقٍ يوم واحد');
  assert.equal(h.$('cms-register-count').textContent, '٤ شكاوى');
  await h.view('analytics');
  assert.match(h.$('cms-kpis').textContent, /٤ مراجعات/);
  assert.doesNotMatch(h.$('cms-kpis').textContent, /٤ مراجعة/);
});

test('incident_location_field_renders', async () => {
  // (c): the new structuring field shows like the others, labelled even when the server sends no label.
  const d = detail({analysis: {structured: {fields: [
    {key: 'district_or_address', label_ar: 'الحي أو العنوان', value: 'حي النرجس، الرياض', verified: true},
    {key: 'incident_location', value: 'حديقة الردف، الطائف', verified: true, source: {page: 1, line: 1, quote: 'حديقة الردف'}},
  ]}, classification: {}, review_reasons: []}});
  const h = await boot({routes: registerRoutes([summary()], {28: d})});
  await h.openDetail(28);
  const cards = h.qa('#cms-d-info .cms-fcard');
  const card = cards.find(c => c.querySelector('.cms-flabel').textContent === 'موقع المشكلة');
  assert.ok(card, 'labelled «موقع المشكلة»');
  assert.equal(card.querySelector('.cms-fvalue').textContent, 'حديقة الردف، الطائف');
  assert.ok(card.querySelector('.cms-cite'), 'cited like the other fields');
  const labels = cards.map(c => c.querySelector('.cms-flabel').textContent);
  assert.equal(labels.indexOf('موقع المشكلة'), labels.indexOf('الحي أو العنوان') + 1, 'kept in the server\'s order');
});

/* Field review: values the model gave in its own words wait in «تحتاج مراجعة»
   until the reviewer accepts or changes them (POST /items/{id}/fields). */
const REVIEW_TEXT = '--- Page 1 ---\nإلى: صاحب السمو الملكي أمير منطقة الرياض\nالاسم: خالد عبدالله الحربي\nالطلب: شفط المياه وإصلاح الخط';
const NEAR = (() => {
  const phrase = 'شفط المياه وإصلاح الخط', start = REVIEW_TEXT.indexOf(phrase);
  return {page: 1, line: 3, start, end: start + phrase.length, quote: 'الطلب: ' + phrase, approx: true};
})();
const NAME_SOURCE = {page: 1, line: 2, start: REVIEW_TEXT.indexOf('خالد'), end: REVIEW_TEXT.indexOf('خالد') + 19, quote: 'الاسم: خالد عبدالله الحربي'};
const ASKED = 'شفط المياه وإصلاح الخط خلال أسبوع';
function reviewFields(reviews = {}) {
  const fields = [
    {key: 'complainant_name', label_ar: 'اسم مقدم الشكوى', value: 'خالد عبدالله الحربي', verified: true, source: NAME_SOURCE},
    {key: 'requested_action', label_ar: 'الطلب', value: ASKED, verified: false, near_source: NEAR},
    {key: 'against_entity', label_ar: 'الجهة المشتكى عليها', value: 'مقاول البناء', verified: false, near_source: null},
  ];
  return fields.map(f => {
    const r = reviews[f.key];
    if (f.verified || !r) return f.verified ? f : {...f, pending: true};
    return {...f, value: r.action === 'change' ? r.value : f.value,
      review: {action: r.action, reviewer: r.reviewer || '', at: iso(Date.now()), from: f.value}};
  });
}
function reviewDetail(reviews = {}, over = {}) {
  const fields = reviewFields(reviews), pending = fields.filter(f => f.pending).length;
  const reasons = pending ? ['fields_unverified'] : [];
  return detail({text: REVIEW_TEXT, pending_fields: pending, needs_review: pending > 0, review_reasons: reasons,
    analysis: {structured: {fields}, classification: {}, review_reasons: reasons}, ...over});
}
/* A server that records each POST /fields like complaints_store.review_field. */
function reviewServer({fail = null} = {}) {
  const reviews = {}, posts = [];
  const routes = registerRoutes([summary({pending_fields: 2, needs_review: true})], {28: () => reviewDetail(reviews)}, (method, url, body) => {
    if (method !== 'POST' || !url.endsWith('/items/28/fields')) return undefined;
    posts.push(body);
    if (fail) return fail;
    reviews[body.key] = {action: body.action, value: body.value, reviewer: body.reviewer};
    return {json: reviewDetail(reviews)};
  });
  return {routes, posts, reviews};
}
const gridLabels = h => h.block('بيانات مقدم الشكوى والواقعة').querySelectorAll('.cms-fcard').map(c => c.querySelector('.cms-flabel').textContent);
const pendingRows = h => h.block('تحتاج مراجعة').querySelectorAll('li');

test('pending_fields_section_lists_unmatched_values', async () => {
  const server = reviewServer();
  const h = await boot({routes: server.routes});
  await h.openDetail(28);
  const section = h.block('تحتاج مراجعة');
  const blocks = h.$('cms-d-info').children;
  assert.equal(blocks.indexOf(section) + 1, blocks.indexOf(h.block('بيانات مقدم الشكوى والواقعة')), 'right above the fields');
  assert.match(section.textContent, /قيم استخرجها النموذج بصياغته ولم تُطابق نص المستند حرفياً؛ اقبلها إن كانت صحيحة أو عدّلها\./);
  const rows = pendingRows(h);
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /الطلب/);
  assert.match(rows[0].textContent, new RegExp(ASKED));
  assert.ok(rows[0].querySelectorAll('button').some(b => b.textContent === 'موضعها المحتمل في النص ⤴'), 'the approximate place is offered');
  assert.ok(!rows[1].querySelectorAll('button').some(b => b.textContent.startsWith('موضعها')), 'no place when none was found');
  for (const row of rows) { h.buttonIn(row, 'قبول'); h.buttonIn(row, 'تعديل'); }
  assert.deepEqual(gridLabels(h).filter(l => l === 'الطلب' || l === 'الجهة المشتكى عليها'), [], 'pending fields are not repeated in the grid');
  assert.ok(gridLabels(h).includes('اسم مقدم الشكوى'));
  assert.doesNotMatch(h.$('cms-d-info').textContent, /غير موثّق/, 'the old marking is gone');
});

test('pending_field_accept_moves_focus_and_announces', async () => {
  const server = reviewServer();
  const h = await boot({routes: server.routes, local: {'cms.reviewer': 'سالم'}});
  await h.openDetail(28);
  h.press(pendingRows(h)[0], 'قبول');
  await h.clock.advance(50);
  assert.deepEqual(server.posts[0], {key: 'requested_action', action: 'accept', reviewer: 'سالم'}, 'reviewer from the feedback form, no value');
  assert.equal(h.$('cms-d-status').textContent, 'قُبلت القيمة');
  let rows = pendingRows(h);
  assert.equal(rows.length, 1, 'the accepted field left the section');
  let active = h.doc.activeElement;
  assert.equal(active.textContent, 'قبول', 'focus on the next pending row\'s first button');
  assert.ok(rows[0].contains(active));
  const card = h.block('بيانات مقدم الشكوى والواقعة').querySelectorAll('.cms-fcard').find(c => c.querySelector('.cms-flabel').textContent === 'الطلب');
  assert.ok(card, 'the reviewed field is back in the grid');
  assert.equal(card.querySelector('.cms-fvalue').textContent, ASKED);
  assert.match(card.querySelector('.cms-tag').textContent, /^قبِلها المراجع · سالم/);
  assert.ok(card.querySelectorAll('button').some(b => b.textContent.startsWith('موضعها المحتمل')), 'the approximate place stays with it');

  h.press(rows[0], 'قبول');
  await h.clock.advance(50);
  assert.equal(server.posts.length, 2);
  const settled = h.q('#cms-d-info .cms-pending-done');
  assert.ok(settled, 'the heading gives way to a status line');
  assert.ok(h.doc.activeElement === settled, 'focus on the status line');
  assert.equal(h.qa('#cms-d-info .cms-prow').length, 0);
});

test('pending_field_change_enter_saves_escape_cancels', async () => {
  const server = reviewServer();
  const h = await boot({routes: server.routes});
  await h.openDetail(28);
  h.press(pendingRows(h)[0], 'تعديل');
  let input = pendingRows(h)[0].querySelector('input');
  assert.ok(input, '«تعديل» turns the row into an input');
  assert.equal(input.value, ASKED, 'prefilled with the value');
  assert.ok(h.doc.activeElement === input);
  h.buttonIn(pendingRows(h)[0], 'حفظ');
  h.buttonIn(pendingRows(h)[0], 'إلغاء');
  const esc = new FakeEvent('keydown', {key: 'Escape'});
  input.dispatchEvent(esc);
  await h.settle();
  assert.ok(esc.defaultPrevented && esc.stopped, 'Esc stays inside the row: the panel does not fold');
  h.assertExpanded(28);
  assert.ok(!pendingRows(h)[0].querySelector('input'), 'Esc cancels');
  assert.equal(h.doc.activeElement.textContent, 'تعديل', 'focus back on «تعديل»');
  assert.equal(server.posts.length, 0);

  h.press(pendingRows(h)[0], 'تعديل');
  input = pendingRows(h)[0].querySelector('input');
  input.value = '  شفط المياه  ';
  input.dispatchEvent(new FakeEvent('input'));
  input.dispatchEvent(new FakeEvent('keydown', {key: 'Enter'}));
  await h.clock.advance(50);
  assert.deepEqual(server.posts[0], {key: 'requested_action', action: 'change', value: 'شفط المياه'});
  assert.equal(h.$('cms-d-status').textContent, 'حُفظ التعديل');
  const card = h.block('بيانات مقدم الشكوى والواقعة').querySelectorAll('.cms-fcard').find(c => c.querySelector('.cms-flabel').textContent === 'الطلب');
  assert.equal(card.querySelector('.cms-fvalue').textContent, 'شفط المياه');
  const badge = card.querySelector('.cms-tag');
  assert.match(badge.textContent, /^عدّلها المراجع/);
  assert.match(badge.title, new RegExp('القيمة السابقة: ' + ASKED));
  assert.equal(h.doc.activeElement.textContent, 'قبول', 'focus on the next pending row');

  // Saving the value unchanged is accepting it.
  h.press(pendingRows(h)[0], 'تعديل');
  h.press(pendingRows(h)[0], 'حفظ');
  await h.settle();
  assert.deepEqual(server.posts[1], {key: 'against_entity', action: 'accept'});
});

test('pending_field_error_keeps_row', async () => {
  const server = reviewServer({fail: {status: 409, json: {error: 'الشكوى قيد المعالجة حالياً.'}}});
  const h = await boot({routes: server.routes});
  await h.openDetail(28);
  const accept = h.press(pendingRows(h)[0], 'قبول');
  await h.settle();
  assert.equal(server.posts.length, 1);
  assert.equal(pendingRows(h).length, 2, 'nothing changed');
  assert.match(pendingRows(h)[0].textContent, /الشكوى قيد المعالجة حالياً\./);
  assert.equal(accept.disabled, false, 'the buttons work again');
});

test('near_source_link_highlights_the_approximate_span', async () => {
  const h = await boot({routes: reviewServer().routes});
  await h.openDetail(28);
  h.buttonIn(pendingRows(h)[0], 'موضعها المحتمل في النص ⤴').click();
  await h.settle();
  assert.equal(h.$('cms-v-text-tab').getAttribute('aria-selected'), 'true');
  const hit = h.q('#cms-ocr .cms-line.is-hit');
  assert.ok(hit, 'the line is marked');
  assert.equal(hit.querySelector('mark').textContent, 'شفط المياه وإصلاح الخط');
});

test('pending_fields_wait_while_processing', async () => {
  // Reprocessing: no review is possible (the server answers 409), so the values show in the grid.
  const d = reviewDetail({}, {stage: 'structuring'});
  const h = await boot({routes: registerRoutes([summary({stage: 'structuring'})], {28: d})});
  await h.openDetail(28);
  assert.ok(!h.$('cms-d-info').querySelectorAll('section').some(s => s.firstChild && s.firstChild.textContent === 'تحتاج مراجعة'));
  assert.ok(gridLabels(h).includes('الطلب'));
});

test('pending_addressee_keeps_elsewhere_note', async () => {
  const fields = [{key: 'addressed_to', label_ar: 'الجهة الموجّه إليها الخطاب', value: 'هيئة الاتصالات', verified: false, pending: true}];
  const d = detail({pending_fields: 1, needs_review: true, analysis: {structured: {fields, addressed_to_entity: false}, classification: {}, review_reasons: []}});
  const h = await boot({routes: registerRoutes([summary()], {28: d})});
  await h.openDetail(28);
  assert.match(pendingRows(h)[0].textContent, /موجّه إلى جهة أخرى/, 'the note travels with the pending value');
  assert.ok(!gridLabels(h).includes('الجهة الموجّه إليها الخطاب'), 'no empty «غير مذكور» addressee card');
});

test('evidence_shows_every_quote', async () => {
  const text = '--- Page 1 ---\nطفح الصرف الصحي أمام المدرسة\nويضطر الأطفال إلى عبور المياه يومياً';
  const cite = (phrase, over = {}) => ({page: 1, line: text.slice(0, text.indexOf(phrase)).split('\n').length - 1,
    start: text.indexOf(phrase), end: text.indexOf(phrase) + phrase.length, quote: phrase, ...over});
  const d = detail({text, analysis: {structured: {fields: []}, review_reasons: [],
    warnings: ['قيم لم يُعثر عليها حرفياً في النص وتحتاج تحققاً: الطلب.', 'استُبعد 2 من اقتباسات الأدلة لتعذر العثور عليها في النص.',
      'النص أطول مما يتسع له النموذج؛ اعتمد أوله فقط.'],
    classification: {evidence_dropped: 2, evidence: [
      {quote: 'طفح الصرف الصحي أمام المدرسة', source: cite('طفح الصرف الصحي أمام المدرسة'), verified: true},
      {quote: 'الأطفال يعبرون المياه كل يوم', source: cite('ويضطر الأطفال إلى عبور المياه يومياً', {approx: true}), verified: false},
      {quote: 'قول لا موضع له في النص', source: null, verified: false},
      {quote: 'طفح الصرف', source: cite('طفح الصرف')},          // stored before the change: no flag, verified
    ]}}});
  const h = await boot({routes: registerRoutes([summary()], {28: d})});
  await h.openDetail(28);
  const section = h.block('التصنيف والإحالة والأولوية');
  const quotes = section.querySelectorAll('blockquote');
  assert.equal(quotes.length, 4, 'every quote is shown');
  const model = quotes.filter(q => q.classList.contains('is-model'));
  assert.deepEqual(model.map(q => q.textContent), ['الأطفال يعبرون المياه كل يوم', 'قول لا موضع له في النص']);
  const boxOf = q => q.parentNode;
  for (const q of model) assert.ok(boxOf(q).querySelectorAll('.cms-tag').some(t => t.textContent === 'بصياغة النموذج'));
  assert.ok(h.buttonIn(boxOf(model[0]), 'موضعه المحتمل ⤴'));
  assert.equal(boxOf(model[1]).querySelectorAll('button').length, 0, 'no place when none was found');
  for (const q of quotes.filter(q => !q.classList.contains('is-model'))) assert.ok(boxOf(q).querySelector('.cms-cite').textContent.startsWith('مصدر:'));
  assert.doesNotMatch(section.textContent, /استُبعد/);
  const info = h.$('cms-d-info').textContent;
  assert.doesNotMatch(info, /قيم لم يُعثر عليها حرفياً/, 'retired warnings of stored analyses are not shown');
  assert.doesNotMatch(info, /اقتباسات الأدلة/);
  assert.match(info, /النص أطول مما يتسع له النموذج/, 'other warnings stay');
  h.buttonIn(boxOf(model[0]), 'موضعه المحتمل ⤴').click();
  await h.settle();
  assert.equal(h.q('#cms-ocr .cms-line.is-hit mark').textContent, 'ويضطر الأطفال إلى عبور المياه يومياً');
});

test('register_tags_pending_fields', async () => {
  const rows = [summary({id: 1, needs_review: true, pending_fields: 2}), summary({id: 2, needs_review: true, pending_fields: 0}), summary({id: 3})];
  const h = await boot({routes: registerRoutes(rows)});
  await h.view('register');
  const tags = id => h.q(`#cms-rows tr[data-id="${id}"]`).querySelectorAll('.cms-tag').map(t => t.textContent);
  assert.deepEqual(tags(1), ['تحتاج مراجعة', 'حقول للمراجعة: ٢']);
  assert.deepEqual(tags(2), ['تحتاج مراجعة']);
  assert.deepEqual(tags(3), []);
});

test('field_review_event_in_history', async () => {
  const d = reviewDetail({requested_action: {action: 'change', value: 'شفط المياه'}}, {events: [
    {at: iso(Date.now()), kind: 'field_review', detail: {key: 'requested_action', action: 'change', from: ASKED, to: 'شفط المياه'}},
    {at: iso(Date.now() - 1000), kind: 'field_review', detail: JSON.stringify({key: 'against_entity', action: 'accept', from: 'مقاول البناء', to: 'مقاول البناء'})},
  ]});
  const h = await boot({routes: registerRoutes([summary()], {28: d})});
  await h.openDetail(28);
  const history = h.block('سجل الشكوى').textContent;
  assert.doesNotMatch(history, /field_review/);
  assert.match(history, /مراجعة حقل/);
  assert.match(history, new RegExp(`الطلب: من «${ASKED}» إلى «شفط المياه»`));
  assert.match(history, /الجهة المشتكى عليها: قُبلت القيمة «مقاول البناء»/);
});

/* The detail opens in place: an accordion row under the register row, one
   complaint at a time, kept through every reload of the register. */
const THREE = [summary({id: 1, subject: 'الأولى'}), summary({id: 2, subject: 'الثانية'}), summary({id: 3, subject: 'الثالثة'})];
const threeDetails = () => ({1: detail(THREE[0]), 2: detail(THREE[1]), 3: detail(THREE[2])});

test('accordion_row_toggles_in_place', async () => {
  const h = await boot({routes: registerRoutes(THREE, threeDetails())});
  await h.view('register');
  assert.equal(h.qa('dialog').length, 0, 'no detail dialog left in the markup');
  const row = h.row(2), toggle = h.toggle(2);
  assert.equal(toggle.localName, 'button', 'the trigger is a real button');
  assert.ok(toggle.parentNode === row.children[0], 'in the first cell');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-controls'), 'cms-detail');
  assert.equal(toggle.textContent, 'CMP-2026-000002', 'the reference is the visible label');
  assert.equal(toggle.getAttribute('aria-label'), 'CMP-2026-000002 — تفاصيل الشكوى', 'named with the visible text first');
  assert.ok(toggle.querySelector('svg.cms-chev'), 'a chevron shows the state');
  assert.equal(row.tabIndex, -1, 'one tab stop per row: the button, not the row');
  h.assertCollapsed();

  toggle.click();
  await h.settle();
  h.assertExpanded(2);
  const holder = h.panelRow(), panel = h.$('cms-detail');
  assert.equal(holder.children.length, 1);
  assert.equal(holder.children[0].colSpan, 9, 'the panel row spans every column');
  assert.ok(holder.children[0].children[0].classList.contains('cms-panel-frame'));
  assert.equal(panel.getAttribute('role'), 'region');
  assert.equal(panel.getAttribute('aria-labelledby'), 'cms-d-title');
  assert.equal(h.$('cms-d-title').textContent, 'الثانية', 'labelled by the complaint\'s subject');
  assert.equal(h.$('cms-d-title').localName, 'h3');
  assert.ok(h.doc.activeElement === h.$('cms-d-title'), 'focus moves to the panel heading');
  assert.match(h.$('cms-d-info').textContent, /تقييم المراجع/, 'the detail is inside the panel');
  assert.ok(h.block('تقييم المراجع').firstChild.localName === 'h4', 'blocks sit under the panel heading');

  row.children[3].click();                    // anywhere on the row folds it again
  await h.settle();
  h.assertCollapsed();
  assert.ok(!row.classList.contains('is-expanded'));
  assert.ok(h.doc.activeElement === toggle, 'focus back on the row\'s trigger');
  assert.equal(h.$('cms-d-info').childNodes.length, 0, 'the folded panel is emptied');
});

test('one_complaint_expanded_at_a_time', async () => {
  const h = await boot({routes: registerRoutes(THREE, threeDetails())});
  await h.openDetail(1);
  h.toggle(3).click();
  await h.settle();
  h.assertExpanded(3);
  assert.equal(h.toggle(1).getAttribute('aria-expanded'), 'false', 'the first one folded');
  assert.ok(!h.row(1).classList.contains('is-expanded'));
  assert.equal(h.$('cms-d-title').textContent, 'الثالثة');
  assert.ok(h.doc.activeElement === h.$('cms-d-title'));
  assert.equal(h.count('/complaints/items/3'), 1);
});

test('register_refresh_keeps_open_panel', async () => {
  let rows = THREE;
  const h = await boot({routes: registerRoutes([], threeDetails(), (method, url) => {
    if (method === 'GET' && url.split('?')[0] === '/complaints/items') return {json: {items: rows, total: rows.length}};
    return undefined;
  })});
  await h.openDetail(2);
  const tbody = h.$('cms-rows'), holder = h.panelRow();
  // Drafts, an open editor's worth of state, and the viewer's scroll position.
  h.block('حالة المتابعة').querySelector('input').value = 'ملاحظة لم تُحفظ';
  h.change(h.block('تقييم المراجع').querySelector('select[name="priority"]'), 'low');
  h.$('cms-v-text').scrollTop = 120;
  const info = h.$('cms-d-info').firstChild;
  // The panel's row must never leave the table: a moved <iframe> reloads its document.
  let moved = false;
  const detach = tbody.detach.bind(tbody), replace = tbody.replaceChildren.bind(tbody);
  tbody.detach = node => { if (node === holder) moved = true; return detach(node); };
  tbody.replaceChildren = (...items) => { if (tbody.childNodes.includes(holder)) moved = true; return replace(...items); };
  const loads = h.count('/complaints/items');

  rows = [THREE[1], THREE[0], THREE[2]];           // «آخر تحديث» brings #2 to the top
  h.change(h.$('cms-f-sort'), 'updated:desc');
  await h.settle();
  assert.ok(h.count('/complaints/items') > loads, 'the register reloaded');
  assert.ok(!moved, 'the panel row was not moved');
  assert.ok(h.panelRow() === holder && holder.isConnected);
  assert.deepEqual(tbody.children.map(r => r.dataset.id || 'panel'), ['2', 'panel', '1', '3'], 'the rows are laid around it');
  h.assertExpanded(2, 'after a reload');
  assert.equal(h.block('حالة المتابعة').querySelector('input').value, 'ملاحظة لم تُحفظ');
  assert.equal(h.block('تقييم المراجع').querySelector('select[name="priority"]').value, 'low');
  assert.equal(h.$('cms-v-text').scrollTop, 120, 'the viewer keeps its scroll');
  assert.ok(h.$('cms-d-info').firstChild === info, 'the panel content was not rebuilt');
  assert.equal(h.count('/complaints/items/2'), 1, 'the detail was not fetched again');
  assert.ok(h.$('cms-d-note').hidden);

  rows = [THREE[0], THREE[2]];                      // the filters no longer match it
  h.$('cms-filters').dispatchEvent(new FakeEvent('submit'));
  await h.settle();
  assert.ok(!moved);
  assert.ok(tbody.firstChild === holder, 'kept open, on top of the list');
  assert.ok(holder.classList.contains('is-detached'));
  assert.equal(h.$('cms-d-note').hidden, false);
  assert.equal(h.$('cms-d-note').textContent, 'خارج نتائج التصفية الحالية');
  assert.equal(h.block('حالة المتابعة').querySelector('input').value, 'ملاحظة لم تُحفظ');
  h.assertExpanded(2, 'detached');

  rows = [];                                        // nothing matches: the table stays for the panel
  h.$('cms-filters').dispatchEvent(new FakeEvent('submit'));
  await h.settle();
  assert.equal(h.q('.cms-table-wrap').hidden, false, 'the open panel keeps the table shown');
  assert.equal(h.$('cms-register-empty').hidden, false);
  assert.ok(h.panelRow() === holder);

  rows = THREE;                                     // back in the list: under its row again
  h.$('cms-filters').dispatchEvent(new FakeEvent('submit'));
  await h.settle();
  assert.ok(!moved);
  h.assertExpanded(2, 'back in the list');
  assert.ok(h.$('cms-d-note').hidden);
  assert.ok(!holder.classList.contains('is-detached'));

  await h.collapse();
  assert.ok(h.doc.activeElement === h.toggle(2), 'focus on the trigger of the row as re-rendered');
});

test('register_reload_keeps_focus_on_the_trigger', async () => {
  const h = await boot({routes: registerRoutes(THREE, threeDetails())});
  await h.view('register');
  const old = h.toggle(3);
  old.focus();
  h.$('cms-filters').dispatchEvent(new FakeEvent('submit'));
  await h.settle();
  assert.ok(!old.isConnected, 'the rows were rebuilt');
  assert.ok(h.doc.activeElement === h.toggle(3), 'focus follows the row across the reload');
});

test('saving_reloads_the_register_without_folding', async () => {
  let current = detail(THREE[1]);
  const h = await boot({routes: registerRoutes(THREE, {...threeDetails(), 2: () => current}, (method, url, body) => {
    if (method === 'POST' && url.endsWith('/status')) { current = {...current, status: body.status}; return {json: current}; }
    return undefined;
  })});
  await h.openDetail(2);
  const holder = h.panelRow(), loads = h.count('/complaints/items');
  const status = h.block('حالة المتابعة');
  h.change(status.querySelector('select[name="status"]'), 'in_review');
  h.press(status, 'تحديث الحالة');
  await h.clock.advance(50);
  assert.ok(h.count('/complaints/items') > loads, 'the register follows the change');
  assert.ok(h.panelRow() === holder);
  h.assertExpanded(2, 'after a save');
  assert.equal(h.$('cms-d-status').textContent, 'حُدّثت الحالة: قيد الدراسة', 'the panel\'s live region speaks');
});

test('deep_link_from_intake_expands_in_register', async () => {
  const h = await boot({session: [{id: 2, duplicate: false, filename: 'الثانية.txt'}], routes: registerRoutes(THREE, threeDetails())});
  assert.equal(h.$('cms-intake').hidden, false, 'starts on the intake view');
  const job = h.q('#cms-intake-list [data-key="i2"]');
  assert.ok(job, 'the session job is listed');
  h.press(job, 'عرض التفاصيل');
  await h.settle();
  assert.equal(h.$('cms-register').hidden, false, 'switched to the register');
  assert.equal(h.$('cms-tab-register').getAttribute('aria-selected'), 'true');
  h.assertExpanded(2);
  assert.ok(h.doc.activeElement === h.$('cms-d-title'));
  assert.ok(!h.calls.some(c => c.url.includes('q=')), 'the row was in the list: no search');
});

test('ref_chips_expand_in_register_searching_by_ref', async () => {
  const nine = summary({id: 9, subject: 'التاسعة'});
  const h = await boot({routes: registerRoutes(THREE, {...threeDetails(), 9: detail(nine)}, (method, url) => {
    const pathOnly = url.split('?')[0];
    if (method === 'GET' && pathOnly === '/complaints/items' && url.includes('q=CMP-2026-000009')) return {json: {items: [nine], total: 1}};
    if (pathOnly === '/complaints/analytics') {
      return {json: {totals: {all: 4}, model_quality: {reviewed: 1, priority_agreement: 0,
        top_corrections: [{field: 'priority', from: 'low', to: 'high', count: 1, refs: ['CMP-2026-000003']}]}}};
    }
    if (method === 'POST' && pathOnly === '/complaints/insights') {
      return {json: {insights: {headline: 'عنوان', insights: [{title: 'رؤية', detail: 'تفصيل', refs: ['CMP-2026-000009']}]}}};
    }
    return undefined;
  })});
  // A top-correction chip whose complaint is in the list: straight to its row.
  await h.view('analytics');
  const chip = h.qa('#cms-charts .cms-refchip').find(c => c.textContent === 'CMP-2026-000003');
  assert.ok(chip, 'the top-correction chip is shown');
  chip.click();
  await h.settle();
  assert.equal(h.$('cms-register').hidden, false);
  h.assertExpanded(3);

  // An insights chip whose complaint the filters hide: searched by its reference first.
  await h.view('analytics');
  h.change(h.$('cms-f-priority'), 'low');
  await h.settle();
  h.$('cms-insights-run').click();
  await h.settle();
  h.qa('#cms-insights-body .cms-refchip').find(c => c.textContent === 'CMP-2026-000009').click();
  await h.settle();
  const search = h.calls.filter(c => c.method === 'GET' && c.url.split('?')[0] === '/complaints/items' && c.url.includes('q=CMP-2026-000009'));
  assert.equal(search.length, 1, 'the register was searched by the reference');
  assert.doesNotMatch(search[0].url, /priority=/, 'with the other filters cleared');
  assert.equal(h.$('cms-f-q').value, 'CMP-2026-000009');
  assert.equal(h.$('cms-f-priority').value, '');
  h.assertExpanded(9);
  assert.equal(h.$('cms-d-title').textContent, 'التاسعة');
});

test('collapse_button_and_escape_return_focus', async () => {
  const h = await boot({routes: registerRoutes(THREE, threeDetails())});
  await h.openDetail(1);
  const fold = h.$('cms-d-collapse');
  assert.equal(fold.getAttribute('aria-controls'), 'cms-detail');
  assert.match(fold.getAttribute('aria-label'), /^طيّ/);
  await h.collapse();
  assert.ok(h.doc.activeElement === h.toggle(1), '«طيّ» returns focus to the trigger');

  await h.openDetail(1);
  const esc = new FakeEvent('keydown', {key: 'Escape'});
  h.$('cms-d-json').focus();
  h.$('cms-d-json').dispatchEvent(esc);
  await h.settle();
  assert.ok(esc.defaultPrevented);
  h.assertCollapsed();
  assert.ok(h.doc.activeElement === h.toggle(1), 'Esc returns focus to the trigger');

  await h.openDetail(1);
  const note = h.block('تقييم المراجع').querySelector('textarea');
  note.value = 'نص';
  const typing = new FakeEvent('keydown', {key: 'Escape'});
  note.dispatchEvent(typing);
  await h.settle();
  assert.ok(!typing.defaultPrevented, 'Esc in a text box is left alone');
  h.assertExpanded(1, 'Esc typed in a field does not fold the panel');
  assert.equal(note.value, 'نص');
});

test('delete_folds_removes_row_and_focuses_next', async () => {
  let rows = THREE;
  const h = await boot({routes: registerRoutes([], threeDetails(), (method, url) => {
    const pathOnly = url.split('?')[0];
    if (method === 'GET' && pathOnly === '/complaints/items') return {json: {items: rows, total: rows.length}};
    if (method === 'DELETE' && pathOnly === '/complaints/items/2') { rows = rows.filter(r => r.id !== 2); return {json: {ok: true}}; }
    if (method === 'DELETE' && pathOnly === '/complaints/items/3') { rows = rows.filter(r => r.id !== 3); return {json: {ok: true}}; }
    return undefined;
  })});
  await h.openDetail(2);
  h.press(h.q('.cms-d-actions'), 'حذف');
  await h.clock.advance(50);
  assert.equal(h.confirms.length, 1, 'asked first');
  assert.ok(h.calls.some(c => c.method === 'DELETE' && c.url === '/complaints/items/2'));
  h.assertCollapsed();
  assert.ok(!h.row(2), 'the row is gone');
  assert.ok(h.doc.activeElement === h.toggle(3), 'focus on the next row');
  const live = h.qa('div.cms-sr-only[role="status"]').find(n => n.parentNode === h.$('complaints-workspace'));
  assert.equal(live.textContent, 'حُذفت الشكوى CMP-2026-000002');
  assert.equal(h.$('cms-register-count').textContent, 'شكويان');

  await h.openDetail(3);                     // the last row: focus falls back to the one before it
  h.press(h.q('.cms-d-actions'), 'حذف');
  await h.clock.advance(50);
  h.assertCollapsed();
  assert.ok(h.doc.activeElement === h.toggle(1), 'focus on the previous row after the last one');
});

test('panel_releases_resources_when_folded', async () => {
  const pdf = detail({id: 2, file_available: true, file_kind: 'pdf', filename: 'a.pdf', source: 'upload', page_count: 1});
  const h = await boot({routes: registerRoutes(THREE, {...threeDetails(), 2: pdf},
    (method, url) => (url.endsWith('/file') ? {blob: '%PDF', type: 'application/pdf'} : undefined))});
  await h.openDetail(2);
  const frame = h.q('#cms-v-file iframe');
  assert.ok(frame && frame.src.startsWith('blob:test/'), 'the PDF shows from a blob: URL');
  const url = frame.src;
  // Sized to the register's visible width (here by the script: no container units in this DOM).
  const wrap = h.q('.cms-table-wrap');
  const watching = () => h.observers.filter(o => o.targets.includes(wrap));
  const [observer] = watching();
  assert.ok(observer, 'the panel watches the register width');
  Object.defineProperty(wrap, 'clientWidth', {value: 640, configurable: true});
  observer.fire();
  assert.equal(h.q('#cms-rows .cms-panel-frame').style['--cms-panel-w'], '640px');

  await h.collapse();
  assert.deepEqual(h.revoked, [url], 'the blob: URL is revoked');
  assert.ok(observer.disconnected, 'the observer is dropped');
  assert.equal(h.$('cms-v-file').childNodes.length, 0);
  await h.clock.advance(1000);
  assert.equal(h.clock.pending(), 0, 'no timer left behind');

  await h.openDetail(2);
  h.toggle(1).click();                       // replaced by another complaint: same clean-up
  await h.settle();
  assert.equal(h.revoked.length, 2);
  assert.equal(watching().length, 1, 'one observer for the one open panel');
});

test('reprocess_in_panel_keeps_focus', async () => {
  // The pressed button is disabled at once, which drops the focus to <body>: the panel takes it back.
  let current = detail(THREE[1]), fail = false;
  const h = await boot({routes: registerRoutes(THREE, {...threeDetails(), 2: () => current}, (method, url) => {
    if (method !== 'POST' || url !== '/complaints/items/2/reprocess') return undefined;
    if (fail) return {status: 409, json: {error: 'الشكوى قيد المعالجة.'}};
    current = {...current, stage: 'queued'};
    return {json: {item: {id: 2, ref: current.ref, stage: 'queued'}}};
  })});
  await h.openDetail(2);
  h.press(h.q('.cms-d-actions'), 'إعادة المعالجة');
  await h.settle();
  h.assertExpanded(2);
  assert.ok(h.$('cms-d-reprocess').disabled, 'disabled while the complaint waits in the queue');
  assert.ok(h.doc.activeElement === h.$('cms-d-title'), 'focus on the panel heading, not <body>');

  current = detail(THREE[1]);
  fail = true;
  await h.collapse();
  await h.openDetail(2);
  h.press(h.q('.cms-d-actions'), 'إعادة المعالجة');
  await h.settle();
  assert.equal(h.$('cms-d-error').hidden, false, 'the refusal is shown in the panel');
  assert.ok(!h.$('cms-d-reprocess').disabled);
  assert.ok(h.doc.activeElement === h.$('cms-d-reprocess'), 'focus back on the button after a refusal');
});

/* ---------------------------------------------------------------- runner */
(async () => {
  const json = process.argv.includes('--json');
  const only = process.argv.slice(2).filter(a => !a.startsWith('--'));
  let failed = 0, stray = [];
  process.on('unhandledRejection', exc => { stray.push(exc); });
  for (const t of tests) {
    if (only.length && !only.includes(t.name)) continue;
    stray = [];
    let error = null;
    try {
      await t.fn();
      await settle();
      if (stray.length) throw stray[0];
    } catch (exc) {
      error = exc && exc.stack ? exc.stack : String(exc);
    }
    if (error) failed++;
    if (json) console.log(JSON.stringify({name: t.name, ok: !error, error}));
    else console.log(`${error ? 'FAIL' : 'ok  '} ${t.name}${error ? '\n' + error : ''}`);
  }
  process.exit(failed ? 1 : 0);
})();
