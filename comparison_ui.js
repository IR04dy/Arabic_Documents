/* Separate comparison workspace; never modifies analysis/chat document state. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const MAX_CHARS = 40000;
  const docs = {a: null, b: null};
  let report = null, generation = 0, controller = null, busy = false;
  const tabs = [$('analysis-tab'), $('comparison-tab')];
  function selectTab(tab) {
    tabs.forEach(item => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
      $(item.getAttribute('aria-controls')).hidden = !active;
    });
  }
  tabs.forEach(tab => {
    tab.addEventListener('click', () => selectTab(tab));
    tab.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') next = tabs[1 - tabs.indexOf(tab)];
      if (event.key === 'Home') next = tabs[0];
      if (event.key === 'End') next = tabs[1];
      if (next) { event.preventDefault(); selectTab(next); next.focus(); }
    });
  });
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function setBusy(value) {
    busy = value;
    for (const key of ['a', 'b']) $('cmp-file-' + key).disabled = value;
    $('cmp-run').disabled = value || !$('cmp-file-a').files[0] || !$('cmp-file-b').files[0];
    $('cmp-cancel').hidden = !value;
    $('cmp-progress').hidden = !value;
    if (!value) $('cmp-progress').removeAttribute('value');
  }
  function clearReport() {
    report = null;
    $('cmp-results').hidden = true;
    $('cmp-findings').replaceChildren();
    $('cmp-placeholder').hidden = false;
    $('cmp-error').hidden = true;
    $('cmp-status').textContent = '';
  }
  function error(message) {
    $('cmp-error').textContent = message;
    $('cmp-error').hidden = false;
  }
  function current(gen) {
    if (generation !== gen) throw new DOMException('Cancelled', 'AbortError');
  }
  for (const key of ['a', 'b']) {
    $('cmp-file-' + key).addEventListener('change', () => {
      generation++;
      controller?.abort();
      docs[key] = null;
      $('cmp-view-' + key).hidden = true;
      const file = $('cmp-file-' + key).files[0];
      $('cmp-info-' + key).textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : 'لم يتم اختيار مستند';
      clearReport();
      setBusy(false);
    });
    $('cmp-view-' + key).addEventListener('click', () => showSource(key));
  }
  async function readDocument(key, gen, signal) {
    if (docs[key]) return docs[key];
    const file = $('cmp-file-' + key).files[0];
    const label = key === 'a' ? 'الأول' : 'الثاني';
    if (!file || !file.size) throw new Error(`المستند ${label} فارغ.`);
    if (file.size > 100 * 1024 * 1024) throw new Error('الحد الأقصى لحجم الملف 100 MB.');
    $('cmp-status').textContent = `استخراج نص المستند ${label}: ${file.name}…`;
    let value;
    if (/\.txt$/i.test(file.name)) {
      if (file.size > MAX_CHARS * 4 + 3) throw new Error(`ملف النص يتجاوز حد ${MAX_CHARS.toLocaleString('ar')} حرف.`);
      try {
        value = {name: file.name, text: new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer()), page_count: 0, empty_pages: []};
      } catch (_) {
        throw new Error('تعذر قراءة ملف النص؛ احفظه بترميز UTF-8.');
      }
    } else {
      if (!/\.(pdf|png|jpe?g|webp|tiff?|bmp)$/i.test(file.name)) throw new Error('اختر PDF أو صورة أو ملف TXT بترميز UTF-8.');
      const form = new FormData();
      form.append('file', file);
      const response = await fetch('/extract', {method: 'POST', body: form, signal});
      const extracted = await response.json();
      if (!response.ok || extracted.error) throw new Error(extracted.error || 'تعذر استخراج النص.');
      value = {name: file.name, text: extracted.full_text, page_count: extracted.page_count,
        empty_pages: (extracted.pages || []).filter(page => !page.text.trim()).map(page => page.page)};
    }
    current(gen);
    if (!value.text?.trim()) throw new Error(`لم يُستخرج نص مقروء من المستند ${label}.`);
    // Python limits Unicode code points; don't count an emoji twice in the browser.
    const chars = [...value.text].length;
    if (chars > MAX_CHARS) throw new Error(`المستند ${label} يتجاوز ${MAX_CHARS.toLocaleString('ar')} حرف. اختر جزءاً أصغر؛ لم يتم اقتطاع النص.`);
    docs[key] = value;
    $('cmp-info-' + key).textContent = `${file.name} · ${chars.toLocaleString('ar')} حرف` +
      (value.page_count ? ` · ${value.page_count.toLocaleString('ar')} صفحة` : '') +
      (value.empty_pages.length ? ` · ${value.empty_pages.length} صفحات بلا نص` : '');
    $('cmp-view-' + key).hidden = false;
    return value;
  }
  $('cmp-run').addEventListener('click', async () => {
    if (busy) return;
    clearReport();
    const gen = ++generation;
    controller = new AbortController();
    const signal = controller.signal;
    setBusy(true);
    try {
      const a = await readDocument('a', gen, signal);
      const b = await readDocument('b', gen, signal);
      current(gen);
      $('cmp-status').textContent = 'بدء المقارنة باستخدام النموذج المحلي…';
      const response = await fetch('/comparison/run', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({a, b}), signal,
      });
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.error || 'تعذرت المقارنة.');
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '', gotResult = false;
      function consume(line) {
        if (!line.trim()) return;
        current(gen);
        const event = JSON.parse(line);
        if (event.event === 'progress') {
          $('cmp-status').textContent = event.message;
          if (event.total) { $('cmp-progress').max = event.total; $('cmp-progress').value = event.done; }
        } else if (event.event === 'error') {
          throw new Error(event.message);
        } else if (event.event === 'result') {
          report = event.report;
          gotResult = true;
          renderReport();
        }
      }
      while (true) {
        const {value, done} = await reader.read();
        current(gen);
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        const lines = buffer.split('\n');
        buffer = lines.pop();
        lines.forEach(consume);
        if (done) break;
      }
      if (buffer.trim()) consume(buffer);
      if (!gotResult) throw new Error('انقطع الاتصال قبل اكتمال التقرير. أعد المحاولة.');
      $('cmp-status').textContent = report.complete ? 'اكتملت جولات المقارنة.' : 'التقرير جزئي؛ راجع ملاحظات التغطية.';
    } catch (exc) {
      if (gen === generation && exc.name !== 'AbortError') {
        controller?.abort();
        error(exc.message || 'تعذرت المقارنة.');
        $('cmp-status').textContent = 'لم تكتمل المقارنة.';
      }
    } finally {
      if (gen === generation) { controller = null; setBusy(false); }
    }
  });
  $('cmp-cancel').addEventListener('click', () => {
    generation++;
    controller?.abort();
    controller = null;
    setBusy(false);
    $('cmp-status').textContent = 'أُلغيت المقارنة. قد يستكمل الخادم عملية الاستخراج أو جولة النموذج الحالية قبل بدء طلب جديد.';
  });
  function renderReport() {
    $('cmp-placeholder').hidden = true;
    $('cmp-results').hidden = false;
    $('cmp-summary-title').textContent = report.text_identical ? 'النصان المستخرجان متطابقان حرفياً' :
      (report.complete ? 'نتائج مقارنة النصوص' : 'نتائج جزئية — توجد فجوات في المراجعة');
    const stats = $('cmp-stats');
    stats.replaceChildren();
    for (const [key, count] of Object.entries(report.counts)) stats.append(el('span', 'cmp-stat', `${report.statuses[key]}: ${count}`));
    $('cmp-coverage').textContent = ['a', 'b'].map(key => {
      const c = report.coverage[key];
      return `${key === 'a' ? 'الأول' : 'الثاني'}: ${c.reviewed_chunks} / ${c.chunks} مقاطع تمت مراجعتها`;
    }).join(' · ') + ` · ${report.passes} جولات`;
    $('cmp-warnings').replaceChildren(...report.warnings.map(text => el('li', '', text)));
    for (const [id, choices, label] of [['cmp-aspect', report.aspects, 'كل الجوانب'], ['cmp-kind', report.statuses, 'كل أنواع النتائج']]) {
      const select = $(id);
      select.replaceChildren(new Option(label, ''));
      for (const [key, text] of Object.entries(choices)) select.add(new Option(text, key));
    }
    renderFindings();
  }
  function renderFindings() {
    if (!report) return;
    const selected = report.findings.filter(item => (!$('cmp-aspect').value || item.aspect === $('cmp-aspect').value) &&
      (!$('cmp-kind').value || item.status === $('cmp-kind').value));
    $('cmp-count').textContent = `${selected.length} من ${report.findings.length} نتيجة`;
    const container = $('cmp-findings');
    container.replaceChildren();
    if (!selected.length) container.append(el('div', 'cmp-placeholder', 'لا توجد نتائج موثقة ضمن هذا الاختيار. غياب النتائج لا يثبت تطابق المستندين.'));
    for (const finding of selected) {
      const card = el('article', 'cmp-finding');
      const heading = el('div', 'cmp-finding-header');
      const badge = el('span', 'cmp-badge', report.statuses[finding.status]);
      badge.dataset.status = finding.status;
      heading.append(badge, el('span', '', report.aspects[finding.aspect]));
      if (finding.source !== 'llm') heading.append(el('span', '', 'فحص نصي مباشر'));
      card.append(heading, el('p', '', finding.explanation));
      const evidence = el('div', 'cmp-evidence');
      for (const key of ['a', 'b']) {
        const box = el('div', 'cmp-quote');
        box.append(el('strong', '', `${key === 'a' ? 'المستند الأول' : 'المستند الثاني'} · ${docs[key].name}`));
        const cite = finding[key];
        if (cite) {
          const quote = el('blockquote', '', cite.quote);
          quote.dir = 'auto';
          const link = el('button', '', `${cite.page ? 'صفحة ' + cite.page + ' · ' : ''}سطر ${cite.line} — عرض في المصدر`);
          link.type = 'button';
          link.addEventListener('click', () => showSource(key, cite));
          box.append(quote, link);
        } else box.append(el('span', '', 'لا يوجد مقابل موثق في المقطع المقارن؛ لا يثبت غيابه عن المستند كله.'));
        evidence.append(box);
      }
      card.append(evidence);
      container.append(card);
    }
  }
  for (const id of ['cmp-aspect', 'cmp-kind']) $(id).addEventListener('change', renderFindings);
  function showSource(key, cite) {
    const doc = docs[key];
    if (!doc) return;
    $('cmp-source-title').textContent = doc.name;
    const pre = $('cmp-source-text');
    pre.replaceChildren();
    let mark;
    if (cite) {
      mark = el('mark', '', doc.text.slice(cite.start_utf16, cite.end_utf16));
      pre.append(document.createTextNode(doc.text.slice(0, cite.start_utf16)), mark,
        document.createTextNode(doc.text.slice(cite.end_utf16)));
    } else pre.textContent = doc.text;
    $('cmp-source').showModal();
    if (mark) mark.scrollIntoView({block: 'center'});
    else pre.scrollTop = 0;
  }
  $('cmp-source-close').addEventListener('click', () => $('cmp-source').close());
  $('cmp-download').addEventListener('click', () => {
    if (!report) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json;charset=utf-8'}));
    const anchor = el('a');
    anchor.href = url;
    anchor.download = 'document-comparison.json';
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  setBusy(false);
})();
