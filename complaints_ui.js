/* Complaint management workspace (third tab): intake → register → analytics over
   /complaints/*. Everything the server returns — OCR text, model output, even
   file names — is untrusted: the DOM is built with createElement/textContent
   only, never parsed from HTML strings. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const root = $('complaints-workspace');
  if (!root) return;

  const LOCALE = 'ar-u-nu-arab';          // counts in Arabic-Indic digits even where 'ar' defaults to Latin
  const DIGITS = '٠١٢٣٤٥٦٧٨٩';
  const DEFAULT_LIMITS = {max_files: 20, max_bytes: 100 * 1024 * 1024, max_text_chars: 40000};
  const FILE_RE = /\.(pdf|png|jpe?g|webp|tiff?|bmp|txt)$/i;
  const PAGE_SIZE = 100;
  const INTAKE_LIMIT = 200;
  const POLL_MS = 2000;
  const STEPS = [['ocr', 'استخراج النص'], ['structuring', 'الهيكلة'], ['classifying', 'التصنيف والأولوية'], ['done', 'مكتملة']];
  const STAGE_LABELS = {queued: 'في الانتظار', ocr: 'استخراج النص', structuring: 'الهيكلة',
    classifying: 'التصنيف والأولوية', done: 'مكتملة', error: 'تعذرت المعالجة'};
  const ACTIVE = new Set(['queued', 'ocr', 'structuring', 'classifying']);
  const PRIORITIES = ['critical', 'high', 'medium', 'low'];
  const PROVIDER_STATUS = {ready: 'جاهز', loading: 'جارٍ التحميل', not_loaded: 'يُحمَّل عند الحاجة', error: 'خطأ', unconfigured: 'غير مُعدّ'};
  const CONFIDENCE = {high: 'عالية', medium: 'متوسطة', low: 'منخفضة'};
  const FIELD_KINDS = {category: 'categories', ministry: 'ministries', priority: 'priorities', governorate: 'governorates', region: 'regions', status: 'statuses'};
  const FIELD_LABELS = {category: 'التصنيف', subcategory: 'التصنيف الفرعي', ministry: 'الجهة المختصة', priority: 'الأولوية',
    governorate: 'المحافظة', region: 'المنطقة'};
  // Where a place id came from: the deterministic place map, the model's own guess, or neither.
  const PLACE_SOURCES = {place_map: 'من اسم المكان في النص', city_map: 'من اسم المدينة في النص', llm: 'تقدير النموذج', none: 'لم تُحدَّد'};
  const OUTSIDE = 'outside_jurisdiction';
  const TIMING_LABELS = {ocr_s: 'استخراج النص', structure_s: 'الهيكلة', classify_s: 'التصنيف', total_s: 'الإجمالي'};
  const EVENT_LABELS = {created: 'استلام الشكوى', stage: 'انتقال إلى مرحلة', processed: 'اكتملت المعالجة', error: 'تعذرت المعالجة',
    feedback: 'تقييم المراجع', status: 'تغيير الحالة', reprocess: 'إعادة المعالجة', field_review: 'مراجعة حقل'};
  const LTR_FIELDS = new Set(['national_id', 'phone', 'email']);
  // Fallbacks when a field arrives without label_ar (an older server, or a new field it does not label yet).
  const FIELD_NAMES = {addressed_to: 'الجهة الموجّه إليها الخطاب', incident_location: 'موقع المشكلة'};
  /* Analyses stored before reviewers could accept or change a field still carry
     these two warnings; the «تحتاج مراجعة» section and the evidence list now
     say the same without alarm, so they are not shown. */
  const RETIRED_WARNINGS = /^(?:قيم لم يُعثر عليها حرفياً في النص|استُبعد .+ من اقتباسات الأدلة)/;
  const NETWORK_ERROR = 'تعذر الاتصال بالخادم. تحقق من تشغيل التطبيق ثم أعد المحاولة.';
  const HTTP_ERRORS = {400: 'بيانات الطلب غير صالحة.', 404: 'لم يُعثر على الشكوى؛ ربما حُذفت.',
    409: 'لا يمكن تنفيذ الإجراء الآن؛ الشكوى قيد المعالجة.', 413: 'حجم الطلب أكبر من الحد المسموح.',
    415: 'نوع الملف غير مدعوم.', 422: 'بيانات الطلب غير صالحة.', 503: 'النموذج غير متاح حالياً؛ أعد المحاولة لاحقاً.'};
  /* Tokens that must keep memory order inside RTL text (README «Arabic numbers
     and bidi»): e-mails, refs like CMP-2026-000012, and any run of digits with
     - / : . , inside. The hyphen is LAST in the class so it is not a range. */
  const TOKEN = /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|[A-Za-z]{2,}-[0-9٠-٩]+(?:-[0-9٠-٩]+)*|[0-9٠-٩][0-9٠-٩٫٬.,:\/-]*[0-9٠-٩]/g;
  // Same marker rule as provenance.py: the whole line, optional trailing blanks.
  const PAGE_MARK = /^--- Page ([0-9٠-٩۰-۹]+) ---[ \t]*$/;

  let config = null, configLoading = null;
  let active = false, view = 'intake';
  let queue = null, pollTimer = 0, polling = false, wasBusy = false;
  const session = new Map();              // id → {id, ref, filename, stage, duplicate, lastStage}
  let rejected = [], rejectedSeq = 0;     // files refused before or by the server (no id)
  let intakeItems = [], latestStage = new Map();   // latestStage: id → stage of every row fetched
  const jobRows = new Map();
  let uploading = false;
  const register = {items: [], total: 0, gen: 0};
  let analytics = null, analyticsGen = 0, insightsResult = null, trendState = null;
  /* The detail opens in place (an accordion row in the register): expandedId is
     the complaint it shows, panelRow the <tr> that holds it, null when collapsed. */
  let detail = null, detailGen = 0, expandedId = null, panelRow = null, openGen = 0;
  // Field review: the open editors (field key → unsaved text) and the complaint whose last pending field was just settled.
  const fieldEdits = new Map();
  let settledFields = null;
  let fileUrl = null, fileUrlId = null, fileAbort = null, viewerTab = 'file', pendingPage = 0;
  let ocr = null, ocrKey = null;

  class ApiError extends Error {
    constructor(message, status) { super(message); this.status = status; }
  }

  /* ---------------------------------------------------------------- helpers */
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }
  function button(text, cls, onClick) {
    const node = el('button', cls || 'cms-btn', text);
    node.type = 'button';
    if (onClick) node.addEventListener('click', onClick);
    return node;
  }
  function str(value) { return typeof value === 'string' ? value : (value === null || value === undefined ? '' : String(value)); }
  function arr(value) { return Array.isArray(value) ? value : []; }
  function obj(value) { return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; }
  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString(LOCALE) : '—';
  }
  function pct(ratio) {
    const n = Number(ratio);
    return Number.isFinite(n) ? Math.round(n * 100).toLocaleString(LOCALE) + '٪' : '—';
  }
  function seconds(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toLocaleString(LOCALE, {maximumFractionDigits: 1}) + ' ث' : '—';
  }
  function arDigits(text) { return String(text).replace(/[0-9]/g, d => DIGITS[+d]); }
  function pad(n) { return String(n).padStart(2, '0'); }
  function bytes(n) { return `${num(Math.round(n / 1048576))} MB`; }
  /* Arabic counted nouns: [one, two, 3–10, 11+]. The dual changes with the
     case, so a noun after a preposition or in an idafa (من، رفع) takes the
     *_GEN form and an adverbial one (متأخرة يومين) the *_ACC form. */
  const FILES = ['ملف واحد', 'ملفان', 'ملفات', 'ملفاً'];
  const FILES_GEN = ['ملف واحد', 'ملفين', 'ملفات', 'ملفاً'];
  const COMPLAINTS = ['شكوى واحدة', 'شكويان', 'شكاوى', 'شكوى'];
  const COMPLAINTS_GEN = ['شكوى واحدة', 'شكويين', 'شكاوى', 'شكوى'];
  const DAYS = ['يوم واحد', 'يومان', 'أيام', 'يوماً'];
  const DAYS_ACC = ['يوماً واحداً', 'يومين', 'أيام', 'يوماً'];
  const REVIEWS = ['مراجعة واحدة', 'مراجعتان', 'مراجعات', 'مراجعة'];
  const RATINGS = ['تقييم واحد', 'تقييمان', 'تقييمات', 'تقييماً'];
  const PAGES = ['صفحة واحدة', 'صفحتان', 'صفحات', 'صفحة'];
  function count(n, [one, two, few, many]) {
    const v = Number(n) || 0;
    if (v === 1) return one;
    if (v === 2) return two;
    const tail = v % 100;
    return `${num(v)} ${tail >= 3 && tail <= 10 ? few : (v === 0 ? few : many)}`;
  }
  function ltr(text) {
    const node = el('bdo');
    node.dir = 'ltr';
    node.textContent = str(text);
    return node;
  }
  /* Text nodes, with every bidi-sensitive token pinned left-to-right. */
  function appendText(node, text) {
    const s = str(text);
    let last = 0;
    for (const m of s.matchAll(TOKEN)) {
      if (m.index > last) node.append(document.createTextNode(s.slice(last, m.index)));
      node.append(ltr(m[0]));
      last = m.index + m[0].length;
    }
    if (last < s.length) node.append(document.createTextNode(s.slice(last)));
    return node;
  }
  function textEl(tag, cls, text) { return appendText(el(tag, cls), text); }
  function showError(id, message, action) {
    const box = $(id);
    box.replaceChildren(el('span', '', message));
    if (action) box.append(action);
    box.hidden = false;
  }
  function hideError(id) { $(id).hidden = true; $(id).replaceChildren(); }
  function store(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* private mode */ } }
  function recall(key) { try { return localStorage.getItem(key); } catch (_) { return null; } }
  // A focused button that gets disabled drops the focus to <body>.
  function focusLost() { return !document.activeElement || document.activeElement === document.body; }
  const live = el('div', 'cms-sr-only');
  live.setAttribute('role', 'status');
  root.append(live);
  function announce(text) {
    // While the detail panel is open its own status line speaks (and shows) the result beside the action.
    const target = panelShown() ? $('cms-d-status') : live;
    target.textContent = '';
    setTimeout(() => { target.textContent = text; }, 30);
  }
  function query(params) {
    return Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined)
      .map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(String(v))).join('&');
  }
  function itemPath(id, suffix = '') { return '/complaints/items/' + encodeURIComponent(String(id)) + suffix; }

  /* Timestamps arrive as UTC ISO; the service counts days in Riyadh time
     (UTC+3, no DST), so the interface does too. */
  function riyadh(iso) {
    const t = Date.parse(str(iso));
    return Number.isFinite(t) ? new Date(t + 3 * 3600e3) : null;
  }
  function fmtDate(iso) {
    const d = riyadh(iso);
    return d ? arDigits(`${d.getUTCFullYear()}/${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}`) : '';
  }
  function fmtDateTime(iso) {
    const d = riyadh(iso);
    return d ? fmtDate(iso) + ' ' + arDigits(`${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`) : '';
  }
  function dateNode(iso, withTime) {
    const text = withTime ? fmtDateTime(iso) : fmtDate(iso);
    return text ? appendText(el('span'), text) : el('span', 'cms-muted', '—');
  }

  /* ---------------------------------------------------------------- network */
  function errorText(data, status, path = '') {
    const message = data && typeof data.error === 'string' ? data.error.trim() : '';
    if (message) return message.length > 300 ? message.slice(0, 300) + '…' : message;
    // A bare 404 outside /items/{id} means the complaints service is not mounted at all.
    if (status === 404 && !/^\/complaints\/items\/\d/.test(path)) return 'خدمة الشكاوى غير متاحة على هذا الخادم.';
    if (status === 404 && path.endsWith('/file')) return 'الملف الأصلي غير متاح.';
    return HTTP_ERRORS[status] || (status >= 500 ? 'حدث خطأ في الخادم؛ أعد المحاولة لاحقاً.' : 'تعذر تنفيذ الطلب.');
  }
  async function readJson(response) {
    try { return await response.json(); } catch (_) { return null; }
  }
  async function api(path, {method = 'GET', json, signal} = {}) {
    const init = {method, signal, cache: 'no-store', headers: {Accept: 'application/json'}};
    if (json !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(json);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (exc) {
      if (exc && exc.name === 'AbortError') throw exc;
      throw new ApiError(NETWORK_ERROR, 0);
    }
    const data = await readJson(response);
    if (!response.ok) throw new ApiError(errorText(data, response.status, path), response.status);
    return data;
  }

  /* --------------------------------------------------------------- taxonomy */
  const tax = () => obj(config && config.taxonomy);
  const list = kind => arr(tax()[kind]).filter(item => item && item.id !== undefined && item.id !== null);
  const find = (kind, id) => list(kind).find(item => item.id === id) || null;
  function label(kind, id) {
    if (id === null || id === undefined || id === '') return '';
    const item = find(kind, id);
    return (item && str(item.label_ar)) || String(id);
  }
  function subLabel(id) {
    if (!id) return '';
    for (const category of list('categories')) {
      const sub = arr(category.subcategories).find(item => item && item.id === id);
      if (sub) return str(sub.label_ar) || String(id);
    }
    return String(id);
  }
  function reasonLabel(id) {
    const reasons = obj(tax().review_reasons);
    return str(reasons[id]) || String(id);
  }
  function signalLabel(id, fallback) { return label('signals', id) || str(fallback) || String(id); }
  function defaultMinistry(category) { const item = find('categories', category); return item ? str(item.ministry) : ''; }
  function isOpenStatus(id) {
    const item = find('statuses', id);
    return item ? item.open !== false : !['resolved', 'rejected'].includes(id);
  }
  function priorityOrder() {
    const ids = list('priorities').map(item => item.id);
    return ids.length ? ids : PRIORITIES;
  }
  function limits() {
    const out = {...DEFAULT_LIMITS};
    for (const [key, value] of Object.entries(obj(config && config.limits))) if (Number(value) > 0) out[key] = Number(value);
    return out;
  }
  function providerLabel(id) {
    const item = arr(config && config.providers).find(p => p && p.id === id);
    return item ? str(item.label) || String(id) : str(id);
  }
  /* The desk that receives every complaint (taxonomy receiving_entity), or null
     on a server that does not configure one — then the entity is simply not named. */
  function entity() {
    const e = obj(tax().receiving_entity);
    return str(e.label_ar).trim() ? e : null;
  }
  function entityName(fallback) { const e = entity(); return e ? str(e.label_ar).trim() : fallback; }
  function jurisdictionLabel() {
    const e = entity();
    return (e && e.region && label('regions', e.region)) || 'المنطقة';
  }
  /* Outside the entity's jurisdiction. The server decides from the effective
     region, so a reviewer's region correction counts (summary.outside_jurisdiction,
     the same rule as the KPI and the reply draft); a server that does not send
     the flag only has the review reason recorded at analysis time. */
  function outsideJurisdiction(item, reasons) {
    const flag = obj(item).outside_jurisdiction;
    if (typeof flag === 'boolean') return flag;
    return arr(reasons === undefined ? obj(item).review_reasons : reasons).includes(OUTSIDE);
  }
  function outsideTag() {
    const badge = tag('خارج النطاق', 'out');
    badge.title = reasonLabel(OUTSIDE) === OUTSIDE ? `موقع الشكوى خارج نطاق ${jurisdictionLabel()}` : reasonLabel(OUTSIDE);
    return badge;
  }

  function priorityBadge(id) {
    if (!id) return el('span', 'cms-muted', '—');
    const badge = el('span', 'cms-prio', label('priorities', id));
    badge.dataset.priority = PRIORITIES.includes(id) ? id : 'unknown';
    return badge;
  }
  function tag(text, tone) { return el('span', 'cms-tag' + (tone ? ' is-' + tone : ''), text); }
  function sla(item) {
    if (!item || item.stage !== 'done' || !item.due_at || !isOpenStatus(item.status)) return null;
    const due = Date.parse(item.due_at);
    if (!Number.isFinite(due)) return null;
    const hours = (due - Date.now()) / 3600e3, abs = Math.abs(hours);
    // «متبقٍ يومان» is a subject, «متأخرة يومين» an adverb: the dual differs.
    const amount = nouns => (abs < 24 ? `${num(Math.max(1, Math.round(abs)))} س` : count(Math.floor(abs / 24), nouns));
    if (hours < 0) return {text: `متأخرة ${amount(DAYS_ACC)}`, tone: 'over'};
    return {text: `متبقٍ ${amount(DAYS)}`, tone: hours <= 24 ? 'soon' : 'ok'};
  }
  function slaNode(item) {
    const info = sla(item);
    if (!info) return el('span', 'cms-muted', item && item.stage === 'done' && !isOpenStatus(item.status) ? 'مغلقة' : '—');
    return el('span', 'cms-sla' + (info.tone === 'ok' ? '' : ' is-' + info.tone), info.text);
  }
  function stageTag(stage) {
    return tag(STAGE_LABELS[stage] || str(stage) || '—', stage === 'error' ? 'crit' : (ACTIVE.has(stage) ? 'accent' : ''));
  }
  function stepper(stage, failedAt) {
    const order = STEPS.map(step => step[0]);
    const ol = el('ol', 'cms-steps');
    ol.setAttribute('aria-label', 'مراحل المعالجة: ' + (STAGE_LABELS[stage] || str(stage)));
    const index = stage === 'queued' ? -1 : order.indexOf(stage);
    const failIndex = stage === 'error' ? order.indexOf(failedAt) : -1;
    STEPS.forEach(([, name], i) => {
      const li = el('li', 'cms-step', name);
      let state = 'لم تبدأ';
      if (stage === 'done' || (index >= 0 && i < index) || (failIndex >= 0 && i < failIndex)) { li.classList.add('is-done'); state = 'اكتملت'; }
      else if (i === index) { li.classList.add('is-active'); li.setAttribute('aria-current', 'step'); state = 'جارية'; }
      else if (i === failIndex) { li.classList.add('is-failed'); state = 'تعذرت'; }
      li.title = `${name}: ${state}`;
      li.append(el('span', 'cms-sr-only', ` (${state})`));
      ol.append(li);
    });
    return ol;
  }

  /* ---------------------------------------------------------------- tooltip */
  const tip = el('div', 'cms-tip');
  tip.hidden = true;
  tip.setAttribute('aria-hidden', 'true');
  root.append(tip);
  function showTip(x, y, value, text) {
    tip.replaceChildren(el('strong', '', value), el('span', '', text));
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    const left = Math.min(Math.max(8, x - box.width / 2), window.innerWidth - box.width - 8);
    let top = y - box.height - 12;
    if (top < 8) top = y + 18;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hideTip() { tip.hidden = true; }
  function hover(target, content) {
    target.addEventListener('pointermove', event => { const c = content(); showTip(event.clientX, event.clientY, c[0], c[1]); });
    target.addEventListener('pointerleave', hideTip);
  }

  /* ----------------------------------------------------------------- config */
  function loadConfig({quiet = false} = {}) {
    if (configLoading) return configLoading;
    configLoading = (async () => {
      try {
        const data = await api('/complaints/config');
        config = obj(data);
        hideError('cms-alert');
        renderEntity();
        populateFilters();
        renderProviders();
        renderLimits();
      } catch (exc) {
        if (!quiet || !config) {
          showError('cms-alert', exc.message + ' تُعرض المعرّفات كما هي حتى يتوفر إعداد التصنيفات.',
            button('إعادة المحاولة', 'cms-btn cms-btn-sm', () => loadConfig().then(refreshView)));
          renderProviders();
        }
      } finally {
        configLoading = null;
      }
    })();
    return configLoading;
  }
  const introFallback = $('cms-intro').textContent;
  function renderEntity() {
    const e = entity(), chip = $('cms-entity');
    chip.hidden = !e;
    if (!e) { $('cms-intro').textContent = introFallback; return; }
    const name = str(e.label_ar).trim();
    $('cms-entity-name').textContent = name;
    chip.title = [str(e.desk_ar).trim() ? `${str(e.desk_ar).trim()} — ${name}` : name, str(e.label_en).trim(),
      e.region ? 'نطاق الاختصاص: ' + label('regions', e.region) : ''].filter(Boolean).join(' · ');
    $('cms-intro').textContent = `تستقبل ${name} الشكاوى الموجّهة إليها من ملفات PDF والصور والنصوص؛ يُستخرج نص كل شكوى وبياناتها، ` +
      'ثم تُصنَّف وتُحدَّد الوزارة المختصة التي تُحال إليها وأولويتها بنموذج لغوي قابل للتبديل، مع متابعتها وتحليلها.';
  }
  function renderLimits() {
    const lim = limits();
    $('cms-drop-hint').textContent = `PDF أو صور أو TXT بترميز UTF-8 · حتى ${num(lim.max_files)} ملفاً في كل دفعة · ${bytes(lim.max_bytes)} للملف`;
    updateCount();
  }
  function renderProviders(switching = false) {
    const select = $('cms-provider'), dot = $('cms-provider-dot'), chip = $('cms-locality');
    const providers = arr(config && config.providers).filter(p => p && p.id);
    select.replaceChildren();
    if (!providers.length) {
      select.add(new Option(config ? 'لا توجد نماذج مُعدّة' : 'غير متاح', ''));
      select.disabled = true;
      dot.dataset.state = 'error';
      return;
    }
    for (const p of providers) {
      const status = PROVIDER_STATUS[p.status] || str(p.status);
      select.add(new Option(`${str(p.label) || p.id}${status ? ' — ' + status : ''}${p.local === false ? ' (خارجي)' : ''}`, p.id));
    }
    const current = providers.find(p => p.id === config.active_provider) || null;
    if (current) select.value = current.id;
    select.disabled = switching;
    select.title = current ? [str(current.model), current.n_ctx ? `سياق ${num(current.n_ctx)} رمز` : ''].filter(Boolean).join(' · ') : '';
    dot.dataset.state = current ? str(current.status) : 'unknown';
    const local = !current || current.local !== false;
    chip.textContent = local ? 'معالجة محلية' : '⚠ معالجة عبر خدمة خارجية';
    chip.classList.toggle('is-remote', !local);
    chip.title = local ? 'لا تغادر المستندات هذا الجهاز.' : 'النموذج النشط خارج هذا الجهاز؛ تُرسل إليه نصوص الشكاوى.';
  }
  $('cms-provider').addEventListener('change', async event => {
    const id = event.target.value;
    if (!config || !id || id === config.active_provider) return;
    const previous = config.active_provider;
    renderProviders(true);
    try {
      const data = obj(await api('/complaints/provider', {method: 'PUT', json: {provider: id}}));
      config.active_provider = str(data.active_provider) || id;
      if (Array.isArray(data.providers)) config.providers = data.providers;
      hideError('cms-alert');
      announce('النموذج النشط: ' + providerLabel(config.active_provider));
    } catch (exc) {
      config.active_provider = previous;
      showError('cms-alert', exc.message);
    }
    renderProviders();
    refreshAfterChange();
  });

  function populateFilters() {
    const fill = (id, kind, all) => {
      const select = $(id), current = select.value;
      select.replaceChildren(new Option(all, ''));
      for (const item of list(kind)) select.add(new Option(str(item.label_ar) || String(item.id), item.id));
      if ([...select.options].some(option => option.value === current)) select.value = current;
    };
    fill('cms-f-category', 'categories', 'كل التصنيفات');
    fill('cms-f-ministry', 'ministries', 'كل الجهات');
    fill('cms-f-priority', 'priorities', 'كل الأولويات');
    fill('cms-f-status', 'statuses', 'كل الحالات');
    fill('cms-f-governorate', 'governorates', 'كل المحافظات');
    fill('cms-f-region', 'regions', 'كل المناطق');
    $('cms-f-governorate').closest('label').hidden = !list('governorates').length;
  }

  /* ------------------------------------------------------------------ queue */
  /* The chip is a polite live region polled every POLL_MS: touch it only when
     what it says changes, or a screen reader repeats «جارٍ: …» all batch long. */
  function setQueueChip(dot, text, title) {
    const node = $('cms-queue');
    const sig = dot.dataset.state + '\n' + text.textContent;
    if (node.dataset.sig !== sig) { node.replaceChildren(dot, text); node.dataset.sig = sig; }
    node.title = title;
  }
  async function loadQueue() {
    try {
      queue = obj(await api('/complaints/queue'));
    } catch (exc) {
      queue = null;
      const dot = el('span', 'cms-dot');
      dot.dataset.state = 'error';
      setQueueChip(dot, el('span', '', 'تعذر قراءة حالة الطابور'), exc.message);
      return;
    }
    if (config && queue.active_provider && queue.active_provider !== config.active_provider) {
      config.active_provider = queue.active_provider;      // switched from another window
      renderProviders();
    }
    const queued = Number(queue.queued) || 0, errors = Number(queue.errors) || 0;
    const current = queue.processing && typeof queue.processing === 'object' ? queue.processing : null;
    const dot = el('span', 'cms-dot');
    dot.dataset.state = queue.worker === 'stopped' ? 'error' : (current ? 'loading' : 'ready');
    const text = el('span');
    if (current) {
      text.append('جارٍ: ');
      text.append(current.ref ? ltr(current.ref) : document.createTextNode(str(current.filename) || '—'));
      text.append(' · ' + (STAGE_LABELS[current.stage] || str(current.stage)));
    }
    const parts = [];
    if (queued) parts.push(`في الانتظار ${num(queued)}`);
    if (errors) parts.push(`تعذرت ${num(errors)}`);
    if (queue.worker === 'stopped') parts.push('المعالج متوقف');
    if (parts.length) text.append((current ? ' · ' : '') + parts.join(' · '));
    if (!current && !parts.length) text.append('لا توجد شكاوى قيد المعالجة');
    setQueueChip(dot, text, '');
  }

  /* ---------------------------------------------------------------- polling */
  function busyNow() {
    // The queue is counted from the register itself, so it is the source of truth;
    // session stages only stand in while it cannot be read (they can be stale).
    if (queue) return (Number(queue.queued) || 0) > 0 || !!queue.processing;
    for (const job of session.values()) if (ACTIVE.has(job.stage)) return true;
    return false;
  }
  function schedule() {
    clearTimeout(pollTimer);
    pollTimer = 0;
    if (active && !document.hidden && busyNow()) pollTimer = setTimeout(tick, POLL_MS);
  }
  async function tick() {
    pollTimer = 0;
    if (polling) return;
    polling = true;
    try {
      // Queue first: the list is then at least as new, so a queue that reads idle
      // never leaves a row stuck on a stage the list saw a moment earlier.
      await loadQueue();
      await loadIntake();
      await followDetail();
    } finally {
      polling = false;
    }
    const busy = busyNow();
    if (wasBusy && !busy) {               // the queue just drained
      loadConfig({quiet: true});           // provider statuses change as models load/unload
      if (view !== 'intake') refreshView();
    }
    wasBusy = busy;
    schedule();
  }
  function kick() { wasBusy = true; clearTimeout(pollTimer); tick(); }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { clearTimeout(pollTimer); pollTimer = 0; }
    else if (active) tick();
  });

  /* ----------------------------------------------------------------- intake */
  function saveSession() {
    try {
      sessionStorage.setItem('cms.session', JSON.stringify([...session.values()].map(j => ({id: j.id, duplicate: j.duplicate, filename: j.filename}))));
    } catch (_) { /* storage blocked: the list still works for this page */ }
  }
  (() => {
    try {
      for (const j of arr(JSON.parse(sessionStorage.getItem('cms.session') || '[]'))) {
        if (j && Number.isInteger(j.id)) session.set(j.id, {id: j.id, ref: '', filename: str(j.filename), stage: 'queued', duplicate: !!j.duplicate, lastStage: null});
      }
    } catch (_) { /* nothing to restore */ }
  })();
  function addJob(item, filename) {
    const id = Number(item.id);
    if (!Number.isInteger(id)) return;
    const job = session.get(id) || {id, lastStage: null};
    Object.assign(job, {ref: str(item.ref), filename: str(item.filename) || filename || job.filename || '',
      stage: str(item.stage) || 'queued', duplicate: !!item.duplicate});
    session.set(id, job);
    saveSession();
  }
  function rejectFile(filename, message) {
    rejected.unshift({key: ++rejectedSeq, filename: str(filename) || '—', error: message});
    rejected = rejected.slice(0, 50);
  }
  function followJob(job, item) {
    if (job.stage !== item.stage && ACTIVE.has(job.stage) && job.stage !== 'queued') job.lastStage = job.stage;
    Object.assign(job, {ref: str(item.ref), stage: str(item.stage), filename: str(item.filename) || job.filename});
  }
  /* A session job older than the newest INTAKE_LIMIT rows (an old complaint sent
     back for reprocessing, or a reload after a big batch) is asked for by id, or
     its row would keep its last stage for good. Settled ones keep their row. */
  async function loadOffList(jobs) {
    for (let i = 0; i < jobs.length; i += 6) {
      await Promise.all(jobs.slice(i, i + 6).map(async job => {
        try {
          const item = obj(await api(itemPath(job.id)));
          followJob(job, item);
          const row = {};
          for (const key of ['id', 'ref', 'filename', 'source', 'stage', 'error', 'subject', 'priority', 'category', 'needs_review']) row[key] = item[key];
          job.row = row;
          latestStage.set(job.id, job.stage);
        } catch (exc) {
          if (exc && exc.status === 404) session.delete(job.id);      // deleted meanwhile
        }
      }));
    }
  }
  async function loadIntake() {
    try {
      const data = obj(await api('/complaints/items?' + query({sort: 'created', order: 'desc', limit: INTAKE_LIMIT})));
      const items = arr(data.items).filter(item => item && Number.isInteger(Number(item.id)));
      const seen = new Set();
      latestStage = new Map(items.map(item => [Number(item.id), str(item.stage)]));
      for (const item of items) {
        const id = Number(item.id);
        seen.add(id);
        const job = session.get(id);
        if (job) { followJob(job, item); job.row = null; }
      }
      const missing = [...session.values()].filter(job => !seen.has(job.id));
      // Fewer than INTAKE_LIMIT rows are all there is: a session id missing from them was deleted.
      if (items.length < INTAKE_LIMIT) for (const job of missing) session.delete(job.id);
      else await loadOffList(missing.filter(job => ACTIVE.has(job.stage) || !job.row));
      saveSession();
      intakeItems = items.filter(item => session.has(Number(item.id)) || item.stage !== 'done');
      $('cms-intake-summary').classList.remove('cms-count-over');
    } catch (exc) {
      $('cms-intake-summary').textContent = exc.message;
      $('cms-intake-summary').classList.add('cms-count-over');
    }
    renderIntake();
  }
  function intakeEntries() {
    const byId = new Map(intakeItems.map(item => [Number(item.id), item]));
    for (const job of session.values()) if (!byId.has(job.id)) byId.set(job.id, job.row ? {...job.row, stage: job.stage} : job);
    return [...byId.values()].sort((a, b) => Number(b.id) - Number(a.id)).slice(0, 150);
  }
  function renderIntake() {
    const entries = [
      ...rejected.map(r => ({key: 'x' + r.key, sig: '', build: () => rejectedRow(r)})),
      ...intakeEntries().map(item => {
        const job = session.get(Number(item.id));
        const sig = JSON.stringify([item.stage, item.ref, item.filename, item.error, item.subject, item.priority,
          item.category, item.needs_review, job && job.duplicate, job && job.lastStage]);
        return {key: 'i' + item.id, sig, build: () => jobRow(item, job)};
      }),
    ];
    const listNode = $('cms-intake-list');
    const focused = document.activeElement && listNode.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = focused && focused.closest('[data-key]') ? focused.closest('[data-key]').dataset.key : null;
    const focusAction = focused ? focused.dataset.action : null;
    const next = new Map(), nodes = [];
    for (const entry of entries) {
      const old = jobRows.get(entry.key);
      const node = old && old.sig === entry.sig ? old.node : entry.build();
      next.set(entry.key, {sig: entry.sig, node});
      nodes.push(node);
    }
    // Drop stale rows first, then insert new ones in place: an unchanged row is
    // never moved, so a focused button inside it keeps focus.
    const keep = new Set(nodes);
    for (const child of [...listNode.children]) if (!keep.has(child)) child.remove();
    let cursor = listNode.firstChild;
    for (const node of nodes) {
      if (node === cursor) { cursor = cursor.nextSibling; continue; }
      listNode.insertBefore(node, cursor);
    }
    while (cursor) { const after = cursor.nextSibling; cursor.remove(); cursor = after; }
    jobRows.clear();
    for (const [key, value] of next) jobRows.set(key, value);
    if (focusKey && focused && !focused.isConnected) {
      const row = jobRows.get(focusKey);
      const target = row && (row.node.querySelector(`[data-action="${focusAction}"]`) || row.node.querySelector('button'));
      if (target) target.focus();
    }
    const counts = {active: 0, done: 0, error: 0};
    for (const item of intakeEntries()) {
      if (ACTIVE.has(item.stage)) counts.active++;
      else if (item.stage === 'done') counts.done++;
      else if (item.stage === 'error') counts.error++;
    }
    const empty = !nodes.length;
    $('cms-intake-empty').hidden = !empty;
    $('cms-intake-clear').hidden = !(counts.done || rejected.length);
    if (!$('cms-intake-summary').classList.contains('cms-count-over')) {
      $('cms-intake-summary').textContent = empty ? '' :
        [`قيد المعالجة ${num(counts.active)}`, `مكتملة ${num(counts.done)}`, counts.error ? `تعذرت ${num(counts.error)}` : ''].filter(Boolean).join(' · ');
    }
  }
  function rejectedRow(r) {
    const li = el('li', 'cms-job');
    li.dataset.key = 'x' + r.key;
    li.dataset.stage = 'error';
    const head = el('div');
    head.append(textEl('div', 'cms-job-name', r.filename), el('div', 'cms-job-sub', 'لم يُقبل الملف'));
    head.firstChild.dir = 'auto';
    const actions = el('div', 'cms-job-actions');
    const dismiss = button('إخفاء', 'cms-btn cms-btn-sm', () => { rejected = rejected.filter(x => x !== r); renderIntake(); });
    dismiss.dataset.action = 'dismiss';
    actions.append(dismiss);
    li.append(head, el('div'), actions, textEl('p', 'cms-job-note is-error', r.error));
    return li;
  }
  function jobRow(item, job) {
    const id = Number(item.id), stage = str(item.stage);
    const li = el('li', 'cms-job');
    li.dataset.key = 'i' + id;
    li.dataset.stage = stage;
    const head = el('div');
    const name = textEl('div', 'cms-job-name', str(item.filename) || (item.source === 'text' ? 'نص ملصق' : 'شكوى'));
    name.dir = 'auto';
    const sub = el('div', 'cms-job-sub');
    if (item.ref) sub.append(ltr(item.ref));
    sub.append(stageTag(stage));
    if (job && job.duplicate) sub.append(tag('مرفوع سابقاً', 'warn'));
    if (stage === 'done') {
      sub.append(priorityBadge(item.priority));
      if (item.category) sub.append(el('span', '', label('categories', item.category)));
      if (item.needs_review) sub.append(tag('تحتاج مراجعة', 'warn'));
    }
    head.append(name, sub);
    const actions = el('div', 'cms-job-actions');
    if (stage === 'error') {
      const retry = button('إعادة المعالجة', 'cms-btn cms-btn-sm', event => reprocess(id, false, event.currentTarget));
      retry.dataset.action = 'reprocess';
      actions.append(retry);
    }
    const open = button('عرض التفاصيل', 'cms-btn cms-btn-sm', () => openComplaint(id, {ref: str(item.ref)}));
    open.dataset.action = 'open';
    actions.append(open);
    li.append(head, stepper(stage, job && job.lastStage), actions);
    if (stage === 'error') li.append(textEl('p', 'cms-job-note is-error', str(item.error) || 'تعذرت معالجة الشكوى.'));
    else if (stage === 'done' && item.subject) li.append(textEl('p', 'cms-job-note', item.subject));
    return li;
  }
  $('cms-intake-clear').addEventListener('click', () => {
    for (const [id, job] of [...session]) if (job.stage === 'done') session.delete(id);
    intakeItems = intakeItems.filter(item => item.stage !== 'done');
    rejected = [];
    saveSession();
    renderIntake();
  });

  // Files: the drop zone, the picker button, and a guard so a file dropped next
  // to the zone does not navigate the whole app away to the PDF.
  const drop = $('cms-drop'), picker = $('cms-files');
  const hasFiles = event => !!event.dataTransfer && [...event.dataTransfer.types].includes('Files');
  drop.addEventListener('click', event => {
    if (event.target === picker || event.target.closest('button') || uploading) return;
    picker.click();
  });
  $('cms-pick').addEventListener('click', () => picker.click());
  picker.addEventListener('change', () => { const files = [...picker.files]; picker.value = ''; handleFiles(files); });
  ['dragenter', 'dragover'].forEach(type => drop.addEventListener(type, event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    drop.classList.add('is-over');
  }));
  drop.addEventListener('dragleave', event => { if (!drop.contains(event.relatedTarget)) drop.classList.remove('is-over'); });
  drop.addEventListener('drop', event => {
    event.preventDefault();
    drop.classList.remove('is-over');
    handleFiles([...(event.dataTransfer ? event.dataTransfer.files : [])]);
  });
  root.addEventListener('dragover', event => { if (hasFiles(event)) event.preventDefault(); });
  root.addEventListener('drop', event => { if (hasFiles(event)) event.preventDefault(); });

  function uploadBatch(files, onProgress) {
    // XHR rather than fetch: large scanned PDFs deserve an upload progress bar.
    return new Promise((resolve, reject) => {
      const form = new FormData();
      for (const file of files) form.append('files', file, file.name);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/complaints/upload');
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.addEventListener('progress', event => { if (event.lengthComputable) onProgress(event.loaded / event.total); });
      xhr.addEventListener('load', () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (_) { data = null; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(obj(data));
        else reject(new ApiError(errorText(data, xhr.status, '/complaints/upload'), xhr.status));
      });
      xhr.addEventListener('error', () => reject(new ApiError(NETWORK_ERROR, 0)));
      xhr.addEventListener('abort', () => reject(new ApiError('أُلغي الرفع.', 0)));
      xhr.send(form);
    });
  }
  /* One sentence for the live status line: what was received, what was refused
     (here or by the server), and — for a single refusal — why. */
  function uploadSummary(received, duplicates, failed, reason) {
    const pronoun = received === 1 ? 'تجري معالجته' : (received === 2 ? 'تجري معالجتهما بالترتيب' : 'تجري معالجتها بالترتيب');
    const parts = [received ? `استُلم ${count(received, FILES)}` + (duplicates ? ` (منها ${num(duplicates)} مرفوع سابقاً)` : '') + ' — ' + pronoun
      : 'لم يُستلم أي ملف'];
    if (failed === 1 && reason) parts.push('رُفض ملف واحد: ' + reason.replace(/\.\s*$/, ''));
    else if (failed) parts.push(`رُفض ${count(failed, FILES)}؛ الأسباب في القائمة أدناه`);
    return parts.join(' · ') + '.';
  }
  async function handleFiles(files) {
    if (!files.length) return;
    if (uploading) { showError('cms-intake-error', 'انتظر اكتمال الرفع الحالي ثم أضف ملفات أخرى.'); return; }
    hideError('cms-intake-error');
    const lim = limits(), accepted = [];
    const progress = $('cms-upload-progress'), status = $('cms-upload-status');
    let received = 0, duplicates = 0, failed = 0, lastReason = '';
    const refuse = (name, message) => { rejectFile(name, message); failed++; lastReason = message; };
    for (const file of files) {
      const typed = file.type === 'application/pdf' || /^image\//.test(file.type) || file.type === 'text/plain';
      if (!FILE_RE.test(file.name) && !typed) refuse(file.name, 'نوع الملف غير مدعوم؛ اختر PDF أو صورة أو ملف TXT.');
      else if (!file.size) refuse(file.name, 'الملف فارغ.');
      else if (file.size > lim.max_bytes) refuse(file.name, `حجم الملف يتجاوز الحد الأقصى ${bytes(lim.max_bytes)}.`);
      else accepted.push(file);
    }
    renderIntake();
    // The status line is the live region: it speaks for every drop, even one where nothing was accepted.
    if (!accepted.length) { status.textContent = uploadSummary(0, 0, failed, lastReason); return; }
    uploading = true;
    drop.classList.add('is-busy');
    $('cms-pick').disabled = true;
    try {
      const batches = Math.ceil(accepted.length / lim.max_files);
      for (let b = 0; b < batches; b++) {
        const batch = accepted.slice(b * lim.max_files, (b + 1) * lim.max_files);
        status.textContent = `جارٍ رفع ${count(batch.length, FILES_GEN)}` + (batches > 1 ? ` (الدفعة ${num(b + 1)} من ${num(batches)})…` : '…');
        progress.hidden = false;
        progress.value = 0;
        const data = await uploadBatch(batch, ratio => { progress.value = Math.round(ratio * 100); });
        for (const item of arr(data.items)) {
          if (item && item.id !== undefined && item.id !== null && !item.error) {
            addJob(item, str(item.filename));
            received++;
            if (item.duplicate) duplicates++;
          } else {
            refuse(item && item.filename, str(item && item.error) || 'تعذر رفع الملف.');
          }
        }
        renderIntake();
      }
      status.textContent = uploadSummary(received, duplicates, failed, lastReason);
    } catch (exc) {
      showError('cms-intake-error', exc.message);
      status.textContent = received ? `استُلم ${count(received, FILES)} قبل توقف الرفع.` : (failed ? uploadSummary(0, 0, failed, lastReason) : '');
    } finally {
      uploading = false;
      drop.classList.remove('is-busy');
      $('cms-pick').disabled = false;
      progress.hidden = true;
      renderIntake();
      if (received) { kick(); refreshAfterChange(); }
    }
  }

  function updateCount() {
    const text = $('cms-text').value, max = limits().max_text_chars;
    const chars = [...text].length;              // Python counts code points, not UTF-16 units
    const node = $('cms-text-count');
    node.textContent = `${num(chars)} / ${num(max)} حرف`;
    node.classList.toggle('cms-count-over', chars > max);
    return chars;
  }
  $('cms-text').addEventListener('input', updateCount);
  $('cms-text-submit').addEventListener('click', async () => {
    const text = $('cms-text').value, title = $('cms-text-title').value.trim();
    hideError('cms-intake-error');
    if (!text.trim()) { showError('cms-intake-error', 'اكتب نص الشكوى أولاً.'); return; }
    const max = limits().max_text_chars;
    if (updateCount() > max) { showError('cms-intake-error', `النص يتجاوز ${num(max)} حرف؛ اختصره ثم أعد المحاولة.`); return; }
    const submit = $('cms-text-submit');
    submit.disabled = true;
    try {
      const data = obj(await api('/complaints/text', {method: 'POST', json: title ? {text, title} : {text}}));
      const item = obj(data.item);
      if (item.id === undefined || item.id === null) throw new ApiError('لم يُرجع الخادم رقم الشكوى.', 0);
      addJob(item, title || 'نص ملصق');
      $('cms-text').value = '';
      $('cms-text-title').value = '';
      updateCount();
      $('cms-upload-status').textContent = item.duplicate ? 'هذا النص مرفوع سابقاً؛ عُرضت الشكوى الموجودة.' : 'أُضيف النص إلى طابور المعالجة.';
      renderIntake();
      kick();
      refreshAfterChange();
    } catch (exc) {
      showError('cms-intake-error', exc.message);
    } finally {
      submit.disabled = false;
    }
  });

  async function reprocess(id, withOcr, trigger) {
    if (trigger) trigger.disabled = true;
    try {
      const data = obj(await api(itemPath(id, '/reprocess'), {method: 'POST', json: {ocr: !!withOcr}}));
      addJob({...obj(data.item), id, stage: str(obj(data.item).stage) || 'queued'});
      const job = session.get(Number(id));
      if (job) job.lastStage = null;
      announce('أُعيدت الشكوى إلى طابور المعالجة.');
      renderIntake();
      if (detail && Number(detail.id) === Number(id)) await reloadDetail();
      // Disabling the button dropped the focus to <body>: keep it in the panel
      // (on the button, or on the heading while the complaint waits in the queue).
      if (trigger && panel.contains(trigger) && focusLost()) (trigger.disabled ? $('cms-d-title') : trigger).focus({preventScroll: true});
      kick();
      refreshAfterChange();
    } catch (exc) {
      if (trigger && trigger.isConnected) { trigger.disabled = false; if (focusLost()) trigger.focus({preventScroll: true}); }
      if (panelShown() && expandedId === Number(id)) showError('cms-d-error', exc.message);
      else showError('cms-intake-error', exc.message);
    }
  }

  /* --------------------------------------------------------------- register */
  const FILTER_KEYS = ['category', 'ministry', 'priority', 'status', 'governorate', 'region'];
  function filterValues() {
    const [sort, order] = ($('cms-f-sort').value || 'created:desc').split(':');
    const values = {q: $('cms-f-q').value.trim()};
    for (const key of FILTER_KEYS) values[key] = $('cms-f-' + key).value;
    if ($('cms-f-review').checked) values.needs_review = 1;
    values.sort = sort;
    values.order = order;
    return values;
  }
  function filtersActive() {
    const v = filterValues();
    return !!(v.q || FILTER_KEYS.some(key => v[key]) || v.needs_review);
  }
  /* register.pending is the load in flight, so a deep link can wait for the list it lands in. */
  function loadRegister(append = false) {
    const run = fetchRegister(append);
    register.pending = run;
    run.then(() => { if (register.pending === run) register.pending = null; });
    return run;
  }
  async function registerSettled() {
    while (register.pending) {
      const run = register.pending;
      await run;
      if (register.pending === run) register.pending = null;
    }
  }
  async function fetchRegister(append) {
    const gen = ++register.gen;
    const filters = filterValues();
    $('cms-export').href = '/complaints/export.csv?' + query(filters);
    const offset = append ? register.items.length : 0;
    const wrap = root.querySelector('.cms-table-wrap');
    wrap.classList.add('is-loading');
    $('cms-more').disabled = true;
    try {
      const data = obj(await api('/complaints/items?' + query({...filters, limit: PAGE_SIZE, offset})));
      if (gen !== register.gen) return;
      const items = arr(data.items).filter(item => item && item.id !== undefined && item.id !== null);
      register.items = append ? register.items.concat(items) : items;
      register.total = Number.isFinite(Number(data.total)) ? Number(data.total) : register.items.length;
      hideError('cms-register-error');
      renderRegister();
    } catch (exc) {
      if (gen === register.gen) showError('cms-register-error', exc.message);
    } finally {
      if (gen === register.gen) { wrap.classList.remove('is-loading'); $('cms-more').disabled = false; }
    }
  }
  /* Every reload (polling, filters, a save) rebuilds the rows but never touches
     the open panel's row: moving it would reload the PDF viewer and drop what
     the reviewer typed. The new rows are laid around it instead — the expanded
     complaint's row right above it — and it keeps its place on screen. A
     complaint the filters no longer match keeps its panel, at the top of the
     list, marked «خارج نتائج التصفية الحالية» (a save that changes the status
     or the review flag must not snatch the panel away mid-review). */
  function renderRegister() {
    const tbody = $('cms-rows'), focused = document.activeElement;
    const focusRow = focused && focused !== document.body && tbody.contains(focused) ? focused.closest('tr.cms-rrow') : null;
    const anchor = panelRow && panelRow.isConnected ? panelRow.getBoundingClientRect().top : null;
    const rows = register.items.map(registerRow);
    for (const child of [...tbody.children]) if (child !== panelRow) child.remove();
    if (panelRow && panelRow.parentNode === tbody) {
      const at = rows.findIndex(row => Number(row.dataset.id) === expandedId);
      rows.forEach((row, i) => { if (i <= at) tbody.insertBefore(row, panelRow); else tbody.append(row); });
    } else tbody.append(...rows);
    markRows();
    keepTop(panelRow, anchor);
    if (focusRow && !focusRow.isConnected) {
      const trigger = triggerFor(focusRow.dataset.id);
      if (trigger) trigger.focus({preventScroll: true});
    }
    const shown = register.items.length;
    const headline = filtersActive() ? 'لا توجد شكاوى مطابقة' : 'لا توجد شكاوى بعد';
    // The count is the live region, so a search with no results says so there too
    // (kept visually hidden: the empty-state box below shows the same words).
    const countNode = $('cms-register-count');
    countNode.textContent = !shown ? headline + '.' : (shown >= register.total ? count(register.total, COMPLAINTS)
      : `عرض ${num(shown)} من ${count(register.total, COMPLAINTS_GEN)}`);
    countNode.classList.toggle('cms-sr-only', !shown);
    const empty = $('cms-register-empty');
    empty.hidden = !!shown;
    if (!shown) {
      empty.replaceChildren(el('strong', '', headline),
        document.createTextNode(filtersActive() ? 'غيّر عوامل التصفية أو امسحها.' : 'ابدأ من «استقبال ومعالجة» برفع ملفات الشكاوى.'));
    }
    root.querySelector('.cms-table-wrap').hidden = !shown && expandedId === null;
    $('cms-more').hidden = shown >= register.total;
  }
  function cell(content, cls) {
    const td = el('td', cls);
    if (content instanceof Node) td.append(content); else td.textContent = str(content);
    return td;
  }
  /* A row is an accordion header: the reference is a real button (Enter and
     Space toggle it, aria-expanded says the state), and a click anywhere on the
     row does the same — unless the reviewer was selecting text to copy. */
  function registerRow(item) {
    const tr = el('tr', 'cms-rrow');
    tr.dataset.id = item.id;
    const toggle = el('button', 'cms-rtoggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', 'cms-detail');
    // The name keeps the visible reference first (voice control says what it sees); aria-expanded gives the state.
    toggle.setAttribute('aria-label', `${str(item.ref) || '#' + item.id} — تفاصيل الشكوى`);
    toggle.append(chevron(), ltr(str(item.ref) || '#' + item.id));
    const done = item.stage === 'done';
    const subject = el('div');
    const title = textEl('div', 'cms-subject', str(item.subject) || str(item.filename) || '—');
    title.dir = 'auto';
    subject.append(title);
    const meta = el('div', 'cms-cell-sub');
    if (item.complainant_name) meta.append(textEl('span', '', item.complainant_name));
    if (item.needs_review) meta.append(tag('تحتاج مراجعة', 'warn'));
    else if (item.reviewed) meta.append(tag('روجعت', 'ok'));
    if (Number(item.pending_fields) > 0) meta.append(tag('حقول للمراجعة: ' + num(item.pending_fields)));
    if (meta.childNodes.length) subject.append(meta);
    const category = el('div', '', label('categories', item.category) || '—');
    if (item.subcategory) category.append(el('div', 'cms-cell-sub', subLabel(item.subcategory)));
    tr.append(
      cell(toggle, 'cms-nowrap'),
      cell(subject),
      cell(category),
      cell(label('ministries', item.ministry) || '—'),
      cell(done ? priorityBadge(item.priority) : el('span', 'cms-muted', '—'), 'cms-nowrap'),
      cell(placeCell(item)),
      cell(done ? el('span', '', label('statuses', item.status) || '—') : stageTag(item.stage), 'cms-nowrap'),
      cell(slaNode(item), 'cms-nowrap'),
      cell(dateNode(item.created_at), 'cms-nowrap'),
    );
    // The button's own click (mouse, Enter, Space) bubbles here: one handler, one toggle.
    tr.addEventListener('click', () => { if (!selectingIn(tr)) toggleDetail(item.id); });
    return tr;
  }
  function chevron() {
    const icon = svg('svg', {viewBox: '0 0 16 16', width: 12, height: 12, class: 'cms-chev', 'aria-hidden': 'true', focusable: 'false'});
    icon.append(svg('path', {d: 'M6 3.5 10.5 8 6 12.5', fill: 'none', stroke: 'currentColor', 'stroke-width': 2,
      'stroke-linecap': 'round', 'stroke-linejoin': 'round'}));
    return icon;
  }
  function selectingIn(node) {
    const selection = typeof window.getSelection === 'function' ? window.getSelection() : null;
    return !!selection && !selection.isCollapsed && String(selection).trim() !== '' && node.contains(selection.anchorNode);
  }
  /* The register's geography is the governorate; the region shows only when it
     is not the entity's own (a complaint about a place outside its jurisdiction). */
  function placeCell(item) {
    const box = el('div', '', label('governorates', item.governorate) || '—');
    const e = entity();
    const foreign = item.region && item.region !== 'unknown' && e && e.region && item.region !== e.region;
    const outside = outsideJurisdiction(item);
    if (foreign || outside) {
      const sub = el('div', 'cms-cell-sub');
      if (foreign) sub.append(el('span', '', label('regions', item.region)));
      if (outside) sub.append(outsideTag());
      box.append(sub);
    }
    return box;
  }
  let searchTimer = 0;
  $('cms-filters').addEventListener('submit', event => { event.preventDefault(); loadRegister(); });
  $('cms-f-q').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadRegister(), 300); });
  for (const id of [...FILTER_KEYS.map(key => 'cms-f-' + key), 'cms-f-sort', 'cms-f-review']) {
    $(id).addEventListener('change', () => loadRegister());
  }
  function clearFilters() {
    clearTimeout(searchTimer);
    $('cms-f-q').value = '';
    for (const key of FILTER_KEYS) $('cms-f-' + key).value = '';
    $('cms-f-review').checked = false;
    $('cms-f-sort').value = 'created:desc';
  }
  $('cms-f-reset').addEventListener('click', () => { clearFilters(); loadRegister(); });
  $('cms-more').addEventListener('click', () => loadRegister(true));

  /* -------------------------------------------------------------- analytics */
  const AGREEMENT_FIELDS = [['category_agreement', 'التصنيف'], ['ministry_agreement', 'الجهة المختصة'], ['priority_agreement', 'الأولوية'],
    ['governorate_agreement', 'المحافظة'], ['region_agreement', 'المنطقة']];
  const REF_CHIPS = 4;                       // per top-correction row; the rest are counted
  async function loadAnalytics() {
    const gen = ++analyticsGen;
    $('cms-charts').classList.add('is-loading');
    try {
      const data = obj(await api('/complaints/analytics'));
      if (gen !== analyticsGen) return;
      analytics = data;
      hideError('cms-an-error');
      renderAnalytics();
    } catch (exc) {
      if (gen === analyticsGen) showError('cms-an-error', exc.message);
    } finally {
      if (gen === analyticsGen) $('cms-charts').classList.remove('is-loading');
    }
  }
  $('cms-an-refresh').addEventListener('click', () => loadAnalytics());

  function kpi(labelText, value, sub, flag) {
    const tile = el('div', 'cms-kpi');
    if (flag) tile.dataset.flag = flag;
    tile.append(el('div', 'cms-kpi-label', labelText), el('div', 'cms-kpi-value', value));
    if (sub) tile.append(el('div', 'cms-kpi-sub', sub));
    return tile;
  }
  function renderAnalytics() {
    const a = obj(analytics), t = obj(a.totals), s = obj(a.sla), mq = obj(a.model_quality);
    const updated = $('cms-an-updated');
    updated.replaceChildren();
    if (a.generated_at) appendText(updated, 'آخر تحديث: ' + fmtDateTime(a.generated_at));
    const agreements = AGREEMENT_FIELDS.map(([key]) => mq[key])
      .filter(v => v !== null && v !== undefined && Number.isFinite(Number(v))).map(Number);
    const agreement = agreements.length ? agreements.reduce((x, y) => x + y, 0) / agreements.length : null;
    const inFlight = (Number(t.queued) || 0) + (Number(t.processing) || 0);
    const outside = Number.isFinite(Number(t.outside_jurisdiction)) ? Number(t.outside_jurisdiction) : null;
    $('cms-kpis').replaceChildren(
      kpi('إجمالي الشكاوى', num(t.all || 0), [`مكتملة ${num(t.done || 0)}`, inFlight ? `قيد المعالجة ${num(inFlight)}` : '',
        t.error ? `تعذرت ${num(t.error)}` : ''].filter(Boolean).join(' · ')),
      kpi('المفتوحة', num(t.open || 0), `المغلقة ${num(t.closed || 0)}`),
      kpi('الحرجة المفتوحة', num(t.critical_open || 0), 'أولوية حرجة ولم تُغلق بعد', Number(t.critical_open) > 0 ? 'crit' : ''),
      kpi('المتأخرة عن المهلة', num(t.overdue || 0), `تقترب من موعدها خلال ٢٤ ساعة: ${num(s.due_soon || 0)}`, Number(t.overdue) > 0 ? 'crit' : ''),
      kpi('تحتاج مراجعة', num(t.needs_review || 0), `تمت مراجعة ${num(t.reviewed || 0)}`, Number(t.needs_review) > 0 ? 'warn' : ''),
      // Region survives only here: the count of complaints the entity must pass on to another region's emirate.
      kpi('خارج نطاق المنطقة', outside === null ? '—' : num(outside),
        `موقعها خارج ${jurisdictionLabel()}؛ تُحال لإمارة المنطقة المختصة`, outside > 0 ? 'warn' : ''),
      kpi('نسبة اتفاق المراجعين مع النموذج', agreement === null ? '—' : pct(agreement),
        Number(mq.reviewed) ? `متوسط التصنيف والجهة والأولوية والمحافظة والمنطقة · ${count(mq.reviewed, REVIEWS)}` : 'لا توجد مراجعات بعد'),
    );
    const rows = (source, kind, fallback) => arr(source).filter(r => r && r.id !== undefined && r.id !== null)
      .map(r => ({label: label(kind, r.id) || fallback || String(r.id), value: Number(r.count) || 0}))
      .sort((x, y) => y.value - x.value);
    const prioCounts = new Map(arr(a.by_priority).filter(r => r && r.id).map(r => [r.id, Number(r.count) || 0]));
    const priorityRows = [...new Set([...priorityOrder(), ...prioCounts.keys()])]
      .map(id => ({label: label('priorities', id), value: prioCounts.get(id) || 0, priority: id}));
    const slaRows = [['on_track', 'ضمن المهلة'], ['due_soon', 'تقترب من الموعد (٢٤ ساعة)'], ['overdue', 'متأخرة عن المهلة']]
      .map(([key, text]) => ({label: text, value: Number(s[key]) || 0}));
    const toneScope = el('section', 'cms-card cms-chart');
    toneScope.append(chartHead('نبرة الشكاوى والنطاق المتأثر', 'من تقدير النموذج للشكاوى المكتملة'));
    const split = el('div', 'cms-split');
    split.append(subChart('النبرة', rows(a.by_tone, 'tones')), subChart('النطاق المتأثر', rows(a.by_scope, 'scopes')));
    toneScope.append(split);
    $('cms-charts').replaceChildren(
      trendCard(a.trend),
      barCard('حسب التصنيف', rows(a.by_category, 'categories'), {column: 'التصنيف', sub: 'الشكاوى المكتملة المعالجة'}),
      barCard('حسب الجهة المختصة', rows(a.by_ministry, 'ministries'), {column: 'الجهة'}),
      barCard('توزيع الأولويات', priorityRows, {column: 'الأولوية', sort: false, sub: 'كل لون مقترن باسم الأولوية'}),
      barCard('حالة المهلة', slaRows, {column: 'الحالة', sort: false, sub: 'الشكاوى المفتوحة المكتملة المعالجة'}),
      barCard('حسب المحافظة', rows(a.by_governorate, 'governorates', 'غير محدد'), {column: 'المحافظة',
        sub: `محافظات ${jurisdictionLabel()} · الشكاوى المكتملة المعالجة`}),
      barCard('حسب حالة المتابعة', rows(a.by_status, 'statuses'), {column: 'الحالة'}),
      heatCard(a.category_priority),
      barCard('إشارات القواعد المكتشفة', arr(a.signals).filter(r => r && r.id).map(r => ({label: signalLabel(r.id), value: Number(r.count) || 0}))
        .sort((x, y) => y.value - x.value), {column: 'الإشارة', sub: 'عبارات في النص ترفع الحد الأدنى للأولوية'}),
      toneScope,
      qualityCard(mq, obj(a.processing), arr(a.providers)),
    );
    drawTrend();                          // synchronous: clientWidth forces layout, no frame needed
  }
  function chartHead(title, sub) {
    const head = el('div', 'cms-chart-head');
    head.append(el('h3', '', title));
    if (sub) head.append(el('span', 'cms-chart-sub', sub));
    return head;
  }
  /* A visually hidden table is every chart's accessible alternative. */
  function srTable(caption, headers, rows) {
    const table = el('table', 'cms-sr-only');
    table.append(el('caption', '', caption));
    const head = el('tr');
    for (const h of headers) { const th = el('th', '', h); th.scope = 'col'; head.append(th); }
    const thead = el('thead'); thead.append(head);
    const tbody = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      row.forEach((value, i) => { const c = el(i ? 'td' : 'th', '', value); if (!i) c.scope = 'row'; tr.append(c); });
      tbody.append(tr);
    }
    table.append(thead, tbody);
    return table;
  }
  function bars(data, {column = 'الفئة', scaleMax, caption, format = num} = {}) {
    const frag = document.createDocumentFragment();
    const total = data.reduce((sum, row) => sum + row.value, 0);
    if (!data.length || (!total && !scaleMax)) { frag.append(el('div', 'cms-chart-empty', 'لا توجد بيانات بعد.')); return frag; }
    let shown = data;
    if (data.length > 10) {                    // fold the long tail instead of 23 thin rows
      const rest = data.slice(9);
      shown = data.slice(0, 9).concat({label: `باقي الفئات (${num(rest.length)})`, value: rest.reduce((sum, row) => sum + row.value, 0)});
    }
    const max = scaleMax || Math.max(...shown.map(row => row.value), 1);
    const box = el('div', 'cms-bars');
    box.setAttribute('aria-hidden', 'true');
    for (const row of shown) {
      const line = el('div', 'cms-bar-row');
      const name = el('div', 'cms-bar-label');
      if (row.priority) { const sw = el('span', 'cms-swatch'); sw.dataset.priority = row.priority; name.append(sw); }
      name.append(el('span', '', row.label));
      name.title = row.label;
      const track = el('div', 'cms-bar-track');
      const f = row.value === null ? 0 : Math.max(0, Math.min(1, row.value / max));
      const bar = el('div', 'cms-bar');
      bar.style.width = (f * 100).toFixed(2) + '%';
      if (row.priority) bar.dataset.priority = row.priority;
      const value = el('span', 'cms-bar-value', row.value === null ? '—' : format(row.value));
      value.style.setProperty('inset-inline-start', (f * 100).toFixed(2) + '%');
      track.append(bar, value);
      line.append(name, track);
      hover(line, () => [row.value === null ? '—' : format(row.value),
        row.label + (total && !scaleMax ? ` · ${pct(row.value / total)} من الإجمالي` : '')]);
      box.append(line);
    }
    frag.append(box, srTable(caption || column, [column, 'القيمة'].concat(scaleMax ? [] : ['النسبة']),
      data.map(row => [row.label, row.value === null ? '—' : format(row.value)].concat(scaleMax ? [] : [total ? pct(row.value / total) : '—']))));
    return frag;
  }
  function barCard(title, data, {column, sub, sort = true} = {}) {
    const card = el('section', 'cms-card cms-chart');
    const rows = sort ? [...data].sort((x, y) => y.value - x.value) : data;
    card.append(chartHead(title, sub), bars(rows, {column, caption: title}));
    return card;
  }
  function subChart(title, data) {
    const box = el('div');
    box.append(el('h4', '', title), bars(data, {column: title, caption: title}));
    return box;
  }

  /* 30-day columns. Oldest day at the right — the reading start in RTL — and
     drawn at the container's pixel width so labels are never stretched. */
  function trendCard(trend) {
    const card = el('section', 'cms-card cms-chart is-wide');
    const data = arr(trend).filter(d => d && d.date).map(d => ({date: str(d.date), value: Number(d.count) || 0}));
    const total = data.reduce((sum, d) => sum + d.value, 0);
    const peak = data.reduce((best, d) => (d.value > (best ? best.value : -1) ? d : best), null);
    card.append(chartHead(`الشكاوى المستلمة يومياً — آخر ${num(data.length || 30)} يوماً`,
      data.length ? `الإجمالي ${num(total)}` + (peak && peak.value ? ` · الذروة ${num(peak.value)} في ${dayLabel(peak.date)}` : '') : ''));
    if (!data.length) { card.append(el('div', 'cms-chart-empty', 'لا توجد بيانات بعد.')); trendState = null; return card; }
    const box = el('div', 'cms-trend');
    box.tabIndex = 0;
    box.setAttribute('role', 'img');
    box.setAttribute('aria-label', `عدد الشكاوى المستلمة يومياً خلال آخر ${num(data.length)} يوماً: الإجمالي ${num(total)}` +
      (peak && peak.value ? `، وأعلى يوم ${dayLabel(peak.date)} بعدد ${num(peak.value)}` : '') + '. الجدول التالي يسرد القيم.');
    card.append(box, srTable('الشكاوى المستلمة يومياً', ['اليوم', 'العدد'], data.map(d => [dayLabel(d.date, true), num(d.value)])));
    trendState = {box, data, focus: -1, geometry: null};
    box.addEventListener('pointermove', event => trendHover(event.clientX));
    box.addEventListener('pointerleave', () => trendHighlight(-1));
    box.addEventListener('focus', () => trendHighlight(data.length - 1, true));
    box.addEventListener('blur', () => trendHighlight(-1));
    box.addEventListener('keydown', event => {
      const state = trendState;
      if (!state || state.box !== box) return;
      const step = {ArrowLeft: 1, ArrowRight: -1}[event.key];   // later days sit to the left
      let i = state.focus;
      if (step) i = Math.max(0, Math.min(data.length - 1, (i < 0 ? data.length - 1 : i) + step));
      else if (event.key === 'Home') i = 0;
      else if (event.key === 'End') i = data.length - 1;
      else return;
      event.preventDefault();
      trendHighlight(i, true);
    });
    return card;
  }
  function dayLabel(date, withYear) {
    const d = new Date(str(date) + 'T00:00:00Z');
    if (!Number.isFinite(d.getTime())) return str(date);
    try {
      return d.toLocaleDateString('ar-u-ca-gregory-nu-arab', {day: 'numeric', month: 'long', year: withYear ? 'numeric' : undefined, timeZone: 'UTC'});
    } catch (_) {
      return arDigits(str(date));
    }
  }
  function niceScale(max) {
    const raw = Math.max(1, max) / 4, magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map(m => m * magnitude).find(s => s >= raw) || magnitude * 10;
    const top = Math.max(step, Math.ceil(max / step) * step);
    const ticks = [];
    for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v);
    return {top, ticks};
  }
  const SVG = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, text) {
    const node = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function drawTrend() {
    const state = trendState;
    if (!state || !state.box.isConnected) return;
    const width = Math.round(state.box.clientWidth);
    if (!width) return;                                     // hidden view: redrawn when shown
    const data = state.data, height = 210;
    const m = {top: 22, bottom: 28, right: 34, left: 8};
    const plotW = width - m.right - m.left, plotH = height - m.top - m.bottom;
    const {top, ticks} = niceScale(Math.max(...data.map(d => d.value)));
    const slot = plotW / data.length, barW = Math.min(24, Math.max(2, slot * 0.64));
    const x = i => width - m.right - slot * (i + 0.5);
    const y = v => m.top + plotH - (v / top) * plotH;
    const chart = svg('svg', {viewBox: `0 0 ${width} ${height}`, width, height, 'aria-hidden': 'true', focusable: 'false', direction: 'ltr'});
    for (const t of ticks) {
      chart.append(svg('line', {x1: m.left, x2: width - m.right, y1: y(t), y2: y(t), class: t ? 'cms-gridline' : 'cms-baseline'}));
      chart.append(svg('text', {x: width - m.right + 6, y: y(t) + 4, class: 'cms-axis-text', 'text-anchor': 'start'}, num(t)));
    }
    const hoverLine = svg('line', {x1: 0, x2: 0, y1: m.top, y2: m.top + plotH, class: 'cms-hover-line', visibility: 'hidden'});
    chart.append(hoverLine);
    const cols = [];
    data.forEach((d, i) => {
      if (!d.value) { cols.push(null); return; }
      // Rounded 4px at the data end, square on the baseline.
      const x0 = x(i) - barW / 2, x1 = x(i) + barW / 2, yt = y(d.value), yb = y(0);
      const r = Math.min(4, barW / 2, yb - yt);
      const path = svg('path', {class: 'cms-col',
        d: `M${x0},${yb} L${x0},${yt + r} Q${x0},${yt} ${x0 + r},${yt} L${x1 - r},${yt} Q${x1},${yt} ${x1},${yt + r} L${x1},${yb} Z`});
      chart.append(path);
      cols.push(path);
    });
    // Direct labels only where the story is: the peak and the latest day.
    const peakIndex = data.reduce((best, d, i) => (d.value > data[best].value ? i : best), 0);
    for (const i of new Set([peakIndex, data.length - 1])) {
      if (data[i].value) chart.append(svg('text', {x: x(i), y: y(data[i].value) - 6, class: 'cms-value-text', 'text-anchor': 'middle'}, num(data[i].value)));
    }
    // Day labels walk back from today (always labelled), ≥110px apart so they never collide.
    const step = Math.max(1, Math.ceil(110 / slot));
    for (let i = data.length - 1; i >= 0; i -= step) {
      const newest = i === data.length - 1, oldest = i === 0;
      const anchor = newest ? 'start' : (oldest ? 'end' : 'middle');
      const tx = newest ? x(i) - barW / 2 : (oldest ? x(i) + barW / 2 : x(i));
      chart.append(svg('text', {x: tx, y: height - 8, class: 'cms-axis-text', 'text-anchor': anchor}, dayLabel(data[i].date)));
    }
    state.box.replaceChildren(chart);
    state.geometry = {x, y, slot, width, m, cols, hoverLine};
    if (state.focus >= 0) trendHighlight(state.focus, document.activeElement === state.box);
  }
  function trendHover(clientX) {
    const state = trendState;
    if (!state || !state.geometry) return;
    const rect = state.box.getBoundingClientRect(), g = state.geometry;
    const i = Math.floor((g.width - g.m.right - (clientX - rect.left)) / g.slot);
    trendHighlight(Math.max(0, Math.min(state.data.length - 1, i)), true);
  }
  function trendHighlight(i, show) {
    const state = trendState;
    if (!state || !state.geometry) return;
    const g = state.geometry;
    g.cols.forEach((col, k) => { if (col) col.classList.toggle('is-hot', k === i); });
    state.focus = i;
    if (i < 0 || !show) { g.hoverLine.setAttribute('visibility', 'hidden'); hideTip(); return; }
    g.hoverLine.setAttribute('x1', g.x(i));
    g.hoverLine.setAttribute('x2', g.x(i));
    g.hoverLine.setAttribute('visibility', 'visible');
    const rect = state.box.getBoundingClientRect(), d = state.data[i];
    showTip(rect.left + g.x(i), rect.top + g.y(d.value), d.value ? count(d.value, COMPLAINTS) : 'لا شكاوى', dayLabel(d.date, true));
  }
  if (window.ResizeObserver) {
    let timer = 0;
    new ResizeObserver(() => { clearTimeout(timer); timer = setTimeout(drawTrend, 60); }).observe($('cms-charts'));
  }

  function heatCard(source) {
    const card = el('section', 'cms-card cms-chart is-wide');
    card.append(chartHead('التصنيف × الأولوية', 'عدد الشكاوى المكتملة؛ كلما اشتد اللون زاد العدد'));
    const prios = priorityOrder();
    const rows = arr(source).filter(r => r && r.category).map(r => {
      const cells = prios.map(p => Number(r[p]) || 0);
      return {label: label('categories', r.category), cells, total: cells.reduce((x, y) => x + y, 0)};
    }).filter(r => r.total).sort((x, y) => y.total - x.total);
    if (!rows.length) { card.append(el('div', 'cms-chart-empty', 'لا توجد بيانات بعد.')); return card; }
    const max = Math.max(...rows.flatMap(r => r.cells), 1);
    const table = el('table', 'cms-heat');
    table.append(el('caption', 'cms-sr-only', 'عدد الشكاوى لكل تصنيف وأولوية'));
    const head = el('tr');
    const corner = el('th', '', 'التصنيف');
    corner.scope = 'col';
    head.append(corner);
    for (const p of prios) {
      const th = el('th');
      th.scope = 'col';
      const sw = el('span', 'cms-swatch');
      sw.dataset.priority = p;
      th.append(sw, document.createTextNode(label('priorities', p)));
      head.append(th);
    }
    const totalHead = el('th', '', 'الإجمالي');
    totalHead.scope = 'col';
    head.append(totalHead);
    const thead = el('thead');
    thead.append(head);
    const tbody = el('tbody');
    for (const row of rows) {
      const tr = el('tr');
      const th = el('th', '', row.label);
      th.scope = 'row';
      tr.append(th);
      row.cells.forEach((value, k) => {
        const td = el('td', value ? '' : 'is-zero', num(value));
        // One hue, deeper with the count; capped so the printed number keeps its contrast.
        if (value) td.style.background = `color-mix(in srgb, var(--cms-bar) ${Math.round(8 + 44 * value / max)}%, transparent)`;
        hover(td, () => [num(value), `${row.label} · ${label('priorities', prios[k])}`]);
        tr.append(td);
      });
      tr.append(el('td', 'is-total', num(row.total)));
      tbody.append(tr);
    }
    table.append(thead, tbody);
    const wrap = el('div', 'cms-heat-wrap');
    wrap.append(table);
    card.append(wrap);
    return card;
  }

  function fieldValueLabel(field, id) {
    if (id === null || id === undefined || id === '') return 'بدون';
    if (field === 'subcategory') return subLabel(id);
    return FIELD_KINDS[field] ? label(FIELD_KINDS[field], id) : String(id);
  }
  function qualityCard(mq, processing, providers) {
    const card = el('section', 'cms-card cms-chart is-wide');
    const reviewed = Number(mq.reviewed) || 0;
    card.append(chartHead('جودة النموذج وملاحظات المراجعين', reviewed ? `الشكاوى التي روجعت: ${num(reviewed)}` : 'لا توجد مراجعات بعد'));
    const grid = el('div', 'cms-grid-2');
    const left = el('div');
    left.append(el('h4', '', 'نسبة الاتفاق لكل حقل'));
    const agreementRows = AGREEMENT_FIELDS
      .map(([key, text]) => ({label: text, value: mq[key] === null || mq[key] === undefined || !Number.isFinite(Number(mq[key])) ? null : Number(mq[key])}));
    if (reviewed) left.append(bars(agreementRows, {column: 'الحقل', scaleMax: 1, caption: 'نسبة الاتفاق لكل حقل', format: pct}));
    else left.append(el('div', 'cms-chart-empty', 'تظهر النسب بعد أن يؤكد المراجعون التصنيف أو يصححوه.'));
    const right = el('div');
    right.append(el('h4', '', 'أكثر التصحيحات تكراراً'));
    const corrections = arr(mq.top_corrections).filter(c => c && c.field);
    const refsOf = c => arr(c.refs).filter(r => typeof r === 'string' && r.trim());
    const withRefs = corrections.some(c => refsOf(c).length);
    if (corrections.length) {
      const wrap = el('div', 'cms-heat-wrap');
      const table = el('table', 'cms-mini-table');
      const head = el('tr');
      for (const h of ['الحقل', 'من', 'إلى', 'العدد'].concat(withRefs ? ['الشكاوى'] : [])) { const th = el('th', '', h); th.scope = 'col'; head.append(th); }
      const thead = el('thead');
      thead.append(head);
      const tbody = el('tbody');
      for (const c of corrections) {
        const tr = el('tr');
        tr.append(el('td', '', FIELD_LABELS[c.field] || str(c.field)), el('td', '', fieldValueLabel(c.field, c.from)),
          el('td', '', fieldValueLabel(c.field, c.to)), el('td', '', num(c.count)));
        if (withRefs) {
          // The corrected complaints themselves, so a reviewer can see what the model keeps getting wrong.
          const refs = refsOf(c), chips = el('div', 'cms-refchips');
          refs.slice(0, REF_CHIPS).forEach(r => chips.append(refChip(r)));
          if (refs.length > REF_CHIPS) chips.append(el('span', 'cms-muted', `+${num(refs.length - REF_CHIPS)}`));
          tr.append(cell(refs.length ? chips : el('span', 'cms-muted', '—')));
        }
        tbody.append(tr);
      }
      table.append(thead, tbody);
      wrap.append(table);
      right.append(wrap);
    } else right.append(el('div', 'cms-chart-empty', 'لا توجد تصحيحات مسجلة.'));
    const kv = el('dl', 'cms-kv');
    const add = (k, v) => { kv.append(el('dt', '', k)); const dd = el('dd'); if (v instanceof Node) dd.append(v); else dd.textContent = v; kv.append(dd); };
    add('متوسط زمن المعالجة', [['avg_total_s', 'الإجمالي'], ['avg_ocr_s', 'استخراج النص'], ['avg_structure_s', 'الهيكلة'], ['avg_classify_s', 'التصنيف']]
      .map(([key, text]) => `${text} ${seconds(processing[key])}`).join(' · '));
    const used = providers.filter(p => p && p.id);
    if (used.length) add('النماذج المستخدمة', used.map(p => `${providerLabel(p.id)}: ${num(p.count)}`).join(' · '));
    right.append(kv);
    grid.append(left, right);
    card.append(grid);
    return card;
  }

  /* --------------------------------------------------------------- insights */
  $('cms-insights-run').addEventListener('click', async () => {
    const run = $('cms-insights-run'), status = $('cms-insights-status');
    run.disabled = true;
    hideError('cms-insights-error');
    status.replaceChildren(el('span', 'cms-spinner'), document.createTextNode(' جارٍ توليد الرؤى من الإحصاءات… قد يستغرق ذلك دقيقة أو أكثر.'));
    try {
      insightsResult = obj(await api('/complaints/insights', {method: 'POST', json: {}}));
      renderInsights();
      status.replaceChildren();
      appendText(status, [insightsResult.generated_at ? 'وُلّدت في ' + fmtDateTime(insightsResult.generated_at) : '',
        insightsResult.provider ? 'النموذج: ' + providerLabel(insightsResult.provider) : ''].filter(Boolean).join(' · '));
    } catch (exc) {
      status.replaceChildren();
      showError('cms-insights-error', exc.message);
    } finally {
      run.disabled = false;
    }
  });
  function refChip(ref) {
    const chip = el('button', 'cms-refchip');
    chip.type = 'button';
    chip.append(ltr(ref));
    chip.title = 'فتح الشكوى ' + ref;
    chip.addEventListener('click', () => openRef(ref));
    return chip;
  }
  function openRef(ref) {
    const match = /^[A-Z]+-\d{4}-(\d+)$/.exec(str(ref).trim());    // CMP-{YYYY}-{id:06d}
    if (match) { openComplaint(Number(match[1]), {ref: str(ref).trim()}); return; }
    $('cms-f-q').value = str(ref);
    showView('register');
  }
  function renderInsights() {
    const body = $('cms-insights-body'), data = obj(insightsResult && insightsResult.insights);
    body.replaceChildren();
    if (data.headline) body.append(textEl('p', 'cms-headline', data.headline));
    const insights = arr(data.insights).filter(i => i && (i.title || i.detail));
    if (insights.length) {
      const listNode = el('ol', 'cms-insight-list');
      for (const item of insights) {
        const li = el('li', 'cms-insight');
        li.append(textEl('strong', '', item.title), textEl('p', '', item.detail));
        const refs = arr(item.refs).filter(r => typeof r === 'string' && r);
        if (refs.length) { const chips = el('div'); refs.forEach(r => chips.append(refChip(r))); li.append(chips); }
        listNode.append(li);
      }
      body.append(listNode);
    }
    const recs = arr(data.recommendations).filter(r => r && r.action);
    if (recs.length) {
      body.append(el('div', 'cms-subhead', 'توصيات للجهات'));
      const wrap = el('div', 'cms-heat-wrap');
      const table = el('table', 'cms-mini-table');
      const head = el('tr');
      for (const h of ['الجهة', 'التوصية', 'الأولوية']) { const th = el('th', '', h); th.scope = 'col'; head.append(th); }
      const thead = el('thead');
      thead.append(head);
      const tbody = el('tbody');
      for (const rec of recs) {
        const tr = el('tr');
        tr.append(el('td', '', label('ministries', rec.ministry) || '—'), cell(textEl('span', '', rec.action)), cell(priorityBadge(rec.priority)));
        tbody.append(tr);
      }
      table.append(thead, tbody);
      wrap.append(table);
      body.append(wrap);
    }
    const watch = arr(data.watch).filter(w => typeof w === 'string' && w);
    if (watch.length) {
      body.append(el('div', 'cms-subhead', 'للمتابعة'));
      const ul = el('ul', 'cms-watch');
      watch.forEach(w => ul.append(textEl('li', '', w)));
      body.append(ul);
    }
    if (!body.childNodes.length) body.append(el('div', 'cms-chart-empty', 'لم يُرجع النموذج رؤى لهذه البيانات.'));
  }

  /* ----------------------------------------------------------------- detail */
  /* A complaint opens in place, as an accordion: a full-width row right under
     its register row holds #cms-detail — one panel, one complaint at a time.
     Collapsed, the panel waits hidden where ui.html declares it. The panel is
     sized to the register's visible width by CSS (100cqi of its scroll box);
     where container units are missing, a ResizeObserver sets the width. */
  const panel = $('cms-detail'), panelHome = panel.parentNode;
  const CONTAINER_UNITS = !!(window.CSS && typeof window.CSS.supports === 'function' && window.CSS.supports('width', '1cqi'));
  let widthObserver = null, widthFrame = 0, panelWidth = 0;
  let heightFrame = 0;
  const smoothScroll = () => !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function rowFor(id) { return $('cms-rows').querySelector(`tr.cms-rrow[data-id="${Number(id)}"]`); }
  function triggerFor(id) { const row = rowFor(id); return row ? row.querySelector('.cms-rtoggle') : null; }
  function panelShown() { return expandedId !== null && !$('cms-register').hidden; }
  /* The open row is highlighted and says so (aria-expanded); a panel whose row
     the current filters leave out stays on top of the list with a note. */
  function markRows() {
    for (const row of $('cms-rows').querySelectorAll('tr.cms-rrow')) {
      const on = Number(row.dataset.id) === expandedId;
      row.classList.toggle('is-expanded', on);
      const toggle = row.querySelector('.cms-rtoggle');
      if (toggle) toggle.setAttribute('aria-expanded', String(on));
    }
    const detached = expandedId !== null && !rowFor(expandedId);
    $('cms-d-note').hidden = !detached;
    if (panelRow) panelRow.classList.toggle('is-detached', detached);
  }
  /* The element that scrolls the register vertically: the workspace on wide
     screens, the document where the app lets the page scroll (≤ 1100 px). */
  function scrollerOf(node) {
    for (let n = node && node.parentNode; n && n.nodeType === 1; n = n.parentNode) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY || '') && n.scrollHeight > n.clientHeight + 1) return n;
    }
    return document.scrollingElement || document.documentElement;
  }
  /* Put `node` back where it was on screen (its top was `before`) after the rows above it changed. */
  function keepTop(node, before) {
    if (before === null || !node || !node.isConnected) return;
    const delta = node.getBoundingClientRect().top - before;
    if (Math.abs(delta) >= 1) scrollerOf(node).scrollTop += delta;
  }
  /* Scroll one box so `target` is centred in it, or just in sight ('nearest'). */
  function scrollWithin(box, target, block) {
    const b = box.getBoundingClientRect(), t = target.getBoundingClientRect(), view = box.clientHeight;
    let delta = 0;
    if (block === 'center') delta = t.top + t.height / 2 - (b.top + view / 2);
    else if (t.top < b.top) delta = t.top - b.top;
    else if (t.bottom > b.top + view) delta = Math.min(t.top - b.top, t.bottom - b.top - view);
    if (Math.abs(delta) < 1) return;
    const top = box.scrollTop + delta;
    if (typeof box.scrollTo === 'function') box.scrollTo({top, behavior: smoothScroll() ? 'smooth' : 'auto'});
    else box.scrollTop = top;
  }
  /* scrollIntoView without leaving the panel: a citation must move the text
     viewer (and, stacked on a phone, the panel body), never the page. */
  function revealInPanel(target, block) {
    let inner = target, how = block;
    for (let n = target.parentNode; n && n.nodeType === 1 && n !== panel.parentNode; n = n.parentNode) {
      if (!/(auto|scroll)/.test(getComputedStyle(n).overflowY || '') || !(n.scrollHeight > n.clientHeight + 1)) continue;
      scrollWithin(n, inner, how);
      inner = n;                    // an outer scroller only has to show the inner one
      how = 'nearest';
    }
  }
  function syncWidth() {
    widthFrame = 0;
    const frame = panelRow && panelRow.querySelector('.cms-panel-frame');
    const width = Math.floor(root.querySelector('.cms-table-wrap').clientWidth || 0);
    if (!frame || width <= 0 || width === panelWidth) return;
    panelWidth = width;
    frame.style.setProperty('--cms-panel-w', width + 'px');
  }
  function watchWidth() {
    if (CONTAINER_UNITS) return;
    panelWidth = 0;
    syncWidth();
    if (!window.ResizeObserver) return;
    // Written in the next frame: a width set inside the callback could resize the table again (an RO loop).
    widthObserver = new window.ResizeObserver(() => {
      if (!window.requestAnimationFrame) syncWidth();
      else if (!widthFrame) widthFrame = window.requestAnimationFrame(syncWidth);
    });
    widthObserver.observe(root.querySelector('.cms-table-wrap'));
  }
  function unwatchWidth() {
    if (widthObserver) { widthObserver.disconnect(); widthObserver = null; }
    if (widthFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(widthFrame);
    widthFrame = 0;
    panelWidth = 0;
  }
  /* The panel takes what the register's scroller shows below its row, so its
     bottom edge is on screen as soon as it opens: a wheel over the PDF frame
     does not scroll the page on. The CSS min(85dvh, 960px) is the fallback. */
  function fitHeight() {
    heightFrame = 0;
    if (!panelRow || !panelRow.isConnected) return;
    let box = null;
    for (let n = panelRow.parentNode; n && n.nodeType === 1; n = n.parentNode) {
      if (/(auto|scroll)/.test(getComputedStyle(n).overflowY || '') && n.scrollHeight > n.clientHeight + 1) { box = n; break; }
    }
    const row = rowFor(expandedId);
    const room = Math.floor((box ? box.clientHeight : window.innerHeight || 0)
                            - (row ? row.getBoundingClientRect().height : 0) - 24);   // the 12px scroll margin + 12px below
    if (room > 0) panel.style.setProperty('--cms-panel-h', Math.max(320, Math.min(960, room)) + 'px');
  }
  function onResize() {
    if (!window.requestAnimationFrame) fitHeight();
    else if (!heightFrame) heightFrame = window.requestAnimationFrame(fitHeight);
  }
  function mountPanel(row) {
    const tbody = $('cms-rows');
    panelRow = el('tr', 'cms-detail-row');
    const td = el('td');
    td.colSpan = root.querySelectorAll('.cms-table thead th').length || 9;
    const frame = el('div', 'cms-panel-frame');
    frame.append(panel);
    td.append(frame);
    panelRow.append(td);
    tbody.insertBefore(panelRow, row ? row.nextSibling : tbody.firstChild);
    panel.hidden = false;
    root.querySelector('.cms-table-wrap').hidden = false;
    markRows();
    watchWidth();
    if (window.addEventListener) window.addEventListener('resize', onResize);
  }
  /* Bring the open row and its panel into view: the row's height is the
     frame's scroll margin, so the row stays visible above the panel header. */
  function revealPanel() {
    const frame = panelRow && panelRow.querySelector('.cms-panel-frame');
    if (!frame || !frame.isConnected || typeof frame.scrollIntoView !== 'function') return;
    const row = rowFor(expandedId);
    frame.style.scrollMarginTop = Math.round((row ? row.getBoundingClientRect().height : 0) + 12) + 'px';
    fitHeight();
    frame.scrollIntoView({block: 'start', inline: 'nearest', behavior: smoothScroll() ? 'smooth' : 'auto'});
  }
  function toggleDetail(id) {
    if (expandedId === Number(id)) collapse();
    else expand(id);
  }
  function expand(id) {
    id = Number(id);
    if (!Number.isInteger(id)) return;
    if (expandedId !== id) {
      const row = rowFor(id), before = row ? row.getBoundingClientRect().top : null;
      if (expandedId !== null) collapse({focus: false});
      keepTop(row, before);            // the panel closing above must not pull this row off screen
      expandedId = id;
      mountPanel(row);
      loadDetail(id);
    }
    $('cms-d-title').focus({preventScroll: true});      // the heading names the complaint; Tab goes on to its actions
    revealPanel();
  }
  function collapse({focus = true} = {}) {
    if (expandedId === null) return;
    const id = expandedId;
    expandedId = null;
    detailGen++;
    resetDetail();                     // aborts the file fetch, revokes its blob: URL, empties the panel
    unwatchWidth();
    if (window.removeEventListener) window.removeEventListener('resize', onResize);
    if (heightFrame && window.cancelAnimationFrame) window.cancelAnimationFrame(heightFrame);
    heightFrame = 0;
    panel.style.setProperty('--cms-panel-h', '');      // '' removes it: back to the CSS fallback
    hideTip();
    panel.hidden = true;
    panelHome.append(panel);
    if (panelRow) { panelRow.remove(); panelRow = null; }
    $('cms-d-title').textContent = 'تفاصيل الشكوى';
    markRows();
    if (!register.items.length) root.querySelector('.cms-table-wrap').hidden = true;
    if (focus) {
      const target = triggerFor(id) || $('cms-rows').querySelector('.cms-rtoggle') || $('cms-f-q');
      target.focus();
    }
  }
  /* Every other way in (the intake list, the insights and top-correction ref
     chips): switch to the register, find the row — searched by its reference
     when the current list does not have it — and expand it there. */
  async function openComplaint(id, {ref = ''} = {}) {
    id = Number(id);
    if (!Number.isInteger(id)) return;
    const gen = ++openGen;
    if (view !== 'register') showView('register');
    await registerSettled();
    if (gen !== openGen) return;
    if (!rowFor(id) && ref && expandedId !== id) {
      clearFilters();
      $('cms-f-q').value = ref;
      await loadRegister();
      if (gen !== openGen) return;
    }
    expand(id);
  }
  async function loadDetail(id) {
    const gen = ++detailGen;
    resetDetail();
    // The row already knows the subject: the heading names the complaint while the rest loads.
    const item = register.items.find(i => Number(i.id) === id);
    const title = $('cms-d-title');
    title.replaceChildren();
    appendText(title, (item && (str(item.subject) || str(item.filename))) || 'جارٍ تحميل الشكوى…');
    title.dir = 'auto';
    const meta = $('cms-d-meta'), ref = el('span', 'cms-d-ref');
    ref.append(ltr((item && str(item.ref)) || '#' + id));
    meta.append(ref, el('span', 'cms-muted', 'جارٍ تحميل التفاصيل…'));
    try {
      const data = obj(await api(itemPath(id)));
      if (gen !== detailGen) return;
      detail = data;
      renderDetail(true);
    } catch (exc) {
      if (gen !== detailGen) return;
      $('cms-d-title').textContent = 'تعذر تحميل الشكوى';
      meta.replaceChildren(ref);
      showError('cms-d-error', exc.message);
    }
  }
  async function reloadDetail() {
    if (!detail || expandedId === null) return;
    const gen = detailGen, id = detail.id;
    try {
      const data = obj(await api(itemPath(id)));
      if (gen !== detailGen || expandedId === null) return;
      detail = data;
      renderDetail(false);
    } catch (exc) {
      if (gen === detailGen) showError('cms-d-error', exc.message);
    }
  }
  /* While the panel shows an item that is still processing, follow its stage. */
  async function followDetail() {
    if (!detail || expandedId === null || !ACTIVE.has(detail.stage)) return;
    const stage = latestStage.get(Number(detail.id));
    // An item no list fetched this tick (older than the intake window) is asked for directly.
    if (!stage || stage !== detail.stage) await reloadDetail();
  }
  function releaseFile() {
    if (fileAbort) { fileAbort.abort(); fileAbort = null; }
    if (fileUrl) { URL.revokeObjectURL(fileUrl); fileUrl = null; }
    fileUrlId = null;
    $('cms-v-file').replaceChildren();
  }
  function resetDetail() {
    releaseFile();
    detail = null;
    ocr = null;
    ocrKey = null;
    pendingPage = 0;
    fieldEdits.clear();
    settledFields = null;
    for (const id of ['cms-d-meta', 'cms-d-info', 'cms-ocr', 'cms-d-status']) $(id).replaceChildren();
    hideError('cms-d-error');
    for (const id of ['cms-d-reprocess', 'cms-d-reocr', 'cms-d-json', 'cms-d-delete']) $(id).disabled = true;
    $('cms-d-reocr').hidden = true;
    const download = root.querySelector('.cms-vtabs a');
    if (download) download.remove();
  }
  $('cms-d-collapse').addEventListener('click', () => collapse());
  // Esc folds the panel — but not from a field (a text box, a list, an open field editor keeps its Esc).
  panel.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || expandedId === null) return;
    const t = event.target;
    if (t && (['input', 'textarea', 'select'].includes(t.localName) || t.isContentEditable)) return;
    event.preventDefault();
    collapse();
  });

  /* The detail panel is rebuilt after every save. What the reviewer had in hand
     survives it: the focused control (by its data-focus key — the one just used
     is usually disabled by then, so a save names it) and unsaved input in the
     other form (every data-focus control whose value differs from what it was
     built with), except in the form just saved, which shows the stored values. */
  const builtValue = new WeakMap();
  function formControl(node, key) {
    node.dataset.focus = key;
    builtValue.set(node, node.value);
    return node;
  }
  function captureDetail(saved) {
    const info = $('cms-d-info'), active = document.activeElement;
    const drafts = new Map();
    for (const node of info.querySelectorAll('[data-focus]')) {
      if (!builtValue.has(node) || (saved && node.dataset.focus.startsWith(saved + '.'))) continue;
      if (node.value !== builtValue.get(node)) drafts.set(node.dataset.focus, node.value);
    }
    const inside = !!active && active !== info && info.contains(active);
    // Focus is only put back when it was in the panel, or lost to <body> (a
    // disabled save button drops it): never pulled from the viewer or the header.
    const lost = !active || active === document.body || inside;
    return {drafts, focus: inside && active.dataset ? active.dataset.focus || '' : '', lost, scrollTop: info.scrollTop};
  }
  function restoreDetail(kept, focusKey) {
    const info = $('cms-d-info');
    for (const [key, value] of kept.drafts) {
      const node = info.querySelector(`[data-focus="${key}"]`);
      if (!node) continue;
      node.value = value;
      node.dispatchEvent(new Event('change'));     // dependent fields (subcategories, the ministry hint) follow
    }
    info.scrollTop = kept.scrollTop;
    const key = kept.lost ? focusKey || kept.focus : '';
    const target = key ? info.querySelector(`[data-focus="${key}"]`) : null;
    if (target) target.focus({preventScroll: true});
  }
  function renderDetail(first, {saved = '', focus = ''} = {}) {
    const kept = first ? null : captureDetail(saved);
    const d = detail, stage = str(d.stage), done = stage === 'done';
    const meta = $('cms-d-meta');
    meta.replaceChildren();
    const ref = el('span', 'cms-d-ref');
    ref.append(ltr(str(d.ref) || '#' + d.id));
    meta.append(ref);
    if (done) {
      meta.append(priorityBadge(d.priority), tag(label('statuses', d.status) || '—'));
      const info = sla(d);
      if (info) meta.append(el('span', 'cms-sla' + (info.tone === 'ok' ? '' : ' is-' + info.tone), info.text));
    } else meta.append(stageTag(stage));
    if (d.needs_review) meta.append(tag('تحتاج مراجعة', 'warn'));
    else if (d.reviewed) meta.append(tag('روجعت', 'ok'));
    if (d.created_at) { const when = el('span'); when.append('استُلمت ', dateNode(d.created_at, true)); meta.append(when); }
    const title = $('cms-d-title');
    title.replaceChildren();
    appendText(title, str(d.subject) || str(d.filename) || 'شكوى دون موضوع');
    title.dir = 'auto';

    const processing = ACTIVE.has(stage);
    $('cms-d-reprocess').disabled = processing;
    // A TXT upload has no extraction step to repeat: the plain reprocess is the same thing.
    $('cms-d-reocr').hidden = !d.file_available || fileKind(d) === 'txt';
    $('cms-d-reocr').disabled = processing;
    $('cms-d-json').disabled = false;
    $('cms-d-delete').disabled = processing;

    const analysis = obj(d.analysis);
    const info = $('cms-d-info');
    info.replaceChildren();
    if (!done) info.append(processingBlock(d));
    if (done && d.needs_review) info.append(reviewBlock(analysis));
    if (done || d.analysis) {
      // Only a finished complaint takes a field review (the server answers 409 otherwise).
      const pending = done ? pendingFields(analysis) : [];
      info.append(summaryBlock(d, analysis));
      if (pending.length || (done && settledFields === Number(d.id))) info.append(pendingBlock(d, analysis, pending));
      info.append(fieldsBlock(d, analysis, pending), classificationBlock(d, analysis));
    }
    if (done) info.append(feedbackBlock(d), statusBlock(d));
    if (typeof d.acknowledgment === 'string' && d.acknowledgment.trim()) info.append(ackBlock(d.acknowledgment));
    info.append(processingInfoBlock(d, analysis), historyBlock(d));
    if (first) info.scrollTop = 0;
    else restoreDetail(kept, focus);

    const tabs = root.querySelector('.cms-vtabs');
    if (d.file_available && !tabs.querySelector('a')) {
      const link = el('a', '', 'تنزيل الملف الأصلي');
      link.href = itemPath(d.id, '/file');
      link.download = '';
      tabs.append(link);
    }
    renderOcr();
    if (first) selectViewer(d.file_available && fileKind(d) !== 'txt' ? 'file' : 'text');
    else if (viewerTab === 'file') ensureFile();
  }
  function block(title, cls) {
    const section = el('section', 'cms-block' + (cls ? ' ' + cls : ''));
    if (title) section.append(el('h4', '', title));      // under the panel's <h3>, itself under the tab's <h2>
    return section;
  }
  function processingBlock(d) {
    const stage = str(d.stage);
    const section = block(stage === 'error' ? 'تعذرت معالجة الشكوى' : 'الشكوى قيد المعالجة', stage === 'error' ? 'is-crit' : '');
    const job = session.get(Number(d.id));
    section.append(stepper(stage, job && job.lastStage));
    if (stage === 'error') section.append(textEl('p', 'cms-job-note is-error', str(d.error) || 'تعذرت معالجة الشكوى.'));
    else section.append(el('p', 'cms-muted', 'تظهر البيانات والتصنيف هنا بعد اكتمال المعالجة؛ تتحدث الصفحة تلقائياً.'));
    return section;
  }
  function reviewBlock(analysis) {
    const section = block('تحتاج مراجعة بشرية', 'is-warn');
    const reasons = arr(analysis.review_reasons).filter(r => typeof r === 'string' && r);
    const chips = el('div', 'cms-chips');
    (reasons.length ? reasons : ['']).forEach(r => chips.append(tag(r ? reasonLabel(r) : 'لم تُذكر أسباب', 'warn')));
    section.append(chips);
    return section;
  }
  function validSource(source) {
    return !!source && typeof source === 'object' && Number(source.page) >= 1 && Number(source.line) >= 1;
  }
  function citeButton(source) {
    if (!validSource(source)) return null;
    const node = el('button', 'cms-cite', `مصدر: صفحة ${num(source.page)} · سطر ${num(source.line)} ⤴`);
    node.type = 'button';
    if (source.quote) node.title = str(source.quote);
    node.addEventListener('click', () => showCitation(source));
    return node;
  }
  /* Where the text probably says what the model worded its own way (an
     `approx` span): the same jump and highlight as a citation, a humbler label. */
  function nearButton(source, text) {
    if (!validSource(source)) return null;
    const node = el('button', 'cms-cite', text);
    node.type = 'button';
    node.title = `صفحة ${num(source.page)} · سطر ${num(source.line)}` + (source.quote ? ': ' + str(source.quote) : '');
    node.addEventListener('click', () => showCitation(source));
    return node;
  }
  function summaryBlock(d, analysis) {
    const structured = obj(analysis.structured);
    const section = block('الملخص');
    if (structured.is_complaint === false) section.append(tag('لا يبدو هذا المستند شكوى', 'warn'));
    const summary = str(d.summary) || str(structured.summary);
    const p = textEl('p', 'cms-summary', summary || 'لا يوجد ملخص.');
    p.dir = 'auto';
    if (!summary) p.classList.add('cms-muted');
    section.append(p);
    const facts = arr(structured.key_facts).filter(f => typeof f === 'string' && f.trim());
    if (facts.length) {
      section.append(el('div', 'cms-subhead', 'أبرز الوقائع'));
      const ul = el('ul', 'cms-facts');
      facts.forEach(f => ul.append(textEl('li', '', f)));
      section.append(ul);
    }
    const warnings = arr(analysis.warnings).filter(w => typeof w === 'string' && w && !RETIRED_WARNINGS.test(w));
    if (warnings.length) {
      const ul = el('ul', 'cms-warnings');
      warnings.forEach(w => ul.append(textEl('li', '', w)));
      section.append(ul);
    }
    return section;
  }
  function fieldName(f) { return str(f.label_ar) || FIELD_NAMES[f.key] || str(f.key); }
  function valueBox(key, value) {
    const box = el('div', 'cms-fvalue');
    const text = str(value);
    if (!text.trim()) { box.classList.add('is-empty'); box.textContent = 'غير مذكور'; }
    else if (LTR_FIELDS.has(key)) box.append(ltr(text));
    else { appendText(box, text); box.dir = 'auto'; }
    return box;
  }
  /* Structured fields pass no `verified`: an unmatched value waits in «تحتاج
     مراجعة» and comes back with the reviewer's badge. Only reference numbers,
     which take no review, are still marked «غير موثّق». */
  function fieldCard(labelText, value, {key, verified, source, near, review, wide} = {}) {
    const card = el('div', 'cms-fcard' + (wide ? ' is-wide' : ''));
    card.append(el('div', 'cms-flabel', labelText), valueBox(key, value));
    const text = str(value);
    const extra = el('div', 'cms-fmeta');
    const cite = review && review.action === 'change' ? null : citeButton(source);   // a changed value is not the cited one
    if (cite) extra.append(cite);
    else if (text.trim() && verified === false) {
      const flag = el('span', 'cms-unverified', 'غير موثّق');
      flag.title = 'لم يُعثر على هذه القيمة في النص المستخرج؛ تحقق منها في المستند.';
      extra.append(flag);
    } else {
      const place = nearButton(near, 'موضعها المحتمل في النص ⤴');
      if (place) extra.append(place);
    }
    if (review) extra.append(reviewBadge(review));
    if (extra.childNodes.length) card.append(extra);
    return card;
  }
  /* The reviewer's decision on a field, with their name; a change keeps the
     model's value in the tooltip (and for screen readers, which skip titles). */
  function reviewBadge(review) {
    const changed = review.action === 'change';
    const badge = tag(changed ? 'عدّلها المراجع' : 'قبِلها المراجع', changed ? 'accent' : 'ok');
    const who = str(review.reviewer).trim();
    if (who) appendText(badge, ' · ' + who);
    if (changed) {
      badge.title = 'القيمة السابقة: ' + (str(review.from).trim() || 'فارغة');
      badge.append(el('span', 'cms-sr-only', ` (${badge.title})`));
    }
    return badge;
  }
  // The addressee is the entity the letter was sent to, not the complainant nor the party complained about.
  function elsewhereNote() {
    const note = el('div', 'cms-hint');
    note.append(tag('موجّه إلى جهة أخرى', 'warn'), document.createTextNode(`الخطاب غير موجّه إلى ${entityName('الجهة المستقبلة')}؛ صُنّف وأُحيل مع ذلك.`));
    return note;
  }
  function fieldsBlock(d, analysis, pending = []) {
    const structured = obj(analysis.structured);
    const section = block('بيانات مقدم الشكوى والواقعة');
    const grid = el('div', 'cms-fields');
    const waiting = new Set(pending.map(f => f.key));        // shown in «تحتاج مراجعة» until reviewed
    const fields = arr(structured.fields).filter(f => f && f.key && !waiting.has(f.key));
    if (!fields.length) grid.append(el('div', 'cms-muted', waiting.size ? 'الحقول المستخرجة كلها في «تحتاج مراجعة» أعلاه.' : 'لا توجد حقول مستخرجة.'));
    const elsewhere = structured.addressed_to_entity === false;
    let addressee = null;
    for (const f of fields) {
      const card = fieldCard(fieldName(f), f.value, {key: f.key, source: f.source, near: f.near_source,
        review: f.review && typeof f.review === 'object' ? f.review : null,
        wide: ['addressed_to', 'against_entity', 'requested_action', 'incident_location'].includes(f.key)});
      if (f.key === 'addressed_to') addressee = card;
      grid.append(card);
    }
    if (elsewhere && !waiting.has('addressed_to')) {         // a pending addressee carries the note in its row
      const note = elsewhereNote();
      if (addressee) addressee.append(note);
      else { const card = fieldCard(FIELD_NAMES.addressed_to, '', {wide: true}); card.append(note); grid.prepend(card); }
    }
    if (structured.governorate || d.governorate || list('governorates').length) grid.append(placeCard(d, 'governorate', obj(structured.governorate), 'المحافظة'));
    grid.append(placeCard(d, 'region', obj(structured.region), 'المنطقة', outsideJurisdiction(d, analysis.review_reasons)));
    section.append(grid);
    const refs = arr(structured.reference_numbers).filter(r => r && str(r.value).trim());
    if (refs.length) {
      section.append(el('div', 'cms-subhead', 'الأرقام المرجعية الواردة'));
      const refGrid = el('div', 'cms-fields');
      refs.forEach(r => refGrid.append(fieldCard('رقم مرجعي', r.value, {key: 'national_id', verified: r.verified, source: r.source})));
      section.append(refGrid);
    }
    return section;
  }
  /* Governorate or region, with how it was decided — or the reviewer's correction. */
  function placeCard(d, field, placed, labelText, outside) {
    const card = fieldCard(labelText, label(FIELD_KINDS[field], d[field] || placed.id) || '');
    const how = correctionNote(d, field, d['model_' + field]) || PLACE_SOURCES[placed.source];
    if (how) card.append(how instanceof Node ? how : el('div', 'cms-hint', how));
    if (outside) { const meta = el('div', 'cms-fmeta'); meta.append(outsideTag()); card.append(meta); }
    return card;
  }

  /* Field review. A value the model gave in its own words — not found verbatim
     in the text, so probably true but paraphrased — waits in «تحتاج مراجعة»
     until the reviewer accepts it as is or changes it (POST /items/{id}/fields). */
  function pendingFields(analysis) {
    return arr(obj(analysis.structured).fields).filter(f => f && f.key && f.pending === true);
  }
  const fieldSaved = key => 'fields.' + str(key).replace(/[^\w-]/g, '_');
  const fieldFocus = (key, part) => fieldSaved(key) + '.' + part;
  /* Once the last pending field is settled the heading gives way to a status
     line, which takes the focus; it stays until the panel is expanded again. */
  function pendingBlock(d, analysis, pending) {
    if (!pending.length) {
      const section = block('', 'cms-pending is-settled');
      const line = el('p', 'cms-pending-done', 'رُوجعت كل الحقول المعلّقة، وتظهر الآن ضمن بيانات مقدم الشكوى والواقعة.');
      line.tabIndex = -1;
      line.dataset.focus = 'fields.done';
      section.append(line);
      return section;
    }
    const section = block('تحتاج مراجعة', 'cms-pending');
    section.append(el('p', 'cms-muted', 'قيم استخرجها النموذج بصياغته ولم تُطابق نص المستند حرفياً؛ اقبلها إن كانت صحيحة أو عدّلها.'));
    const elsewhere = obj(analysis.structured).addressed_to_entity === false;
    const listNode = el('ul', 'cms-pending-list');
    for (const f of pending) listNode.append(pendingRow(d, f, pending, elsewhere && f.key === 'addressed_to'));
    section.append(listNode);
    return section;
  }
  /* One pending field: «قبول» / «تعديل», or — while fieldEdits holds it — an
     input with «حفظ» / «إلغاء» (Enter saves, Esc cancels). Switching modes swaps
     only this row; a panel rebuild keeps the editor and its text. */
  function pendingRow(d, f, pending, elsewhere) {
    const key = str(f.key), name = fieldName(f), editing = fieldEdits.has(key);
    const li = el('li', 'cms-prow' + (editing ? ' is-editing' : ''));
    li.dataset.field = key;
    const main = el('div', 'cms-prow-main'), actions = el('div', 'cms-prow-actions');
    const message = el('span', 'cms-prow-msg');
    message.setAttribute('role', 'status');
    let input = null, busy = false;
    const control = (text, cls, part, spoken, onClick) => {
      const node = button(text, cls, onClick);
      node.dataset.focus = fieldFocus(key, part);
      node.setAttribute('aria-label', spoken);        // «قبول» alone does not say which field
      return node;
    };
    const swap = part => {
      if (!li.parentNode) return;
      const fresh = pendingRow(d, f, pending, elsewhere);
      li.parentNode.replaceChild(fresh, li);
      const target = fresh.querySelector(`[data-focus="${fieldFocus(key, part)}"]`);
      if (target) { target.focus(); if (part === 'value') target.select(); }
    };
    const edit = () => { fieldEdits.set(key, str(f.value)); swap('value'); };
    const cancel = () => { fieldEdits.delete(key); swap('edit'); };
    async function submit(action) {
      if (busy) return;
      const json = {key, action};
      if (action === 'change') {
        const value = input.value.trim();
        if (value === str(f.value).trim()) json.action = 'accept';     // saved unchanged: accepted as is
        else json.value = value;
      }
      const named = $('cms-d-info').querySelector('[data-focus="feedback.reviewer"]');
      const reviewer = named ? named.value.trim() : '';
      if (reviewer) { json.reviewer = reviewer; store('cms.reviewer', reviewer); }
      busy = true;
      const back = document.activeElement;
      for (const b of li.querySelectorAll('button')) b.disabled = true;
      if (input) input.readOnly = true;
      message.textContent = 'جارٍ الحفظ…';
      const before = pending.map(p => str(p.key));
      try {
        const data = obj(await api(itemPath(d.id, '/fields'), {method: 'POST', json}));
        if (detail && Number(detail.id) === Number(d.id)) {
          fieldEdits.delete(key);
          detail = {...data, acknowledgment: data.acknowledgment === undefined ? detail.acknowledgment : data.acknowledgment};
          // On to the next row still pending (or the last one left), else the status line.
          const after = pendingFields(obj(detail.analysis)).map(p => str(p.key));
          const next = before.slice(before.indexOf(key) + 1).find(k => after.includes(k)) || after[after.length - 1];
          if (!after.length) settledFields = Number(d.id);
          renderDetail(false, {saved: fieldSaved(key), focus: next ? fieldFocus(next, fieldEdits.has(next) ? 'value' : 'accept') : 'fields.done'});
          // The section shrank under the kept scroll position: bring the new focus into view.
          const landed = document.activeElement;
          if (landed && $('cms-d-info').contains(landed)) revealInPanel(landed, 'nearest');
        }
        announce(json.action === 'accept' ? 'قُبلت القيمة' : 'حُفظ التعديل');
        refreshAfterChange();
      } catch (exc) {
        busy = false;
        for (const b of li.querySelectorAll('button')) b.disabled = false;
        if (input) input.readOnly = false;
        message.textContent = exc.message;
        if (!li.isConnected) showError('cms-d-error', exc.message);   // the panel was rebuilt meanwhile
        else if (back && back.isConnected && typeof back.focus === 'function') back.focus();
      }
    }
    if (editing) {
      const wrap = el('label', 'cms-field');
      wrap.append(el('span', '', name));
      input = el('input');
      input.type = 'text';
      input.maxLength = 500;
      input.dir = LTR_FIELDS.has(key) ? 'ltr' : 'auto';
      input.value = fieldEdits.get(key);
      formControl(input, fieldFocus(key, 'value'));
      input.addEventListener('input', () => fieldEdits.set(key, input.value));
      input.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); submit('change'); }
        // Esc belongs to the row here: the panel must not take it as a request to fold.
        else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); cancel(); }
      });
      wrap.append(input);
      main.append(wrap);
      actions.append(control('حفظ', 'cms-btn cms-btn-sm cms-btn-primary', 'save', 'حفظ ' + name, () => submit('change')),
        control('إلغاء', 'cms-btn cms-btn-sm', 'cancel', 'إلغاء تعديل ' + name, cancel));
    } else {
      main.append(el('div', 'cms-flabel', name), valueBox(key, f.value));
      actions.append(control('قبول', 'cms-btn cms-btn-sm', 'accept', 'قبول ' + name, () => submit('accept')),
        control('تعديل', 'cms-btn cms-btn-sm', 'edit', 'تعديل ' + name, edit));
    }
    const place = nearButton(f.near_source, 'موضعها المحتمل في النص ⤴');
    if (place) { const meta = el('div', 'cms-fmeta'); meta.append(place); main.append(meta); }
    if (elsewhere) main.append(elsewhereNote());
    main.append(message);
    li.append(main, actions);
    return li;
  }
  function kvRow(dl, key, ...values) {
    dl.append(el('dt', '', key));
    const dd = el('dd');
    for (const v of values) if (v !== null && v !== undefined && v !== '') dd.append(v instanceof Node ? v : document.createTextNode(String(v)));
    if (!dd.childNodes.length) dd.append(el('span', 'cms-muted', '—'));
    dl.append(dd);
  }
  function correctionNote(d, field, modelValue) {
    if (!d.reviewed || modelValue === undefined || modelValue === null || modelValue === d[field]) return null;
    return el('span', 'cms-hint', `صحّحه المراجع — القيمة الآلية: ${fieldValueLabel(field, modelValue)}`);
  }
  function classificationBlock(d, analysis) {
    const c = obj(analysis.classification);
    const section = block('التصنيف والإحالة والأولوية');
    const dl = el('dl', 'cms-kv');
    const category = d.category || c.category, sub = d.subcategory === undefined ? c.subcategory : d.subcategory;
    kvRow(dl, 'التصنيف', label('categories', category), sub ? ' · ' + subLabel(sub) : '', correctionNote(d, 'category', d.model_category));
    // The entity receives every complaint; the ministry is where it refers it.
    const ministry = d.ministry || c.ministry, usual = defaultMinistry(category);
    kvRow(dl, 'الإحالة إلى', label('ministries', ministry),
      usual && ministry && usual !== ministry ? el('span', 'cms-hint', 'الجهة المعتادة للتصنيف: ' + label('ministries', usual)) : '',
      correctionNote(d, 'ministry', d.model_ministry));
    const floors = arr(c.floors_applied).filter(Boolean).map(id => signalLabel(id, (arr(c.signals).find(s => s && s.id === id) || {}).label_ar));
    const source = c.priority_source === 'rule_floor'
      ? el('span', 'cms-hint', 'رفعتها القواعد' + (floors.length ? ': ' + floors.join('، ') : '') +
        (c.model_priority ? ` — تقدير النموذج: ${label('priorities', c.model_priority)}` : ''))
      : c.priority_source === 'rule_cap'
        // The text addresses the model: its «critical» is held one level down until a reviewer confirms it.
        ? el('span', 'cms-hint', 'أبقتها القواعد دون «' + label('priorities', (list('priorities')[0] || {}).id || 'critical') +
          '» لحين المراجعة (النص يخاطب النظام)' + (c.model_priority ? ` — تقدير النموذج: ${label('priorities', c.model_priority)}` : ''))
        : (c.priority_source === 'llm' ? el('span', 'cms-hint', 'تقدير النموذج') : '');
    const corrected = correctionNote(d, 'priority', d.model_priority);
    kvRow(dl, 'الأولوية', priorityBadge(d.priority || c.priority), corrected || source);
    const factors = el('span', 'cms-chips');
    arr(c.priority_factors).filter(Boolean).forEach(f => factors.append(tag(label('factors', f))));
    kvRow(dl, 'عوامل الأولوية', factors.childNodes.length ? factors : '');
    kvRow(dl, 'النطاق المتأثر', label('scopes', c.affected_scope));
    kvRow(dl, 'نبرة الشكوى', label('tones', c.tone));
    kvRow(dl, 'ثقة النموذج', CONFIDENCE[c.confidence] || str(c.confidence));
    // The count covers every other complaint with this ID, later ones included — not only earlier ones.
    if (Number(c.repeat_count) > 0) kvRow(dl, 'شكاوى أخرى بنفس رقم الهوية', tag(num(c.repeat_count), 'warn'));
    const reasons = arr(analysis.review_reasons).filter(r => typeof r === 'string' && r);
    if (reasons.length) {
      // Still listed after a review, so the reviewer's decision keeps its context.
      const chips = el('span', 'cms-chips');
      reasons.forEach(r => chips.append(tag(reasonLabel(r), d.needs_review ? 'warn' : '')));
      kvRow(dl, 'أسباب المراجعة', chips);
    }
    section.append(dl);
    if (c.rationale) {
      section.append(el('div', 'cms-subhead', 'مبررات النموذج'));
      const p = textEl('p', 'cms-rationale', c.rationale);
      p.dir = 'auto';
      section.append(p);
    }
    // Every quote the model gave: a verified one cites the document (an item stored
    // before the flag existed was verified), the others keep the model's wording.
    const evidence = arr(c.evidence).filter(e => e && str(e.quote).trim());
    if (evidence.length) {
      section.append(el('div', 'cms-subhead', 'الأدلة من النص'));
      for (const e of evidence) section.append(e.verified === false ? modelQuoteBox(e.quote, e.source) : quoteBox(e.quote, e.source));
    }
    const signals = arr(c.signals).filter(s => s && s.id);
    if (signals.length) {
      section.append(el('div', 'cms-subhead', 'إشارات القواعد'));
      for (const s of signals) {
        const head = el('div', 'cms-chips');
        head.append(tag(signalLabel(s.id, s.label_ar), 'accent'));
        if (s.floor) head.append(el('span', 'cms-hint', 'الحد الأدنى للأولوية: ' + label('priorities', s.floor)));
        section.append(head);
        arr(s.quotes).filter(q => q && (q.quote || validSource(q))).forEach(q => section.append(quoteBox(q.quote, q)));
      }
    }
    return section;
  }
  function quoteBox(quote, source) {
    const box = el('div');
    const q = textEl('blockquote', 'cms-quote', str(quote) || '—');
    q.dir = 'auto';
    box.append(q);
    const cite = citeButton(source);
    if (cite) box.append(cite);
    return box;
  }
  /* A quote not found verbatim, in the model's wording: muted, with a quiet
     label (not a warning) and the place the text probably says it, if found. */
  function modelQuoteBox(quote, source) {
    const box = el('div');
    const q = textEl('blockquote', 'cms-quote is-model', str(quote));
    q.dir = 'auto';
    const meta = el('div', 'cms-fmeta');
    const note = tag('بصياغة النموذج');
    note.title = 'لم يرد هذا الاقتباس بحروفه في النص؛ هذه صياغة النموذج.';
    meta.append(note);
    const place = nearButton(source, 'موضعه المحتمل ⤴');
    if (place) meta.append(place);
    box.append(q, meta);
    return box;
  }
  function processingInfoBlock(d, analysis) {
    const section = block('المعالجة');
    const dl = el('dl', 'cms-kv');
    const provider = str(d.provider) || str(analysis.provider);
    kvRow(dl, 'النموذج', provider ? providerLabel(provider) : '', d.model || analysis.model ? el('span', 'cms-hint', str(d.model) || str(analysis.model)) : '');
    const timings = {...obj(analysis.timings), ...obj(d.timings)};
    const parts = Object.entries(timings).filter(([, v]) => Number.isFinite(Number(v)))
      .map(([k, v]) => `${TIMING_LABELS[k] || k} ${seconds(v)}`);
    kvRow(dl, 'الأزمنة', parts.join(' · '));
    kvRow(dl, 'المصدر', d.source === 'text' ? 'نص ملصق' : (str(d.filename) ? textEl('span', '', d.filename) : ''),
      Number(d.page_count) ? ` · ${count(d.page_count, PAGES)}` : '');
    if (d.processed_at) kvRow(dl, 'اكتملت في', dateNode(d.processed_at, true));
    if (Number(d.attempts) > 1) kvRow(dl, 'المحاولات', num(d.attempts));
    section.append(dl);
    return section;
  }

  function selectField(key, labelText, options, value, emptyText) {
    const wrap = el('label', 'cms-field');
    wrap.append(el('span', '', labelText));
    const select = el('select');
    select.name = key;
    if (emptyText !== undefined) select.add(new Option(emptyText, ''));
    else if (!value) select.add(new Option('— غير محدد —', ''));   // never let an empty value read as the first option
    for (const item of options) select.add(new Option(str(item.label_ar) || String(item.id), item.id));
    // An id the taxonomy no longer knows is still shown, so the reviewer sees what is stored.
    if (value && ![...select.options].some(o => o.value === value)) select.add(new Option(String(value), value));
    select.value = value || '';
    wrap.append(select);
    return {wrap, select};
  }
  function feedbackBlock(d) {
    const section = block('تقييم المراجع');
    section.append(el('p', 'cms-muted', 'أكّد تصنيف النموذج أو صحّحه؛ تُحفظ التصحيحات وتُستخدم أمثلةً في تصنيف الشكاوى اللاحقة.'));
    const form = el('div', 'cms-form');
    const category = selectField('category', 'التصنيف', list('categories'), str(d.category));
    const subcategory = selectField('subcategory', 'التصنيف الفرعي', [], str(d.subcategory), '— بدون تصنيف فرعي —');
    const ministry = selectField('ministry', 'الإحالة إلى', list('ministries'), str(d.ministry));
    const priority = selectField('priority', 'الأولوية', list('priorities'), str(d.priority));
    const governorate = selectField('governorate', 'المحافظة', list('governorates'), str(d.governorate));
    const region = selectField('region', 'المنطقة', list('regions'), str(d.region));
    const hint = el('div', 'cms-hint');
    function fillSubcategories() {
      const current = subcategory.select.value;
      const cat = find('categories', category.select.value);
      subcategory.select.replaceChildren(new Option('— بدون تصنيف فرعي —', ''));
      for (const sub of arr(cat && cat.subcategories).filter(s => s && s.id)) subcategory.select.add(new Option(str(sub.label_ar) || sub.id, sub.id));
      subcategory.select.value = [...subcategory.select.options].some(o => o.value === current) ? current : '';
    }
    function updateHint() {
      hint.replaceChildren();
      const usual = defaultMinistry(category.select.value);
      if (usual && usual !== ministry.select.value) {
        hint.append(document.createTextNode('الجهة المعتادة لهذا التصنيف: ' + label('ministries', usual)));
        hint.append(button('اعتمادها', 'cms-link', () => { ministry.select.value = usual; updateHint(); }));
      }
    }
    subcategory.select.value = str(d.subcategory);
    fillSubcategories();
    subcategory.select.value = [...subcategory.select.options].some(o => o.value === str(d.subcategory)) ? str(d.subcategory) : '';
    category.select.addEventListener('change', () => { fillSubcategories(); updateHint(); });
    ministry.select.addEventListener('change', updateHint);
    // Every governorate lies in the entity's region, so naming one settles the region too.
    governorate.select.addEventListener('change', () => {
      const e = entity(), value = governorate.select.value;
      if (value && value !== 'unknown' && e && e.region && [...region.select.options].some(o => o.value === e.region)) region.select.value = e.region;
    });
    // …and a place in another region — or an unknown one — has none of its
    // governorates (the server refuses a governorate with either).
    region.select.addEventListener('change', () => {
      const e = entity(), value = region.select.value;
      if (value && e && e.region && value !== e.region
          && [...governorate.select.options].some(o => o.value === 'unknown')) governorate.select.value = 'unknown';
    });
    if (!list('governorates').length && !d.governorate) governorate.wrap.hidden = true;   // a taxonomy without governorates
    updateHint();
    ministry.wrap.append(hint);
    const note = el('label', 'cms-field cms-wide');
    note.append(el('span', '', 'ملاحظة (اختياري)'));
    const noteInput = el('textarea');
    noteInput.rows = 2;
    noteInput.maxLength = 1000;
    noteInput.dir = 'auto';
    note.append(noteInput);
    const reviewer = el('label', 'cms-field cms-wide');
    reviewer.append(el('span', '', 'اسم المراجع (اختياري)'));
    const reviewerInput = el('input');
    reviewerInput.type = 'text';
    reviewerInput.maxLength = 100;
    reviewerInput.dir = 'auto';
    reviewerInput.value = recall('cms.reviewer') || '';
    reviewer.append(reviewerInput);
    form.append(category.wrap, subcategory.wrap, ministry.wrap, priority.wrap, governorate.wrap, region.wrap, note, reviewer);
    const actions = el('div', 'cms-form-actions');
    const status = el('span', 'cms-muted');
    status.setAttribute('role', 'status');
    const confirmButton = button('تأكيد التصنيف', 'cms-btn');
    const saveButton = button('حفظ التصحيح', 'cms-btn cms-btn-primary');
    const selects = {category: category.select, subcategory: subcategory.select, ministry: ministry.select, priority: priority.select,
      governorate: governorate.select, region: region.select};
    for (const [key, select] of Object.entries(selects)) formControl(select, 'feedback.' + key);
    formControl(noteInput, 'feedback.note');
    formControl(reviewerInput, 'feedback.reviewer');
    formControl(confirmButton, 'feedback.confirm');
    formControl(saveButton, 'feedback.correct');
    async function submit(verdict) {
      const changes = {};
      for (const [key, select] of Object.entries(selects)) if (select.value !== str(d[key])) changes[key] = select.value;
      // Fields the reviewer touched (against the form as built, which may already
      // differ from d — e.g. a subcategory the category does not list) that now
      // differ from what is stored.
      const edited = Object.keys(changes).filter(key => selects[key].value !== builtValue.get(selects[key]));
      if (verdict === 'correct' && !Object.keys(changes).length) { status.textContent = 'لم تغيّر أي حقل؛ استخدم «تأكيد التصنيف» إن كان التصنيف صحيحاً.'; return; }
      if (verdict === 'confirm' && edited.length) {
        // «تأكيد التصنيف» after editing the form must never drop the edits silently:
        // saving them is a correction, and confirming as-is means putting them back.
        const names = edited.map(key => FIELD_LABELS[key] || key).join('، ');
        if (!window.confirm(`غيّرت في النموذج: ${names}.\nهل تريد حفظ هذه التعديلات تصحيحاً؟ (إلغاء يُبقي النموذج كما هو دون حفظ)`)) {
          status.textContent = 'لم يُحفظ شيء. احفظ تعديلاتك بـ«حفظ التصحيح»، أو أعد الحقول إلى قيمها ثم أكّد التصنيف.';
          return;
        }
        verdict = 'correct';
      }
      if (verdict === 'confirm') for (const key of Object.keys(changes)) delete changes[key];
      confirmButton.disabled = saveButton.disabled = true;
      status.textContent = 'جارٍ الحفظ…';
      const name = reviewerInput.value.trim();
      store('cms.reviewer', name);
      try {
        const data = obj(await api(itemPath(d.id, '/feedback'), {method: 'POST',
          json: {verdict, changes, note: noteInput.value.trim(), reviewer: name}}));
        if (detail && Number(detail.id) === Number(d.id)) {
          detail = {...data, acknowledgment: data.acknowledgment === undefined ? detail.acknowledgment : data.acknowledgment};
          renderDetail(false, {saved: 'feedback', focus: 'feedback.' + verdict});
          reloadDetail();                   // the reply draft names the ministry and priority: fetch it fresh
        }
        announce(verdict === 'confirm' ? 'أُكّد التصنيف.' : 'حُفظ التصحيح.');
        refreshAfterChange();
      } catch (exc) {
        status.textContent = exc.message;
        confirmButton.disabled = saveButton.disabled = false;
      }
    }
    confirmButton.addEventListener('click', () => submit('confirm'));
    saveButton.addEventListener('click', () => submit('correct'));
    actions.append(confirmButton, saveButton, status);
    section.append(form, actions);
    const history = arr(d.feedback);
    if (history.length) section.append(el('div', 'cms-hint', `سُجّل ${count(history.length, RATINGS)} لهذه الشكوى؛ انظر السجل أدناه.`));
    return section;
  }
  function statusBlock(d) {
    const section = block('حالة المتابعة');
    const form = el('div', 'cms-form');
    const status = selectField('status', 'الحالة', list('statuses'), str(d.status));
    const note = el('label', 'cms-field');
    note.append(el('span', '', 'ملاحظة (اختياري)'));
    const input = el('input');
    input.type = 'text';
    input.maxLength = 1000;
    input.dir = 'auto';
    note.append(input);
    form.append(status.wrap, note);
    const actions = el('div', 'cms-form-actions');
    const message = el('span', 'cms-muted');
    message.setAttribute('role', 'status');
    const save = button('تحديث الحالة', 'cms-btn cms-btn-primary', async () => {
      if (status.select.value === str(d.status) && !input.value.trim()) { message.textContent = 'اختر حالة مختلفة أو أضف ملاحظة.'; return; }
      save.disabled = true;
      message.textContent = 'جارٍ التحديث…';
      try {
        const data = obj(await api(itemPath(d.id, '/status'), {method: 'POST', json: {status: status.select.value, note: input.value.trim()}}));
        if (detail && Number(detail.id) === Number(d.id)) {
          detail = {...data, acknowledgment: data.acknowledgment === undefined ? detail.acknowledgment : data.acknowledgment};
          renderDetail(false, {saved: 'status', focus: 'status.save'});
        }
        announce('حُدّثت الحالة: ' + label('statuses', status.select.value));
        refreshAfterChange();
      } catch (exc) {
        message.textContent = exc.message;
        save.disabled = false;
      }
    });
    formControl(status.select, 'status.status');
    formControl(input, 'status.note');
    formControl(save, 'status.save');
    actions.append(save, message);
    section.append(form, actions);
    return section;
  }
  function ackBlock(text) {
    const section = block('رد مقترح لمقدم الشكوى');
    const box = textEl('p', 'cms-ack', text);
    box.dir = 'auto';
    const message = el('span', 'cms-muted');
    message.setAttribute('role', 'status');
    const copy = button('نسخ', 'cms-btn cms-btn-sm', async () => {
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); ok = true; }
      } catch (_) { ok = false; }
      if (!ok) {
        // The Clipboard API needs a secure context (the app opened by its LAN address over http is not one): copy from a hidden field.
        const back = document.activeElement;
        const area = el('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.cssText = 'position:fixed;opacity:0;inset-inline-start:0;top:0';
        root.append(area);
        area.select();
        try { ok = document.execCommand('copy'); } catch (_) { ok = false; }
        area.remove();
        if (back && back.isConnected && typeof back.focus === 'function') back.focus({preventScroll: true});
      }
      message.textContent = ok ? 'نُسخ الرد.' : 'تعذر النسخ؛ حدّد النص وانسخه يدوياً.';
    });
    const row = el('div', 'cms-form-actions');
    row.append(copy, message);
    section.append(box, row);
    return section;
  }
  function historyBlock(d) {
    const section = block('سجل الشكوى');
    const names = new Map(arr(obj(obj(d.analysis).structured).fields).filter(f => f && f.key).map(f => [str(f.key), fieldName(f)]));
    const feedback = arr(d.feedback).filter(Boolean);
    const entries = [];
    for (const f of feedback) entries.push({at: f.created_at, kind: 'feedback', feedback: f});
    for (const e of arr(d.events).filter(Boolean)) {
      if (e.kind === 'feedback' && feedback.length) continue;     // the feedback list has the richer record
      entries.push({at: e.at, kind: str(e.kind), detail: e.detail});
    }
    entries.sort((x, y) => (Date.parse(y.at) || 0) - (Date.parse(x.at) || 0));
    if (!entries.length) { section.append(el('p', 'cms-muted', 'لا توجد أحداث مسجلة.')); return section; }
    const ol = el('ol', 'cms-timeline');
    for (const entry of entries) {
      const li = el('li');
      li.dataset.kind = entry.kind;
      const time = el('span', 'cms-time');
      time.append(dateNode(entry.at, true));
      li.append(time);
      if (entry.feedback) {
        const f = entry.feedback;
        li.append(el('strong', '', f.verdict === 'confirm' ? 'أكّد المراجع التصنيف' : (f.verdict === 'correct' ? 'صحّح المراجع التصنيف' : 'تقييم المراجع')));
        for (const [field, change] of Object.entries(obj(f.changes))) {
          const c = obj(change);
          li.append(el('span', 'cms-change', `${FIELD_LABELS[field] || field}: من «${fieldValueLabel(field, c.from)}» إلى «${fieldValueLabel(field, c.to)}»`));
        }
        if (f.note) li.append(textEl('span', 'cms-change', 'ملاحظة: ' + f.note));
        if (f.reviewer) li.append(textEl('span', 'cms-change', 'المراجع: ' + f.reviewer));
      } else {
        li.append(el('strong', '', EVENT_LABELS[entry.kind] || entry.kind || 'حدث'));
        const extra = eventDetail(entry.kind, entry.detail, names);
        if (extra) li.append(textEl('span', 'cms-change', extra));
      }
      ol.append(li);
    }
    section.append(ol);
    return section;
  }
  function eventDetail(kind, raw, names) {
    let info = raw;
    if (typeof info === 'string') { try { info = JSON.parse(info); } catch (_) { return kind === 'error' ? info : ''; } }
    info = obj(info);
    if (kind === 'stage') return STAGE_LABELS[info.stage] || str(info.stage);
    if (kind === 'status') {
      const to = info.to || info.status;
      return [to ? label('statuses', to) : '', info.note ? 'ملاحظة: ' + str(info.note) : ''].filter(Boolean).join(' — ');
    }
    if (kind === 'error') return str(info.message || info.error);
    if (kind === 'reprocess') return info.interrupted ? 'أُعيدت إلى الطابور بعد توقف الخادم أثناء معالجتها' : (info.ocr ? 'مع إعادة استخراج النص' : '');
    if (kind === 'created') return info.duplicate ? 'مرفوع سابقاً' : '';
    if (kind === 'processed') return [info.priority ? 'الأولوية: ' + label('priorities', info.priority) : '', info.provider ? providerLabel(info.provider) : ''].filter(Boolean).join(' · ');
    if (kind === 'field_review') {
      const name = (names && names.get(str(info.key))) || FIELD_NAMES[info.key] || str(info.key);
      const quoted = v => (str(v).trim() ? `«${str(v).trim()}»` : 'قيمة فارغة');
      return info.action === 'change' ? `${name}: من ${quoted(info.from)} إلى ${quoted(info.to)}` : `${name}: قُبلت القيمة ${quoted(info.from)}`;
    }
    return '';
  }

  $('cms-d-reprocess').addEventListener('click', event => { if (detail) reprocess(detail.id, false, event.currentTarget); });
  $('cms-d-reocr').addEventListener('click', event => { if (detail) reprocess(detail.id, true, event.currentTarget); });
  $('cms-d-json').addEventListener('click', () => {
    if (!detail) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(detail, null, 2)], {type: 'application/json;charset=utf-8'}));
    const anchor = el('a');
    anchor.href = url;
    anchor.download = `complaint-${String(detail.ref || detail.id).replace(/[^\w.-]+/g, '_')}.json`;
    root.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  /* A deleted complaint folds, its row goes, and the focus moves to the next
     row (the previous one after the last row; the search box once none is left). */
  $('cms-d-delete').addEventListener('click', async event => {
    if (!detail) return;
    const d = detail, id = Number(d.id), trigger = event.currentTarget;
    if (!window.confirm(`حذف الشكوى ${d.ref || '#' + d.id} وملفها نهائياً؟ لا يمكن التراجع عن الحذف.`)) return;
    trigger.disabled = true;
    try {
      await api(itemPath(d.id), {method: 'DELETE'});
      session.delete(id);
      saveSession();
      intakeItems = intakeItems.filter(item => Number(item.id) !== id);
      const rows = [...$('cms-rows').querySelectorAll('tr.cms-rrow')], at = rows.indexOf(rowFor(id));
      const next = at >= 0 ? rows[at + 1] || rows[at - 1] : null;
      const nextId = next ? Number(next.dataset.id) : null;
      if (expandedId === id) collapse({focus: false});
      const kept = register.items.filter(item => Number(item.id) !== id);
      if (kept.length < register.items.length) register.total = Math.max(0, register.total - 1);
      register.items = kept;
      renderRegister();
      (triggerFor(nextId) || $('cms-f-q')).focus();
      announce(`حُذفت الشكوى ${d.ref || ''}`);
      renderIntake();
      refreshAfterChange();
    } catch (exc) {
      showError('cms-d-error', exc.message);
      trigger.disabled = false;
      if (focusLost() && trigger.isConnected) trigger.focus({preventScroll: true});
    }
  });

  /* ------------------------------------------------------ document viewer */
  const viewerTabs = [$('cms-v-file-tab'), $('cms-v-text-tab')];
  function selectViewer(tab) {
    viewerTab = tab;
    viewerTabs.forEach(button => {
      const on = button.getAttribute('aria-controls') === 'cms-v-' + tab;
      button.setAttribute('aria-selected', String(on));
      button.tabIndex = on ? 0 : -1;
      $(button.getAttribute('aria-controls')).hidden = !on;
    });
    if (tab === 'file') ensureFile();
  }
  viewerTabs.forEach((tab, index) => {
    tab.addEventListener('click', () => selectViewer(index ? 'text' : 'file'));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const next = viewerTabs[event.key === 'Home' ? 0 : (event.key === 'End' ? 1 : 1 - index)];
      selectViewer(next === viewerTabs[0] ? 'file' : 'text');
      next.focus();
    });
  });
  function fileKind(d) {
    const kind = str(d.file_kind);
    if (kind) return kind;
    const name = str(d.filename).toLowerCase();
    if (name.endsWith('.pdf')) return 'pdf';
    if (name.endsWith('.txt')) return 'txt';
    return /\.(png|jpe?g|webp|tiff?|bmp|gif)$/.test(name) ? 'image' : '';
  }
  function imageType(contentType, name) {
    const type = str(contentType).split(';')[0].trim().toLowerCase();
    if (/^image\/(png|jpeg|webp|gif|bmp|tiff)$/.test(type)) return type;
    const ext = (/\.([a-z]+)$/i.exec(str(name)) || [])[1] || '';
    return {png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', bmp: 'image/bmp', tif: 'image/tiff', tiff: 'image/tiff'}[ext.toLowerCase()] || 'application/octet-stream';
  }
  function viewerMessage(text, withDownload) {
    const box = el('div', 'cms-empty', text);
    if (withDownload && detail) {
      const link = el('a', '', 'تنزيل الملف الأصلي');
      link.href = itemPath(detail.id, '/file');
      link.download = '';
      box.append(el('br'), link);
    }
    return box;
  }
  async function ensureFile() {
    const panel = $('cms-v-file'), d = detail;
    if (!d) return;
    if (!d.file_available) {
      panel.replaceChildren(viewerMessage(d.source === 'text' ? 'أُدخلت هذه الشكوى نصاً ملصقاً؛ لا يوجد ملف أصلي. راجع «النص المستخرج».' : 'الملف الأصلي غير متاح.'));
      return;
    }
    const kind = fileKind(d);
    if (fileUrlId === d.id && (fileUrl || panel.childNodes.length)) {
      // Already loaded: only move the PDF to the page of the last citation.
      const frame = panel.querySelector('iframe');
      // Page 1 counts too: after a page-3 citation, a page-1 one must move the viewer back.
      if (frame && fileUrl && pendingPage >= 1 && frame.src !== fileUrl + '#page=' + pendingPage) frame.src = fileUrl + '#page=' + pendingPage;
      return;
    }
    if (fileAbort) fileAbort.abort();
    const controller = new AbortController(), gen = detailGen;
    fileAbort = controller;
    fileUrlId = d.id;
    panel.replaceChildren(el('div', 'cms-empty', 'جارٍ تحميل المستند…'));
    try {
      let response;
      try {
        response = await fetch(itemPath(d.id, '/file'), {signal: controller.signal, cache: 'no-store'});
      } catch (exc) {
        if (exc && exc.name === 'AbortError') throw exc;
        throw new ApiError(NETWORK_ERROR, 0);
      }
      if (!response.ok) throw new ApiError(errorText(await readJson(response), response.status, itemPath(d.id, '/file')), response.status);
      const blob = await response.blob();
      if (gen !== detailGen || controller.signal.aborted) return;
      if (kind === 'txt') {
        const pre = el('pre', 'cms-plain');
        pre.textContent = new TextDecoder('utf-8').decode(await blob.arrayBuffer());
        panel.replaceChildren(pre);
        return;
      }
      if (kind !== 'pdf' && kind !== 'image') { panel.replaceChildren(viewerMessage('لا يمكن عرض هذا النوع من الملفات هنا.', true)); return; }
      // The blob's type is fixed by what the record says, never by the response:
      // a blob: URL runs in this origin, so it must never become an HTML document.
      const type = kind === 'pdf' ? 'application/pdf' : imageType(response.headers.get('content-type'), d.filename);
      fileUrl = URL.createObjectURL(new Blob([blob], {type}));
      if (kind === 'pdf') {
        const frame = el('iframe');
        frame.title = 'المستند الأصلي';
        frame.src = fileUrl + (pendingPage >= 1 ? '#page=' + pendingPage : '');
        panel.replaceChildren(frame);
      } else {
        const img = el('img');
        img.alt = 'صورة المستند الأصلي';
        img.addEventListener('error', () => panel.replaceChildren(viewerMessage('تعذر عرض الصورة في المتصفح (قد تكون بصيغة TIFF). راجع «النص المستخرج» أو نزّل الملف.', true)));
        img.src = fileUrl;
        panel.replaceChildren(img);
      }
    } catch (exc) {
      if (exc && exc.name === 'AbortError') return;
      if (gen !== detailGen) return;
      fileUrlId = null;
      panel.replaceChildren(el('div', 'cms-error', exc.message));
    } finally {
      if (fileAbort === controller) fileAbort = null;
    }
  }

  /* The OCR text, numbered exactly like provenance.py: a "--- Page N ---" line
     starts page N and is not itself numbered; the lines after it count from 1,
     blank lines included; text before any marker is page 1. Offsets are
     UTF-16, i.e. plain indices into this JavaScript string. */
  function indexText(text) {
    const lines = text.split('\n');
    const starts = [], pages = [], numbers = [], marker = [], at = {};
    let offset = 0, page = 1, k = 0;
    lines.forEach((line, i) => {
      starts.push(offset);
      offset += line.length + 1;
      const m = PAGE_MARK.exec(line);
      if (m) {
        page = parseInt(m[1].replace(/[٠-٩]/g, c => c.charCodeAt(0) - 0x660).replace(/[۰-۹]/g, c => c.charCodeAt(0) - 0x6F0), 10);
        k = 0;
      } else {
        k += 1;
        at[page + ':' + k] = i;
      }
      marker.push(!!m);
      pages.push(page);
      numbers.push(m ? 0 : k);
    });
    return {text, lines, starts, pages, numbers, marker, at, nodes: [], hits: []};
  }
  function lineAt(offset) {
    let lo = 0, hi = ocr.starts.length - 1;
    while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (ocr.starts[mid] <= offset) lo = mid; else hi = mid - 1; }
    return lo;
  }
  function lineText(line, a, b) {
    const span = el('span', 'cms-ltext');
    if (a === undefined) return appendText(span, line);
    appendText(span, line.slice(0, a));
    if (b > a) span.append(appendText(el('mark'), line.slice(a, b)));
    appendText(span, line.slice(b));
    return span;
  }
  /* A highlight must not cut a pinned token in half: the halves would land in
     separate <bdo> runs and swap on screen. Widen the range to whole tokens. */
  function snapToken(line, a, b) {
    for (const m of line.matchAll(TOKEN)) {
      const s = m.index, e = s + m[0].length;
      if (s < b && e > a) { if (s < a) a = s; if (e > b) b = e; }
    }
    return [a, b];
  }
  function renderOcr() {
    const box = $('cms-ocr'), d = detail;
    const text = d && typeof d.text === 'string' ? d.text : '';
    const key = d ? d.id + ':' + text.length + ':' + text.slice(0, 64) : null;
    if (ocr && ocrKey === key && ocr.text === text) return;
    ocrKey = key;
    ocr = indexText(text);
    if (!text.trim()) {
      box.replaceChildren(el('div', 'cms-empty', d && ACTIVE.has(d.stage) ? 'لم يُستخرج النص بعد.' : 'لا يوجد نص مستخرج لهذه الشكوى.'));
      return;
    }
    const frag = document.createDocumentFragment();
    if (!ocr.marker[0] && ocr.marker.includes(true)) frag.append(el('div', 'cms-pagemark', 'صفحة ' + num(1)));
    ocr.lines.forEach((line, i) => {
      if (ocr.marker[i]) { frag.append(el('div', 'cms-pagemark', 'صفحة ' + num(ocr.pages[i]))); return; }
      const row = el('div', 'cms-line');
      row.dataset.i = i;
      row.title = `صفحة ${num(ocr.pages[i])} · سطر ${num(ocr.numbers[i])}`;
      const no = el('span', 'cms-lno', num(ocr.numbers[i]));
      no.setAttribute('aria-hidden', 'true');
      row.append(no, lineText(line));
      ocr.nodes[i] = row;
      frag.append(row);
    });
    box.replaceChildren(frag);
  }
  function clearHits() {
    if (!ocr) return;
    for (const i of ocr.hits) {
      const row = ocr.nodes[i];
      if (!row) continue;
      row.classList.remove('is-hit');
      row.replaceChild(lineText(ocr.lines[i]), row.lastChild);
    }
    ocr.hits = [];
  }
  function showCitation(source) {
    selectViewer('text');
    renderOcr();
    if (!ocr || !ocr.nodes.length) return;
    clearHits();
    pendingPage = Number(source.page) || 0;
    let start = Number(source.start), end = Number(source.end), first, last;
    if (Number.isInteger(start) && Number.isInteger(end) && start >= 0 && end > start && end <= ocr.text.length) {
      first = lineAt(start);
      last = lineAt(end - 1);
    } else {
      // No usable offsets: fall back to the page/line pair and mark the whole line.
      const li = ocr.at[Number(source.page) + ':' + Number(source.line)];
      if (li === undefined) { announce('تعذر تحديد موضع الاقتباس في النص.'); return; }
      first = last = li;
      start = ocr.starts[li];
      end = start + ocr.lines[li].length;
    }
    let target = null;
    for (let li = first; li <= last; li++) {
      const row = ocr.nodes[li];
      if (!row) continue;                                  // a page marker line
      const line = ocr.lines[li], base = ocr.starts[li];
      let a = Math.max(0, Math.min(line.length, start - base));
      let b = Math.max(a, Math.min(line.length, end - base));
      if (li > first) a = 0;
      if (li < last) b = line.length;
      [a, b] = snapToken(line, a, b);
      row.replaceChild(lineText(line, a, b), row.lastChild);
      row.classList.add('is-hit');
      ocr.hits.push(li);
      if (!target) target = row;
    }
    if (target) {
      target.tabIndex = -1;
      revealInPanel(target, 'center');      // the viewer scrolls, the register around the panel stays put
      target.focus({preventScroll: true});
    }
  }

  /* ------------------------------------------------------ sub-views, tabs */
  const viewButtons = [...root.querySelectorAll('.cms-views [role="tab"]')];
  function showView(name) {
    view = ['intake', 'register', 'analytics'].includes(name) ? name : 'intake';
    viewButtons.forEach(tab => {
      const on = tab.getAttribute('aria-controls') === 'cms-' + view;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      $(tab.getAttribute('aria-controls')).hidden = !on;
    });
    hideTip();
    store('cms.view', view);
    refreshView();
  }
  viewButtons.forEach((tab, index) => {
    tab.addEventListener('click', () => showView(tab.getAttribute('aria-controls').slice(4)));
    tab.addEventListener('keydown', event => {
      const rtl = getComputedStyle(tab.parentElement).direction === 'rtl';
      const step = {ArrowLeft: rtl ? 1 : -1, ArrowRight: rtl ? -1 : 1}[event.key];
      let next;
      if (step) next = viewButtons[(index + step + viewButtons.length) % viewButtons.length];
      if (event.key === 'Home') next = viewButtons[0];
      if (event.key === 'End') next = viewButtons[viewButtons.length - 1];
      if (!next) return;
      event.preventDefault();
      showView(next.getAttribute('aria-controls').slice(4));
      next.focus();
    });
  });
  function refreshView() {
    if (!active) return Promise.resolve();
    if (view === 'register') return loadRegister();
    if (view === 'analytics') return loadAnalytics();
    return loadIntake();
  }
  function refreshAfterChange() {
    if (!active) return;
    if (view !== 'intake') refreshView();
    loadIntake();
  }

  /* The tab controller (comparison_ui.js) announces every switch; the hidden
     attribute is watched too, so the workspace also wakes if something else
     reveals it. */
  async function activate() {
    active = true;
    if (!config) await loadConfig();
    else loadConfig({quiet: true});
    if (!active) return;
    await Promise.all([loadQueue(), loadIntake(), view === 'intake' ? null : refreshView()]);
    wasBusy = busyNow();
    schedule();
  }
  function deactivate() {
    active = false;
    clearTimeout(pollTimer);
    pollTimer = 0;
    hideTip();
  }
  function sync() {
    const visible = !root.hidden;
    if (visible && !active) activate();
    else if (!visible && active) deactivate();
  }
  window.addEventListener('workspace:change', sync);
  new MutationObserver(sync).observe(root, {attributes: true, attributeFilter: ['hidden']});

  showView(recall('cms.view') || 'intake');
  renderLimits();
  sync();
})();
