/* Detection, user decision, capture preview, and OCR are separate actions. */
(() => {
  const box = document.createElement("details");
  box.id = "qr-results";
  box.style.cssText = "flex:none;border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:6px;max-height:38vh;overflow:auto";
  const summary = document.createElement("summary");
  summary.textContent = "رموز QR والصفحات المرتبطة";
  summary.style.cursor = "pointer";
  const hint = document.createElement("p");
  hint.textContent = "يفحص رموز QR ويعرض نتيجة فحص الأمان فقط. اختر جلب المحتوى أو تخطّي كل رابط قبل فتحه.";
  const start = document.createElement("button");
  start.type = "button"; start.className = "btn-out";
  start.textContent = "اكتشاف QR وفحص الأمان"; start.disabled = true;
  const state = document.createElement("p"); state.setAttribute("role", "status");
  const results = document.createElement("div");
  box.append(summary, hint, start, state, results);
  document.querySelector("#status").after(box);
  document.querySelector("#p1 .pbody").style.overflowY = "auto";
  document.querySelector("#p1 .viewer").style.minHeight = "260px";
  const captures = document.createElement("details");
  captures.id = "qr-captures"; captures.hidden = true;
  captures.style.cssText = "flex:none;border:1px solid var(--line);border-radius:6px;padding:8px;margin-top:6px;max-height:42vh;overflow:auto";
  const captureTitle = document.createElement("summary");
  captureTitle.textContent = "لقطات الصفحات المرتبطة";
  const captureHint = document.createElement("p");
  captureHint.textContent = "معاينة صور محفوظة. اختر استخدام اللقطة لقراءة النص ومتابعة التدقيق والحقول والمحادثة على محتواها.";
  const restore = document.createElement("button");
  restore.type = "button"; restore.className = "btn-out"; restore.hidden = true;
  restore.textContent = "العودة إلى المستند الأصلي وإعادة قراءته";
  restore.onclick = () => {
    try { window.qrRestoreDocument(file); restore.hidden = true; }
    catch (error) { state.textContent = error.message; }
  };
  const gallery = document.createElement("div");
  captures.append(captureTitle, captureHint, restore, gallery); box.after(captures);
  const shownCaptures = new Set();
  let file = null, generation = 0, job = null, working = false;
  const labels = {queued:"في الانتظار", running:"جارٍ المعالجة…", needs_review:"بانتظار قرارك: جلب المحتوى أو تخطّيه",
    completed:"اكتمل الفحص", completed_with_issues:"اكتمل الفحص مع روابط محظورة أو متعذرة",
    failed:"تعذّر الفحص", interrupted:"توقفت المعالجة — راجع النتائج قبل إعادة الفحص",
    captured:"تم الحفظ", blocked:"محظور", skipped_by_operator:"تم التخطي دون جلب المحتوى",
    allow:"لم تظهر تحذيرات تمنع المتابعة", escalate:"توجد تحذيرات تحتاج مراجعتك", block:"محظور"};
  document.addEventListener("qr:document", event => {
    file = event.detail; generation++; job = null; working = false;
    start.disabled = false; results.replaceChildren(); state.textContent = "جاهز لفحص المستند الحالي";
    gallery.replaceChildren(); shownCaptures.clear(); captures.hidden = true; restore.hidden = true;
  });
  async function request(url, options = {}) {
    const response = await fetch(url, {...options, headers:{"X-QRBot-Request":"1", ...options.headers}});
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.error || error.detail || "تعذّر الاتصال بخدمة QR");
    }
    return response;
  }
  function message(text, target = results) {
    const paragraph = document.createElement("p"); paragraph.textContent = text; paragraph.dir = "auto";
    paragraph.style.overflowWrap = "anywhere"; target.append(paragraph); return paragraph;
  }
  function showCapture(value, link, gen) {
    const key = value.job_id + "/" + link.artifact_id;
    if (shownCaptures.has(key)) return;
    shownCaptures.add(key); captures.hidden = false; captures.open = true;
    const card = document.createElement("section");
    card.style.cssText = "border-top:1px solid var(--line);padding:8px 0";
    message(link.url, card);
    message(`رمز QR في صفحة ${link.found_on_pages.join("، ")} من المستند الأصلي`, card);
    for (const flag of link.flags || []) {
      message(`تحذير بعد فتح الصفحة: ${flag.url} — ${(flag.reasons || []).join("؛ ")}`, card);
    }
    const base = `/qr/jobs/${value.job_id}/artifacts/${link.artifact_id}`;
    const images = document.createElement("div");
    for (const [index, previewId] of (link.preview_ids || []).entries()) {
      const img = document.createElement("img");
      img.src = `/qr/jobs/${value.job_id}/artifacts/${previewId}`;
      img.alt = `لقطة الصفحة ${index + 1}`; img.loading = "lazy";
      img.style.cssText = "display:block;width:100%;height:auto;border:1px solid var(--line);margin:6px 0";
      img.onerror = () => { img.alt = "تعذّر عرض اللقطة. يمكنك تحميل PDF."; };
      img.onload = () => {
        if (gen === generation && index === 0) captures.scrollIntoView({block:"end"});
      };
      const fullSize = document.createElement("a");
      fullSize.href = img.src; fullSize.target = "_blank"; fullSize.rel = "noopener";
      fullSize.title = "فتح اللقطة بحجم أكبر"; fullSize.append(img); images.append(fullSize);
    }
    if (!(link.preview_ids || []).length) message("لا توجد معاينة لهذه النتيجة السابقة؛ يمكنك تحميل PDF.", card);
    const download = document.createElement("a");
    download.href = base; download.textContent = "تحميل PDF"; download.style.marginInlineEnd = "10px";
    const use = document.createElement("button");
    use.type = "button"; use.className = "btn-out";
    const caption = "استخدام اللقطة في OCR ومتابعة المعالجة";
    use.textContent = caption;
    const feedback = document.createElement("p"); feedback.setAttribute("role", "status");
    use.onclick = async () => {
      if (!confirm("ستصبح هذه اللقطة المستند النشط للتدقيق واستخراج الحقول والمحادثة. هل تريد المتابعة؟")) return;
      use.disabled = true; feedback.textContent = "جارٍ قراءة اللقطة…";
      try {
        const blob = await (await request(base)).blob();
        if (gen !== generation) return;
        const captured = new File([blob], `QR-${value.job_id.slice(3, 11)}-${link.artifact_id}.pdf`, {type:"application/pdf"});
        const applied = await window.qrUseCapture(captured, base + "/extract", () => gen === generation);
        if (gen !== generation) return;
        if (applied) {
          restore.hidden = false;
          feedback.textContent = "أصبحت اللقطة المستند النشط؛ تستمر خطوات التدقيق والحقول والمحادثة المعتادة.";
          document.querySelector("#p1 .viewer").scrollIntoView({block:"nearest"});
        }
      } catch (error) { if (gen === generation) feedback.textContent = error.message; }
      finally { if (gen === generation) use.disabled = false; }
    };
    card.append(download, use, feedback, images); gallery.append(card);
    setTimeout(() => { if (gen === generation) captures.scrollIntoView({block:"end"}); }, 0);
  }
  function draw(value, gen) {
    job = value; results.replaceChildren();
    state.textContent = labels[value.status] || value.status;
    if (value.error) message(value.error);
    if (value.status.startsWith("completed") && !value.qr_codes.length) message("لم يُعثر على رموز QR.");
    for (const code of value.qr_codes.filter(code => code.kind !== "link")) {
      message(`صفحة ${code.page} · ${code.kind}: ${code.payload}`);
    }
    for (const link of value.links) {
      const card = document.createElement("div");
      card.style.cssText = "border-top:1px solid var(--line);padding:8px 0";
      message(link.url, card);
      message(`صفحة ${link.found_on_pages.join("، ")} · ${labels[link.status] || link.status}`, card);
      if (link.decision) message(`فحص الأمان: ${labels[link.decision] || link.decision}`, card);
      for (const reason of link.reasons || []) message(reason, card);
      if (link.status === "needs_review" && value.status === "needs_review") {
        const approve = document.createElement("button");
        approve.type = "button"; approve.className = "btn-out"; approve.textContent = "جلب المحتوى وعرض اللقطة";
        const skip = document.createElement("button");
        skip.type = "button"; skip.className = "ghost"; skip.textContent = "تخطّي الرابط";
        const decide = async action => {
          if (working) return;
          if (action === "approvals" && !confirm("سيُفتح هذا الرابط من خلال البيئة المعزولة. هل توافق بعد مراجعة نتيجة فحص الأمان؟")) return;
          working = true; start.disabled = true; approve.disabled = true; skip.disabled = true;
          try {
            await request(`/qr/jobs/${value.job_id}/${action}`, {method:"POST",
              headers:{"Content-Type":"application/json"}, body:JSON.stringify({link_ids:[link.approval_id]})});
            if (gen === generation) await poll(value.job_id, gen);
          } catch (error) { if (gen === generation) { state.textContent = error.message; approve.disabled = false; skip.disabled = false; } }
          finally { if (gen === generation) { working = false; start.disabled = false; } }
        };
        approve.onclick = () => decide("approvals"); skip.onclick = () => decide("skips");
        card.append(approve, skip);
      }
      if (link.artifact_id) {
        showCapture(value, link, gen);
        const view = document.createElement("button"); view.type = "button"; view.className = "ghost";
        view.textContent = "عرض اللقطة";
        view.onclick = () => { captures.open = true; captures.scrollIntoView({block:"nearest"}); };
        card.append(view);
      }
      results.append(card);
    }
    const report = document.createElement("a");
    report.href = `/qr/jobs/${value.job_id}/artifacts/report`; report.textContent = "تحميل تقرير الفحص";
    results.append(report);
  }
  async function poll(id, gen) {
    working = true; start.disabled = true;
    try {
      while (gen === generation) {
        const value = await (await request(`/qr/jobs/${id}`)).json();
        if (gen !== generation) return;
        draw(value, gen);
        if (!["queued", "running"].includes(value.status)) return;
        await new Promise(resolve => setTimeout(resolve, 1200));
      }
    } finally { if (gen === generation) { working = false; start.disabled = false; } }
  }
  start.onclick = async () => {
    if (!file || working) return;
    if (job && !confirm("إنشاء فحص جديد سيزيل النتائج الحالية من العرض. لن تُفتح الروابط حتى تختار جلب المحتوى. هل تريد المتابعة؟")) return;
    const gen = ++generation;
    working = true; start.disabled = true; box.open = true; results.replaceChildren();
    gallery.replaceChildren(); shownCaptures.clear(); captures.hidden = true;
    state.textContent = "جارٍ إرسال المستند إلى خدمة QR…";
    const data = new FormData(); data.append("file", file);
    try {
      const created = await (await request("/qr/jobs", {method:"POST", body:data})).json();
      if (gen === generation) await poll(created.job_id, gen);
    } catch (error) {
      if (gen === generation) state.textContent = error.message;
    } finally { if (gen === generation) { working = false; start.disabled = false; } }
  };
})();
