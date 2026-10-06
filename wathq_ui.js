/* "التحقق من البيانات" tab: every Wathq product in one place.
   The catalog (/wathq/catalog) drives everything: the product bar, the
   queries of each product with their prices, and a form built from each
   query's inputs. A lookup runs only when the user presses the button; the
   app's /wathq/query route holds the key, makes the call, masks personal
   data and returns a view. Values are rendered as text nodes (never
   innerHTML) and numbers through the page's appendNumText, so a date or an
   ID keeps its order inside Arabic text. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('verify-workspace');
  if (!root) return;
  const productsBar = $('vf-products'), endpointsBox = $('vf-endpoints'), form = $('vf-form');
  const fieldsBox = $('vf-fields'), lang = $('vf-lang'), langWrap = $('vf-lang-wrap');
  const run = $('vf-run'), cancel = $('vf-cancel'), costNote = $('vf-cost');
  const config = $('vf-config'), status = $('vf-status'), errorBox = $('vf-error');
  const placeholder = $('vf-placeholder'), results = $('vf-results'), title = $('vf-product-title');
  const AR = '٠١٢٣٤٥٦٧٨٩';
  const ar = n => String(n).replace(/[0-9]/g, d => AR[+d]);
  const CONFIRM_FROM = 20;       // SAR: a query this expensive asks twice
  let catalog = null, product = null, endpoint = null;
  let configured = false, busy = false, generation = 0, controller = null, lastEnv = '';
  let cacheSeconds = 0, armed = false, armedAt = 0;
  const CONFIRM_GAP = 700;       // ms: a double-click is not a confirmation
  const panel = $('vf-panel');

  // ---------------------------------------------------------------- helpers
  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null && text !== '') (cls === 'vf-text' ? putPlain : put)(n, text);
    return n;
  }
  function putPlain(el, text) {      // numbers stay left-to-right inside Arabic
    if (typeof appendNumText === 'function') appendNumText(el, String(text));
    else el.append(String(text));
  }
  function put(el, text) {
    text = String(text);
    // A token with no Arabic and no spaces (a masked ID, an e-mail, a URL, a
    // code) is one left-to-right run; anything else is isolated in a <bdi>
    // so an English name keeps its own direction inside Arabic.
    if (text && !/[؀-ۿ\s]/.test(text) && /[•@.:\/A-Za-z0-9]/.test(text)) { el.append(ltr(text)); return; }
    const b = document.createElement('bdi');
    putPlain(b, text);
    el.append(b);
  }
  function ltr(text) {
    const b = document.createElement('bdo'); b.dir = 'ltr'; b.textContent = text; return b;
  }
  function amount(v) {
    if (!/^-?[0-9]+$/.test(v || '')) return v || '';
    return Number(v).toLocaleString('en-US');
  }
  function yesNo(v) { return v === true ? 'نعم' : v === false ? 'لا' : ''; }
  function pct(v) { return v ? ltr(v + '%') : ''; }
  function price(p) {
    if (p == null || p < 0) return 'السعر غير مُعلن';
    if (p === 0) return 'مجاني';
    const n = Number.isInteger(p) ? p : p.toFixed(2);
    return `${ar(n)} ريال`;
  }
  function requests(n) {
    return n === 1 ? 'طلب واحد' : n === 2 ? 'طلبان' : `${ar(n)} طلبات`;
  }
  function fold(raw) {
    return String(raw || '').normalize('NFKC')
      .replace(/[٠-٩]/g, d => '0123456789'['٠١٢٣٤٥٦٧٨٩'.indexOf(d)])
      .replace(/[۰-۹]/g, d => '0123456789'['۰۱۲۳۴۵۶۷۸۹'.indexOf(d)])
      .replace(/[\s\-_.‎‏‪-‮⁦-⁩؜]+/g, '');
  }
  function minutes(sec) {
    const m = Math.round(sec / 60);
    return m === 1 ? 'دقيقة واحدة' : m === 2 ? 'دقيقتين' : `${ar(m)} دقيقة`;
  }
  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString('ar-SA-u-nu-arab-ca-gregory', {dateStyle: 'medium', timeStyle: 'short'});
  }
  function showError(message, code, detail) {
    errorBox.replaceChildren(document.createTextNode(message));
    if (code) { errorBox.append(' (رمز وثق: '); errorBox.append(ltr(code)); errorBox.append(')'); }
    if (detail) errorBox.append(wathqSaid(detail));
    errorBox.hidden = false;
  }
  function wathqSaid(detail) {       // Wathq's own wording for a refusal, as the server relayed it
    const line = node('small', 'vf-said');
    line.append('نص ردّ وثق: ');
    line.append(ltr(detail));
    return line;
  }
  function sentLine(sent, env) {
    const where = env === 'sandbox' ? 'البيئة الاختبارية' : 'بيئة الإنتاج';
    return `متصل بوثق (${where}) · طلبات أُرسلت إلى وثق منذ تشغيل التطبيق: ${ar(sent || 0)}`;
  }
  const DIGIT_KINDS = new Set(['unified_number', 'company_number', 'cr_number_any', 'deed_number',
    'permission_id', 'copy_number', 'drug_id', 'investor_id', 'attorney_code']);
  const HINTS = {
    unified_number: 'الرقم الوطني الموحد: ١٠ أرقام تبدأ بـ ٧٠.',
    company_number: 'الرقم الوطني الموحد (٧٠…) أو رقم السجل التجاري القديم؛ القديم يُحوَّل أولًا بطلب إضافي.',
    cr_number_any: 'رقم السجل التجاري أو الرقم الوطني الموحد: ١٠ أرقام.',
    person_or_entity_id: 'رقم الهوية الوطنية أو الإقامة (١٠ أرقام).',
    investor_id: 'الرقم الموحد للمستثمر (يبدأ بـ ٧٠، من ١٠ إلى ١٢ رقمًا).',
    drug_id: 'رقم تسجيل المستحضر لدى هيئة الغذاء والدواء.',
    deed_number: 'رقم الصك كما هو مطبوع.',
    attorney_code: 'رقم الوكالة كما هو مطبوع.',
  };

  // ---------------------------------------------------------------- status + catalog
  async function loadStatus() {
    try {
      const r = await fetch('/wathq/status', {cache: 'no-store'});
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { configured = false; config.textContent = j.error || 'خدمة التحقق غير متاحة.'; config.dataset.state = 'bad'; }
      else if (!j.configured) {
        configured = false;
        config.textContent = j.reason || 'لم يُضبط مفتاح وثق.';
        config.dataset.state = 'bad';
      } else {
        configured = true;
        lastEnv = j.env;
        cacheSeconds = Number(j.cache_seconds) || 0;
        config.textContent = sentLine(j.sent, j.env);
        config.dataset.state = 'ok';
      }
    } catch (e) {
      configured = false; config.textContent = 'تعذّر الاتصال بالتطبيق.'; config.dataset.state = 'bad';
    }
    refresh();
    sugRefresh();
  }
  async function loadCatalog() {
    if (catalog) return;
    try {
      const r = await fetch('/wathq/catalog', {cache: 'no-store'});
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !Array.isArray(j.products)) { showError(j.error || 'تعذّر تحميل قائمة خدمات وثق.'); return; }
      if (!j.products.length) { showError('قائمة خدمات وثق فارغة.'); return; }
      catalog = j;
      renderProducts();
      selectProduct(catalog.products[0]);
    } catch (e) {
      showError('تعذّر تحميل قائمة خدمات وثق.');
    }
  }

  // ---------------------------------------------------------------- choosing
  function renderProducts() {
    productsBar.replaceChildren();
    catalog.products.forEach((p, i) => {
      const b = node('button', 'vf-product', p.label);
      const n = p.endpoints.length;
      b.append(node('small', 'vf-product-n', n === 1 ? 'استعلام واحد' : n === 2 ? 'استعلامان' : n <= 10 ? ar(n) + ' استعلامات' : ar(n) + ' استعلامًا'));
      b.type = 'button';
      b.id = 'vf-tab-' + p.id;
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-controls', 'vf-panel');
      b.dataset.id = p.id;
      b.setAttribute('aria-selected', 'false');
      b.tabIndex = i === 0 ? 0 : -1;
      b.addEventListener('click', () => selectProduct(p));
      b.addEventListener('keydown', ev => {
        const step = {ArrowDown: 1, ArrowUp: -1, ArrowLeft: 1, ArrowRight: -1}[ev.key];   // a vertical list; RTL: next is to the left
        const list = catalog.products;
        let next;
        if (step) next = list[(list.indexOf(p) + step + list.length) % list.length];
        if (ev.key === 'Home') next = list[0];
        if (ev.key === 'End') next = list[list.length - 1];
        if (next) { ev.preventDefault(); selectProduct(next); productsBar.querySelector(`[data-id="${next.id}"]`).focus(); }
      });
      productsBar.append(b);
    });
  }
  function selectProduct(p) {
    if (p === product) return;            // re-clicking the open tab keeps the form
    product = p;
    for (const b of productsBar.children) {
      const on = b.dataset.id === p.id;
      b.setAttribute('aria-selected', String(on));
      b.tabIndex = on ? 0 : -1;
    }
    panel.setAttribute('aria-labelledby', 'vf-tab-' + p.id);
    title.textContent = p.label;
    renderEndpoints();
    if (!p.endpoints.length) {
      endpoint = null;
      fieldsBox.replaceChildren(node('p', 'vf-note', 'لا توجد خدمات في هذا المنتج.'));
      refresh();
      return;
    }
    const first = p.endpoints.find(e => !e.lookup && e.available) || p.endpoints.find(e => e.available) || p.endpoints[0];
    selectEndpoint(first);
  }
  function endpointOption(e) {
    const label = node('label', 'vf-endpoint');
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'vf-endpoint'; radio.value = e.id;
    radio.disabled = !e.available;
    radio.addEventListener('change', () => selectEndpoint(e));
    label.append(radio);
    const text = node('span', 'vf-endpoint-text');
    text.append(node('span', 'vf-endpoint-label', e.label));
    if (e.description) text.append(node('small', 'vf-endpoint-desc', e.description));
    if (!e.available) text.append(node('small', 'vf-endpoint-desc', 'غير متاحة في البيئة الاختبارية'));
    label.append(text);
    label.append(node('span', 'vf-price' + (e.price === 0 ? ' is-free' : e.price >= CONFIRM_FROM ? ' is-high' : ''), price(e.price)));
    return label;
  }
  function renderEndpoints() {
    endpointsBox.replaceChildren();
    const data = product.endpoints.filter(e => !e.lookup), lists = product.endpoints.filter(e => e.lookup);
    data.forEach(e => endpointsBox.append(endpointOption(e)));
    if (lists.length) {
      const det = node('details', 'vf-lookups');
      det.append(node('summary', '', `قوائم مرجعية (${ar(lists.length)})`));
      const inner = node('div', 'vf-lookups-list');
      lists.forEach(e => inner.append(endpointOption(e)));
      det.append(inner);
      endpointsBox.append(det);
    }
  }
  function selectEndpoint(e) {
    endpoint = e;
    disarm();
    const radio = endpointsBox.querySelector(`input[value="${CSS.escape(e.id)}"]`);
    if (radio) {
      radio.checked = true;
      const det = radio.closest('details');
      if (det) det.open = true;
    }
    renderFields();
    errorBox.hidden = true;
    clearResult();
    refresh();
  }
  function clearResult() {
    results.replaceChildren();
    results.hidden = true;
    placeholder.hidden = false;
    status.textContent = '';
  }
  function renderFields() {
    fieldsBox.replaceChildren();
    for (const inp of endpoint.inputs) {
      const id = 'vf-in-' + inp.name;
      const wrap = node('div', 'vf-field');
      const label = node('label', '', inp.label);
      label.htmlFor = id;
      label.dataset.base = inp.label;
      wrap.append(label);
      let control;
      if (inp.choices && inp.choices.length) {
        control = document.createElement('select');
        const blank = document.createElement('option');
        blank.value = ''; blank.textContent = 'اختر…';
        control.append(blank);
        for (const c of inp.choices) {
          const o = document.createElement('option'); o.value = c.value; o.textContent = c.label; control.append(o);
        }
        if (inp.choices.length === 1) control.value = inp.choices[0].value;
      } else if (inp.kind === 'boolean') {
        control = document.createElement('input');
        control.type = 'checkbox';
      } else {
        control = document.createElement('input');
        control.type = 'text';
        control.dir = 'ltr';
        control.autocomplete = 'off';
        control.spellcheck = false;
        control.maxLength = 40;
        if (DIGIT_KINDS.has(inp.kind)) control.inputMode = 'numeric';
      }
      control.id = id;
      control.name = inp.name;
      control.dataset.kind = inp.kind;
      control.addEventListener(control.type === 'checkbox' || control.tagName === 'SELECT' ? 'change' : 'input', () => {
        errorBox.hidden = true; disarm(); markRequired(); refresh();
      });
      wrap.append(control);
      const typed = endpoint.inputs.some(i => i.kind === 'id_type');
      let hint = inp.hint || HINTS[inp.kind] || '';
      if (inp.kind === 'person_or_entity_id' && typed) hint = 'رقم الهوية أو الإقامة أو المعرّف حسب النوع المختار (أرقام فقط).';
      const conditional = Object.entries(inp.required_if || {});
      if (conditional.length) {
        const [other, values] = conditional[0];
        const src = endpoint.inputs.find(i => i.name === other);
        const names = values.map(v => ((src && src.choices || []).find(c => c.value === v) || {label: v}).label);
        hint = (hint ? hint + ' ' : '') + `مطلوب عند اختيار: ${names.join('، ')}.`;
      }
      if (hint) {
        const h = node('small', 'vf-hint', hint + (inp.personal ? ' لا يُحفظ ولا يُعرض في النتيجة.' : ''));
        h.id = id + '-hint';
        control.setAttribute('aria-describedby', h.id);
        wrap.append(h);
      }
      fieldsBox.append(wrap);
    }
    if (endpoint.one_of && endpoint.one_of.length) {
      const names = endpoint.inputs.filter(i => endpoint.one_of.includes(i.name)).map(i => `«${i.label}»`);
      fieldsBox.append(node('p', 'vf-note', 'أدخل واحدًا على الأقل من: ' + names.join('، ') + '.'));
    }
    if (!endpoint.inputs.length) fieldsBox.append(node('p', 'vf-note', 'لا تحتاج هذه القائمة إلى مدخلات.'));
    langWrap.hidden = !endpoint.language;
    markRequired();
  }
  function isRequired(inp, v) {
    if (inp.required) return true;
    return Object.entries(inp.required_if || {}).some(([other, values]) => values.includes(v[other]));
  }
  function markRequired() {                // "(اختياري)" follows the current choices
    if (!endpoint) return;
    const v = values();
    for (const inp of endpoint.inputs) {
      const label = fieldsBox.querySelector(`label[for="vf-in-${CSS.escape(inp.name)}"]`);
      if (!label) continue;
      const optional = !isRequired(inp, v) && !(endpoint.one_of || []).includes(inp.name);
      label.textContent = label.dataset.base + (optional ? ' (اختياري)' : '');
    }
  }
  function values() {
    const out = {};
    for (const inp of endpoint.inputs) {
      const c = $('vf-in-' + inp.name);
      if (!c) continue;
      if (c.type === 'checkbox') { if (c.checked) out[inp.name] = 'true'; continue; }
      const v = DIGIT_KINDS.has(inp.kind) || inp.kind === 'person_or_entity_id' ? fold(c.value) : c.value.trim();
      if (v) out[inp.name] = v;
    }
    return out;
  }
  function ready() {
    if (!endpoint || !endpoint.available) return false;
    const v = values();
    if (endpoint.inputs.some(i => isRequired(i, v) && !v[i.name])) return false;
    if (endpoint.one_of && endpoint.one_of.length && !endpoint.one_of.some(n => v[n])) return false;
    return true;
  }
  function willConvert() {
    if (!endpoint || !endpoint.converts) return false;
    const inp = endpoint.inputs.find(i => i.kind === 'company_number');
    const v = inp ? values()[inp.name] || '' : '';
    return /^[1-6][0-9]{9}$/.test(v);
  }
  function cost() {
    if (!endpoint) return 0;
    return (endpoint.price > 0 ? endpoint.price : 0) + (willConvert() ? 2 : 0);
  }
  function describeCost() {
    if (!endpoint) { costNote.textContent = ''; return; }
    if (!endpoint.available) {
      costNote.textContent = 'هذه الخدمة غير متاحة في البيئة الاختبارية لوثق؛ عيّن WATHQ_ENV=production لاستخدامها.';
      return;
    }
    let text = endpoint.price < 0 ? 'سعر هذه الخدمة غير مُعلن في قائمة أسعار وثق.'
      : endpoint.price === 0 ? 'هذه الخدمة مجانية في قائمة أسعار وثق.'
      : `يُخصم ${price(endpoint.price)} من رصيد باقتك عند كل استعلام ناجح.`;
    if (willConvert()) {
      text += ' ورقم السجل القديم يحتاج طلب تحويل إضافيًا (٢ ريال).';
      if (endpoint.legacy_ok) text += ' وإن كان السجل مشطوبًا فقد يلزم طلب ثالث بالرقم القديم.';
    }
    text += cacheSeconds > 0
      ? ` تبقى النتيجة في ذاكرة التطبيق ${endpoint.lookup ? 'يومًا كاملًا' : minutes(cacheSeconds)} لنفس المدخلات، فلا يتكرر الخصم خلالها.`
      : ' التخزين المؤقت معطّل، فكل إعادة استعلام تُخصم من جديد.';
    costNote.textContent = text;
  }
  function disarm() {
    if (armed) status.textContent = '';
    armed = false;
    run.classList.remove('is-confirm');
  }
  function refresh() {
    run.disabled = busy || !configured || !ready();
    run.textContent = !endpoint ? 'استعلام من وثق'
      : armed ? `تأكيد: سيُخصم ${price(cost())}` : `استعلام من وثق · ${price(cost())}`;
    describeCost();
  }
  function setBusy(value) {
    const inForm = form.contains(document.activeElement) || document.activeElement === document.body;
    if (value && inForm) { cancel.hidden = false; cancel.focus(); }
    busy = value;
    for (const el of fieldsBox.querySelectorAll('input, select')) el.disabled = value;
    for (const el of endpointsBox.querySelectorAll('input')) el.disabled = value || !endpointFor(el.value).available;
    for (const b of productsBar.children) b.disabled = value;
    lang.disabled = value;
    refresh();
    const hadFocus = document.activeElement === cancel || document.activeElement === document.body;
    cancel.hidden = !value;
    if (!value && hadFocus) {
      const first = fieldsBox.querySelector('input, select');
      (!run.disabled ? run : first || productsBar.querySelector('[aria-selected="true"]')).focus();
    }
  }
  function endpointFor(id) {
    for (const p of catalog.products) for (const e of p.endpoints) if (e.id === id) return e;
    return {available: false};
  }

  // ---------------------------------------------------------------- generic view
  function count(n, shown) {               // "300 of 1,000" when the server cut a list
    return n.truncated && n.total ? `يُعرض ${ar(shown)} من ${ar(n.total)}` : ar(shown);
  }
  function renderNodes(nodes, into, depth) {
    let pairs = null;
    for (const n of nodes) {
      if (n.type === 'field') {
        if (!pairs) { pairs = node('dl', 'vf-grid'); into.append(pairs); }
        const row = node('div', 'vf-pair');
        row.append(node('dt', '', n.label));
        const dd = node('dd'); put(dd, n.value);
        row.append(dd);
        pairs.append(row);
        continue;
      }
      pairs = null;
      if (n.type === 'group') {
        const sec = node('section', 'vf-group');
        sec.append(node(depth < 2 ? 'h4' : 'h5', 'vf-group-title', n.label));
        renderNodes(n.children || [], sec, depth + 1);
        into.append(sec);
      } else if (n.type === 'list') {
        const sec = node('section', 'vf-group');
        sec.append(node(depth < 2 ? 'h4' : 'h5', 'vf-group-title', `${n.label} (${count(n, n.items.length)})`));
        const ul = node('ul', 'vf-list');
        for (const it of n.items) { const li = node('li'); put(li, it); ul.append(li); }
        sec.append(ul);
        into.append(sec);
      } else if (n.type === 'table') {
        const sec = node('section', 'vf-group');
        sec.append(node(depth < 2 ? 'h4' : 'h5', 'vf-group-title', `${n.label} (${count(n, n.rows.length)})`));
        const wrap = node('div', 'vf-table-wrap'), t = node('table', 'vf-table');
        const head = node('tr');
        n.columns.forEach(c => { const th = node('th', '', c); th.scope = 'col'; head.append(th); });
        const thead = node('thead'); thead.append(head); t.append(thead);
        const body = node('tbody');
        for (const r of n.rows) {
          const tr = node('tr');
          for (const cell of r) { const td = node('td'); if (cell) put(td, cell); tr.append(td); }
          body.append(tr);
        }
        t.append(body); wrap.append(t); sec.append(wrap);
        into.append(sec);
      } else if (n.type === 'cards') {
        const sec = node('section', 'vf-group');
        sec.append(node(depth < 2 ? 'h4' : 'h5', 'vf-group-title', `${n.label} (${count(n, n.items.length)})`));
        const many = n.items.length > 5;
        const holder = many ? node('details', 'vf-cards') : sec;
        if (many) { holder.append(node('summary', '', `عرض ${ar(n.items.length)} عنصرًا`)); sec.append(holder); }
        for (const item of n.items) {
          const c = node('div', 'vf-subcard');
          c.append(node('h5', 'vf-group-title', item.label));
          renderNodes(item.children || [], c, depth + 2);
          holder.append(c);
        }
        into.append(sec);
      }
    }
  }
  function header(data) {
    const head = node('section', 'vf-card vf-summary');
    const top = node('div', 'vf-summary-hd');
    top.append(node('h3', '', data.label || 'النتيجة'));
    top.append(node('span', 'vf-chip', data.product || ''));
    head.append(top);
    const meta = node('p', 'vf-meta');
    meta.append(`المصدر: وثق${data.env === 'sandbox' ? ' · بيئة اختبارية' : ''} · ${when(data.fetched_at)} · `);
    meta.append(!data.calls_used ? 'من الذاكرة المؤقتة، لم يُرسل أي طلب إلى وثق'
      : `أُرسل ${requests(data.calls_used)} إلى وثق` + (data.cached ? ' (والنتيجة من الذاكرة المؤقتة)' : ''));
    head.append(meta);
    if (data.query && data.query.legacy_used) {
      head.append(node('p', 'vf-meta', 'لم يُعثر على رقم وطني موحد لهذا السجل، فاستُعلم برقم السجل القديم (سجل مشطوب غالبًا).'));
    }
    if (data.query && data.query.converted && data.query.national_number) {
      const conv = node('p', 'vf-meta');
      conv.append('حُوِّل رقم السجل التجاري إلى الرقم الوطني الموحد ');
      conv.append(ltr(data.query.national_number)); conv.append('.');
      head.append(conv);
    }
    return head;
  }
  function renderGeneric(data) {
    const head = header(data);
    const view = data.view || [];
    if (!view.length) head.append(node('p', 'vf-note', 'لم تُرجع وثق بيانات لهذا الطلب.'));
    else renderNodes(view, head, 0);
    return [head];
  }

  // ---------------------------------------------------------------- contract view
  function card(t, cls) {
    const c = node('section', 'vf-card' + (cls ? ' ' + cls : ''));
    if (t) c.append(node('h3', '', t));
    return c;
  }
  function grid(pairs) {
    const dl = node('dl', 'vf-grid');
    for (const [label, value] of pairs) {
      if (value == null || value === '' || (Array.isArray(value) && !value.length)) continue;
      const row = node('div', 'vf-pair');
      row.append(node('dt', '', label));
      const dd = node('dd');
      if (value instanceof Node) dd.append(value); else put(dd, Array.isArray(value) ? value.join('، ') : value);
      row.append(dd);
      dl.append(row);
    }
    return dl;
  }
  function table(headers, rows) {
    const wrap = node('div', 'vf-table-wrap');
    const t = node('table', 'vf-table');
    const head = node('tr');
    headers.forEach(h => { const th = node('th', '', h); th.scope = 'col'; head.append(th); });
    const thead = node('thead'); thead.append(head); t.append(thead);
    const body = node('tbody');
    for (const cells of rows) {
      const tr = node('tr');
      for (const cell of cells) {
        const td = node('td');
        if (cell instanceof Node) td.append(cell); else if (cell) put(td, cell);
        tr.append(td);
      }
      body.append(tr);
    }
    t.append(body); wrap.append(t);
    return wrap;
  }
  function idCell(masked, type) {
    const span = node('span', 'vf-id');
    if (masked) span.append(ltr(masked));
    if (type) span.append(node('small', '', type));
    return span;
  }
  function renderContract(data) {
    const e = data.entity || {};
    const out = [];
    const head = header(Object.assign({}, data, {label: e.name || 'منشأة بلا اسم في ردّ وثق'}));
    const chips = head.querySelector('.vf-summary-hd');
    for (const chip of [e.legal_form, e.entity_type].filter(Boolean)) chips.append(node('span', 'vf-chip', chip));
    const fy = data.fiscal_year || {};
    head.append(grid([
      ['الرقم الوطني الموحد', e.national_number],
      ['رقم السجل التجاري', e.cr_number],
      ['صفة الشركة', e.characters],
      ['مدة الشركة', e.duration],
      ['المقر الرئيس', e.headquarters],
      ['رقم نسخة العقد', (data.contract || {}).copy_number],
      ['تاريخ العقد', (data.contract || {}).date],
      ['نهاية السنة المالية', fy.end ? [fy.end, fy.calendar].filter(Boolean).join(' ') + (fy.first ? ' (السنة الأولى)' : '') : ''],
      ['قائمة على ترخيص', yesNo(e.license_based)],
      ['جهة الترخيص', e.license_issuer],
      ['وسائل الإبلاغ', data.notification_channels],
    ]));
    out.push(head);

    const cap = data.capital || {};
    if (cap.contribution || cap.stock) {
      const c = card('رأس المال');
      const cur = cap.currency ? ' ' + cap.currency : '';
      if (cap.contribution) {
        const k = cap.contribution;
        c.append(node('h4', 'vf-subhead', 'رأس المال بالحصص'));
        c.append(grid([
          ['نوع رأس المال', k.type],
          ['رأس المال النقدي', k.cash ? amount(k.cash) + cur : ''],
          ['رأس المال العيني', k.in_kind ? amount(k.in_kind) + cur : ''],
          ['قيمة الحصة', k.share_value ? amount(k.share_value) + cur : ''],
          ['عدد الحصص النقدية', amount(k.cash_shares)],
          ['عدد الحصص العينية', amount(k.in_kind_shares)],
        ]));
      }
      if (cap.stock) {
        const s = cap.stock;
        c.append(node('h4', 'vf-subhead', 'رأس المال بالأسهم'));
        c.append(grid([
          ['نوع رأس المال', s.type],
          ['رأس المال المُصدَر', s.capital ? amount(s.capital) + cur : ''],
          ['رأس المال المصرّح به', s.announced ? amount(s.announced) + cur : ''],
          ['رأس المال المدفوع', s.paid ? amount(s.paid) + cur : ''],
          ['نقدي', s.cash ? amount(s.cash) + cur : ''],
          ['عيني', s.in_kind ? amount(s.in_kind) + cur : ''],
        ]));
        if ((s.stocks || []).length) c.append(table(['نوع السهم', 'العدد', 'القيمة', 'الفئة'],
          s.stocks.map(x => [x.type, amount(x.count), amount(x.value), x.class])));
      }
      out.push(c);
    }

    const parties = data.parties || [];
    if (parties.length) {
      const c = card(`الشركاء والملاك (${ar(parties.length)})`);
      c.append(table(['الاسم', 'الصفة', 'النوع', 'الجنسية', 'رقم الإثبات', 'الحصص', 'الأرباح / الخسائر'],
        parties.map(p => {
          const name = node('div'); put(name, p.name || '');
          if (p.cr_number) { const s = node('small', 'vf-sub', 'سجل تجاري: '); s.append(ltr(p.cr_number)); name.append(s); }
          if (p.guardian) {
            const g = node('small', 'vf-sub', 'الولي: ' + (p.guardian.name || ''));
            if (p.guardian.id_masked) { g.append(' · '); g.append(ltr(p.guardian.id_masked)); }
            name.append(g);
          }
          const shares = p.total_shares ? amount(p.total_shares) : '';
          let split = '';
          if (p.profit_pct || p.loss_pct) {
            split = node('span', 'vf-split');
            split.append('أرباح '); split.append(pct(p.profit_pct) || '-');
            split.append(' · خسائر '); split.append(pct(p.loss_pct) || '-');
          }
          return [name, (p.roles || []).join('، '), p.type, p.nationality, idCell(p.id_masked, p.id_type), shares, split];
        })));
      out.push(c);
    }

    const m = data.management || {};
    if ((m.managers || []).length || m.structure) {
      const c = card('الإدارة');
      c.append(grid([['الهيكل الإداري', m.structure], ['طريقة العزل', m.dismissal]]));
      if ((m.managers || []).length) c.append(table(['الاسم', 'المنصب', 'النوع', 'الجنسية', 'رقم الإثبات', 'مدير مرخّص'],
        m.managers.map(x => [x.name, (x.positions || []).join('، '), x.type, x.nationality,
          idCell(x.id_masked, x.id_type), yesNo(x.licensed)])));
      out.push(c);
    }

    if ((data.activities || []).length) {
      const c = card(`الأنشطة (${ar(data.activities.length)})`);
      const ul = node('ul', 'vf-list');
      for (const a of data.activities) {
        const li = node('li');
        if (a.code) { li.append(ltr(a.code)); li.append(' · '); }
        put(li, a.name);
        ul.append(li);
      }
      c.append(ul);
      out.push(c);
    }

    if ((data.decisions || []).length || data.decisions_note || (data.profit_set_aside || {}).pct) {
      const c = card('قرارات الشركاء');
      if ((data.decisions || []).length) c.append(table(['القرار', 'نسبة الموافقة', 'ملاحظة'],
        data.decisions.map(d => [d.name, pct(d.approve_pct), d.note])));
      if (data.decisions_note) c.append(node('p', 'vf-text', data.decisions_note));
      const set = data.profit_set_aside || {};
      if (set.pct) {
        const v = node('span'); v.append(pct(set.pct));
        if (set.purpose) { v.append(' · '); put(v, set.purpose); }
        c.append(grid([['تجنيب من الأرباح', v]]));
      }
      out.push(c);
    }

    if ((data.articles || []).length) {
      const c = card('', 'vf-articles');
      const det = node('details');
      det.append(node('summary', '', `البنود النصية للعقد (${ar(data.articles.length)})`));
      const ol = node('ol', 'vf-articles-list');
      for (const a of data.articles) {
        const li = node('li');
        const label = [a.part, a.title].filter(Boolean).join(' · ');
        if (label) li.append(node('strong', '', label));
        li.append(node('p', 'vf-text', a.text));
        ol.append(li);
      }
      det.append(ol); c.append(det);
      out.push(c);
    }
    return out;
  }

  function show(data) {
    const out = data.view_type === 'contract' ? renderContract(data) : renderGeneric(data);
    out.push(node('p', 'vf-foot', 'أرقام الهويات والهواتف والبريد مخفية جزئيًا. البيانات كما وردت من وثق؛ عند وجود اختلاف يُرجع إلى الجهة المالكة للبيانات.'));
    results.replaceChildren(...out);
    results.hidden = false;
    placeholder.hidden = true;
  }

  // ---------------------------------------------------------------- run
  async function lookup() {
    if (!ready() || !configured || busy) return;
    if (cost() >= CONFIRM_FROM) {               // an expensive query asks twice
      if (!armed) {
        armed = true;
        armedAt = performance.now();
        run.classList.add('is-confirm');
        refresh();
        status.textContent = `هذا الاستعلام مكلف (${price(cost())}). اضغط الزر مرة أخرى للتأكيد.`;
        return;
      }
      if (performance.now() - armedAt < CONFIRM_GAP) return;   // the second click of a double-click
    }
    disarm();
    const gen = ++generation;
    controller = new AbortController();
    errorBox.hidden = true;
    results.hidden = true;
    results.replaceChildren();
    placeholder.hidden = false;
    const ep = endpoint;
    setBusy(true);
    status.textContent = willConvert() ? 'جارٍ تحويل رقم السجل ثم الاستعلام من وثق…' : 'جارٍ الاستعلام من وثق…';
    try {
      const r = await fetch('/wathq/query', {
        method: 'POST', signal: controller.signal, cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-Wathq-Request': '1'},
        body: JSON.stringify({endpoint: ep.id, inputs: values(), language: ep.language ? lang.value : 'ar'}),
      });
      const j = await r.json().catch(e => { if (e.name === 'AbortError') throw e; return null; }) || {};
      if (gen !== generation) return;
      if (r.ok && !j.view_type) {
        status.textContent = '';
        showError('ردّ غير متوقع من التطبيق. أعد المحاولة.');
      } else if (!r.ok) {
        status.textContent = '';
        showError(j.error || `تعذّر الاستعلام (HTTP ${r.status}).`, j.code, j.detail);
        if (configured && typeof j.sent === 'number') config.textContent = sentLine(j.sent, lastEnv);
      } else {
        show(j);
        status.textContent = j.calls_used ? 'اكتمل الاستعلام.' : 'عُرضت النتيجة من الذاكرة المؤقتة دون طلب جديد.';
        if (configured) { lastEnv = j.env; config.textContent = sentLine(j.sent, j.env); config.dataset.state = 'ok'; }
      }
    } catch (e) {
      if (gen !== generation) return;
      status.textContent = '';
      if (e.name === 'AbortError') status.textContent = cacheSeconds > 0
        ? 'أُلغي انتظار الرد. إن أكملته وثق فستُعرض النتيجة من الذاكرة المؤقتة دون خصم إذا أعدت الاستعلام نفسه.'
        : 'أُلغي انتظار الرد. قد تكون وثق أكملت الطلب وخصمته؛ إعادة الاستعلام تُخصم من جديد.';
      else showError('تعذّر الاتصال بالتطبيق.');
    } finally {
      if (gen === generation) { controller = null; setBusy(false); }
    }
  }

  // ---------------------------------------------------------------- what this document can verify
  // After structuring, the analysis page hands the structured result to
  // suggest(). The server asks the local model which services fit (no Wathq
  // call) and returns them in the model's order with their key fields
  // pre-filled. The first is ticked, the rest are not; the user corrects a
  // value or ticks another service, then one button runs the ticked ones
  // through /wathq/query.
  const sugBox = $('vf-suggest'), sugList = $('vf-suggest-list'), sugNote = $('vf-suggest-note');
  const sugRun = $('vf-suggest-run'), sugStatus = $('vf-suggest-status'), sugCost = $('vf-suggest-cost');
  const hint = $('vf-hint');
  let sugGen = 0, sugServices = [], sugBusy = false, sugArmed = false, sugArmedAt = 0, sugStruct = null;
  const VERDICT_AR = {matches: 'يطابق', partly_matches: 'يطابق جزئيًا', differs: 'يختلف عن سجل وثق',
    not_in_document: 'غير وارد في المستند', not_compared: 'لم تتم مقارنته'};

  function sugReset() {
    sugGen++; sugServices = []; sugBusy = false; sugArmed = false; sugStruct = null;
    if (!sugBox) return;
    sugList.replaceChildren(); sugBox.hidden = true;
    sugNote.textContent = ''; sugStatus.textContent = ''; sugCost.textContent = '';
    sugRun.disabled = true; sugRun.textContent = 'تحقق من الخدمات المحددة'; sugRun.classList.remove('is-confirm');
    if (hint) { hint.replaceChildren(); hint.classList.add('hidden'); }
  }
  async function suggest(struct) {
    if (!sugBox) return;
    const gen = ++sugGen;
    sugStruct = struct;
    sugServices = []; sugList.replaceChildren(); sugBox.hidden = false;
    sugNote.textContent = 'جارٍ تحديد ما يمكن التحقق منه في هذا المستند…';
    sugRun.disabled = true; sugCost.textContent = ''; sugStatus.textContent = '';
    loadStatus();
    try {
      const r = await fetch('/wathq/suggest', {
        method: 'POST', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-Wathq-Request': '1'},
        body: JSON.stringify({struct}),
      });
      const j = await r.json().catch(() => ({}));
      if (gen !== sugGen) return;
      if (!r.ok) { sugNote.textContent = j.error || 'تعذّر تحديد خدمات التحقق.'; return; }
      sugServices = Array.isArray(j.services) ? j.services : [];
      renderSuggestions();
    } catch (e) {
      if (gen === sugGen) sugNote.textContent = 'تعذّر الاتصال بالتطبيق.';
    }
  }
  function renderSuggestions() {
    sugList.replaceChildren();
    if (!sugServices.length) {
      sugNote.textContent = 'لم يجد النموذج المحلي خدمة تحقق تناسب بيانات هذا المستند.';
      return;
    }
    sugNote.textContent = 'رتّب النموذج المحلي الخدمات حسب مناسبتها لبيانات المستند: الأولى محددة والباقي بحسب الترتيب. '
      + 'راجع القيم وصحّحها إن لزم، ثم اضغط «تحقق». لا يُرسل شيء إلى وثق قبل ذلك.';
    sugServices.forEach((s, i) => {
      const row = node('div', 'vf-sug');
      row.dataset.index = i;
      const head = node('label', 'vf-sug-head');
      const box = document.createElement('input');
      box.type = 'checkbox'; box.checked = i === 0 && s.available; box.disabled = !s.available;
      box.addEventListener('change', () => { sugDisarm(); sugRefresh(); });
      head.append(box, node('strong', '', s.label), node('span', 'vf-endpoint-desc', s.endpoint_label));
      if (!s.available) head.append(node('span', 'vf-endpoint-desc', 'غير متاحة في البيئة الاختبارية'));
      head.append(node('span', 'vf-price' + (s.price === 0 ? ' is-free' : s.price >= CONFIRM_FROM ? ' is-high' : ''), price(s.price)));
      row.append(head);
      const fields = node('div', 'vf-sug-fields');
      for (const inp of s.inputs_spec || []) {
        const id = `vf-sug-${i}-${inp.name}`;
        const wrap = node('div', 'vf-field');
        const optional = !inp.required && !(s.one_of || []).includes(inp.name);
        const label = node('label', '', inp.label + (optional ? ' (اختياري)' : ''));
        label.htmlFor = id;
        let control;
        if (inp.choices && inp.choices.length) {
          control = document.createElement('select');
          control.append(new Option('—', ''));
          for (const c of inp.choices) control.append(new Option(c.label || c.value, c.value));
          control.value = s.inputs[inp.name] || '';
        } else {
          control = document.createElement('input');
          control.type = 'text'; control.autocomplete = 'off'; control.spellcheck = false;
          control.value = s.inputs[inp.name] || '';
          if (DIGIT_KINDS.has(inp.kind) || inp.kind === 'person_or_entity_id') control.inputMode = 'numeric';
        }
        control.id = id; control.dataset.name = inp.name; control.dataset.kind = inp.kind;
        control.addEventListener(control.tagName === 'SELECT' ? 'change' : 'input', () => { sugDisarm(); sugRefresh(); });
        wrap.append(label, control);
        if (!s.inputs[inp.name] && !optional) wrap.append(node('small', 'vf-hint', 'لم يُعثر عليه في المستند؛ أدخله إن كان متوفرًا.'));
        fields.append(wrap);
      }
      row.append(fields, node('p', 'vf-sug-status'), node('div', 'vf-sug-result'));
      sugList.append(row);
    });
    if (hint) {
      const n = sugServices.length;
      hint.replaceChildren(`يقترح النموذج ${n === 1 ? 'تحققًا واحدًا' : n === 2 ? 'تحققين' : ar(n) + ' تحققات'} من وثق لهذا المستند. `);
      const open = node('button', 'ghost', 'افتح التحقق من البيانات');
      open.type = 'button';
      open.addEventListener('click', () => { const t = $('verify-tab'); if (t) t.click(); });
      hint.append(open);
      hint.classList.remove('hidden');
    }
    sugRefresh();
  }
  function sugRows() {
    return [...sugList.querySelectorAll('.vf-sug')].map(row => {
      const s = sugServices[+row.dataset.index];
      const values = {};
      for (const c of row.querySelectorAll('[data-name]')) {
        const v = DIGIT_KINDS.has(c.dataset.kind) || c.dataset.kind === 'person_or_entity_id' ? fold(c.value) : c.value.trim();
        if (v) values[c.dataset.name] = v;
      }
      return {row, s, values, checked: row.querySelector('.vf-sug-head input').checked};
    });
  }
  function sugMissing(s, values) {
    const spec = s.inputs_spec || [];
    const missing = spec.filter(i => i.required && !values[i.name]).map(i => i.label);
    if ((s.one_of || []).length && !s.one_of.some(n => values[n])) {
      missing.push(spec.filter(i => s.one_of.includes(i.name)).map(i => i.label).join(' أو '));
    }
    return missing;
  }
  function sugTotal() {
    let total = 0;
    for (const {s, values, checked} of sugRows()) {
      if (!checked) continue;
      total += s.price > 0 ? s.price : 0;
      const old = s.converts && Object.values(values).some(v => /^[1-6][0-9]{9}$/.test(v));
      if (old) total += 2;
    }
    return total;
  }
  function sugDisarm() {
    if (sugArmed) sugStatus.textContent = '';
    sugArmed = false; sugRun.classList.remove('is-confirm');
  }
  function sugRefresh() {
    const rows = sugRows();
    const picked = rows.filter(r => r.checked);
    const total = sugTotal();
    sugRun.disabled = sugBusy || !configured || !picked.length;
    sugRun.textContent = sugArmed ? `تأكيد: سيُخصم ${price(total)}` : `تحقق من الخدمات المحددة · ${price(total)}`;
    sugCost.textContent = !configured ? 'لم يُضبط مفتاح وثق بعد.'
      : picked.length ? `${picked.length === 1 ? 'خدمة واحدة محددة' : picked.length === 2 ? 'خدمتان محددتان' : ar(picked.length) + ' خدمات محددة'}؛ يُخصم السعر عند كل استعلام ناجح.`
      : 'حدّد خدمة واحدة على الأقل.';
  }
  async function runSuggested() {
    if (sugBusy || !configured) return;
    const picked = sugRows().filter(r => r.checked);
    if (!picked.length) return;
    if (sugTotal() >= CONFIRM_FROM) {           // an expensive run asks twice, like the form
      if (!sugArmed) {
        sugArmed = true; sugArmedAt = performance.now();
        sugRun.classList.add('is-confirm'); sugRefresh();
        sugStatus.textContent = `هذه الاستعلامات مكلفة (${price(sugTotal())}). اضغط الزر مرة أخرى للتأكيد.`;
        return;
      }
      if (performance.now() - sugArmedAt < CONFIRM_GAP) return;
    }
    sugDisarm();
    const gen = sugGen;
    sugBusy = true; sugRefresh();
    for (const el of sugList.querySelectorAll('input, select')) el.disabled = true;
    let done = 0;
    try {
      for (const {row, s, values} of picked) {
        const st = row.querySelector('.vf-sug-status'), out = row.querySelector('.vf-sug-result');
        out.replaceChildren();
        const missing = sugMissing(s, values);
        if (missing.length) {
          st.dataset.state = 'bad'; st.textContent = 'يلزم إدخال: ' + missing.join('، ');
          continue;
        }
        st.dataset.state = ''; st.textContent = 'جارٍ الاستعلام من وثق…';
        sugStatus.textContent = `جارٍ التحقق (${ar(done + 1)} من ${ar(picked.length)})…`;
        let r, j;
        try {
          r = await fetch('/wathq/query', {
            method: 'POST', cache: 'no-store',
            headers: {'Content-Type': 'application/json', 'X-Wathq-Request': '1'},
            body: JSON.stringify({endpoint: s.endpoint, inputs: values, language: 'ar'}),
          });
          j = await r.json().catch(() => ({}));
        } catch (e) {
          if (gen !== sugGen) return;
          st.dataset.state = 'bad'; st.textContent = 'تعذّر الاتصال بالتطبيق.';
          continue;
        }
        if (gen !== sugGen) return;
        if (!r.ok || !j.view_type) {
          st.dataset.state = 'bad';
          st.replaceChildren(j.error || `تعذّر الاستعلام (HTTP ${r.status}).`);
          if (j.code) { st.append(' (رمز وثق: '); st.append(ltr(j.code)); st.append(')'); }
          if (j.detail) st.append(wathqSaid(j.detail));
          if (configured && typeof j.sent === 'number') config.textContent = sentLine(j.sent, lastEnv);
          continue;
        }
        const det = node('details', '');
        det.open = true;
        det.append(node('summary', '', j.calls_used ? 'نتيجة وثق' : 'نتيجة وثق (من الذاكرة المؤقتة)'));
        det.append(...(j.view_type === 'contract' ? renderContract(j) : renderGeneric(j)));
        out.append(det);
        st.dataset.state = 'ok'; st.textContent = 'اكتمل الاستعلام.';
        done++;
        if (configured) { lastEnv = j.env; config.textContent = sentLine(j.sent, j.env); config.dataset.state = 'ok'; }
        await compareRow(row, j, gen);
      }
      sugStatus.textContent = done === picked.length ? 'اكتملت الاستعلامات.' : `اكتمل ${ar(done)} من ${ar(picked.length)}؛ راجع الملاحظات تحت كل خدمة.`;
    } finally {
      if (gen === sugGen) {
        sugBusy = false;
        for (const el of sugList.querySelectorAll('input, select')) el.disabled = false;
        for (const row of sugList.querySelectorAll('.vf-sug')) {
          const s = sugServices[+row.dataset.index];
          if (!s.available) row.querySelector('.vf-sug-head input').disabled = true;
        }
        sugRefresh();
      }
    }
  }
  // The register answer against the document: numbers and dates by code, the
  // rest judged by the local model. Shown as a table under the result.
  async function compareRow(row, answer, gen) {
    if (!sugStruct) return;
    const st = row.querySelector('.vf-sug-status'), out = row.querySelector('.vf-sug-result');
    st.textContent = 'اكتمل الاستعلام. جارٍ مقارنة بيانات المستند بسجل وثق…';
    let r, j;
    try {
      r = await fetch('/wathq/compare', {
        method: 'POST', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-Wathq-Request': '1'},
        body: JSON.stringify({struct: sugStruct, result: answer}),
      });
      j = await r.json().catch(() => ({}));
    } catch (e) {
      if (gen === sugGen) st.textContent = 'اكتمل الاستعلام. تعذّر الاتصال بالتطبيق للمقارنة.';
      return;
    }
    if (gen !== sugGen) return;
    if (!r.ok || !Array.isArray(j.rows)) {
      st.textContent = 'اكتمل الاستعلام. ' + (j.error || 'تعذّرت المقارنة.');
      return;
    }
    const det = node('details', 'vf-cmp');
    det.open = true;
    det.append(node('summary', '', 'مقارنة المستند بسجل وثق'));
    const c = j.counts || {};
    const parts = ['matches', 'partly_matches', 'differs', 'not_in_document', 'not_compared']
      .filter(k => c[k]).map(k => `${VERDICT_AR[k]}: ${ar(c[k])}`);
    det.append(node('p', 'vf-cmp-counts', parts.join(' · ') || 'لا بنود للمقارنة.'));
    const rows = j.rows.map(x => {
      const chip = node('span', 'vf-verdict', x.verdict_ar || VERDICT_AR[x.verdict] || x.verdict);
      chip.dataset.v = x.verdict;
      const cell = document.createElement('span');
      cell.append(chip);
      if (x.by === 'model') cell.append(node('span', 'vf-by', '(تقدير النموذج)'));
      return [x.label, x.document || '—', x.register, cell];
    });
    det.append(table(['البند', 'المستند', 'وثق', 'النتيجة'], rows));
    det.append(node('p', 'vf-foot', 'الاختلاف يعني أن السجل يذكر قيمة مختلفة، لا حكمًا على المستند. الأرقام والتواريخ قورنت حرفيًا؛ الأسماء والنصوص بتقدير النموذج المحلي.'));
    out.append(det);
    st.textContent = 'اكتمل الاستعلام والمقارنة.';
  }
  if (sugRun) sugRun.addEventListener('click', runSuggested);

  form.addEventListener('submit', ev => { ev.preventDefault(); lookup(); });
  cancel.addEventListener('click', () => { if (controller) controller.abort(); });
  lang.addEventListener('change', () => { disarm(); refresh(); });
  function activate() { loadStatus(); loadCatalog(); }
  window.addEventListener('workspace:change', ev => {
    if (ev.detail && ev.detail.panel === 'verify-workspace') activate();
  });
  if (!root.hidden) activate();
  window.WathqUI = {reload: activate, suggest, reset: sugReset};
  refresh();
})();
