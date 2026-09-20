/* Use the existing OCR -> proofreading -> fields -> chat workflow on explicit selection. */
window.qrUseCapture = async (file, extractUrl, isCurrent) => {
  if (busy) throw new Error("انتظر انتهاء قراءة المستند الحالي أولاً.");
  if (!engineReady) throw new Error("انتظر جاهزية محرك القراءة أولاً.");
  busy = true; updateUpload();
  try {
    const response = await fetch(extractUrl, {method:"POST", headers:{"X-QRBot-Request":"1"}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "تعذّر استخراج نص اللقطة");
    if (!isCurrent()) return false;
    // Switching only after success preserves the current document when OCR fails.
    busy = false;
    setFile(file, {qrCapture:true, extraction:data.extraction});
    setView("text");
    return true;
  } finally {
    busy = false; updateUpload();
  }
};

window.qrRestoreDocument = file => {
  if (busy) throw new Error("انتظر انتهاء قراءة اللقطة أولاً.");
  if (file) setFile(file, {qrCapture:true});
};
