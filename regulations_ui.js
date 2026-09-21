/* Original extracted text only. Uploaded text/results remain in this page's memory. */
(() => {
  const el=id=>document.getElementById(id);
  const dialog=el('regulations-panel'), button=el('related-regulations');
  const status=el('regulations-status'), results=el('regulations-results');
  const retry=el('regulations-retry'), cancel=el('regulations-cancel');
  let generation=0, controller=null, complete=false;
  const digits=n=>String(n).replace(/[0-9]/g,d=>'٠١٢٣٤٥٦٧٨٩'[Number(d)]);
  function node(tag,text,cls){const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;}
  function stop(){generation++;if(controller)controller.abort();controller=null;cancel.hidden=true;button.disabled=!lastText.trim();}
  function reset(){stop();complete=false;results.replaceChildren();status.textContent='';el('regulations-scope').textContent='العينة الحالية: ٨ لوائح · ١٩٠ مقطعًا';retry.hidden=true;button.disabled=true;if(dialog.open)dialog.close();}
  function open(){if(!dialog.open)dialog.showModal();}
  function showResult(data){
    results.replaceChildren();complete=true;
    el('regulations-scope').textContent=`نطاق البحث: ${digits(data.scope.documents)} لوائح · ${digits(data.scope.clauses)} مقطعًا. النتائج مرشحة للمراجعة ولا تثبت انطباق اللائحة.`;
    status.textContent=`اكتمل البحث في ${digits(data.coverage.processed_passages)} مقطعًا من المستند؛ عُثر على ${digits(data.total)} مادة ذات صلة محتملة.`;
    if(data.vector_status!=='available'){
      results.append(node('p','البحث الدلالي غير مكتمل. هذه نتائج جزئية؛ أعد المحاولة بعد تشغيل خدمة التضمين.','reg-warning'));
      retry.hidden=false;
    }
    if(!data.results.length){results.append(node('p','لم يُعثر على مواد تتجاوز معيار الصلة في العينة الحالية.','empty'));return;}
    const groups=new Map();
    for(const item of data.results){
      if(!groups.has(item.source_index))groups.set(item.source_index,[]);
      groups.get(item.source_index).push(item);
    }
    for(const [sourceIndex,items] of groups){
      const group=node('section',null,'reg-group'), first=items[0];
      group.append(node('h3',first.official_title_ar||first.pdf_title_candidate||first.website_title));
      group.append(node('p',`${digits(items.length)} مادة · ${first.official_title_ar?'عنوان موثّق':'عنوان يحتاج إلى التحقق'}`,'reg-meta'));
      for(const item of items){
        const card=node('article',null,'reg-card');
        card.append(node('h4',item.label||'نص اللائحة'));
        card.append(node('p',`صفحات اللائحة: ${item.page_spans.map(digits).join('، ')} · المراجعة: مسودة`,'reg-meta'));
        if(item.quality_flags.some(f=>f.issue==='amendments_not_consolidated'))card.append(node('p','تتضمن اللائحة تعديلات لم تُدمج في هذا النص. راجع صفحات التعديل قبل الاعتماد عليه.','reg-warning'));
        if(item.quality_flags.some(f=>f.issue==='unrepresented_article_heading'))card.append(node('p','يوجد تنبيه في مراجعة أحد عناوين المواد في هذا المصدر.','reg-warning'));
        card.append(node('p',item.original_text,'reg-clause'));
        const link=node('a','فتح مصدر اللائحة ↗','reg-source');
        link.href=`/regulations/source/${encodeURIComponent(sourceIndex)}#page=${item.page_spans[0]}`;
        link.target='_blank';link.rel='noopener';card.append(link);
        const matches=node('details'), summary=node('summary',`النص المرتبط في مستندك (${digits(item.matched_passage_count)} مقطعًا)`);
        matches.append(summary);
        for(const match of item.matches){
          const box=node('div',null,'reg-match');
          box.append(node('p',match.text));
          const show=node('button',match.page?`إظهار النص · صفحة ${digits(match.page)}`:'إظهار النص في المستند','btn-out');
          show.type='button';show.onclick=()=>{dialog.close();const p=document.getElementById('p1');if(p.classList.contains('collapsed'))p.querySelector('.collapse').click();showSource(match.start_utf16,match.end_utf16);};
          box.append(show);matches.append(box);
        }
        if(item.matched_passage_count>item.matches.length)matches.append(node('p','تُعرض أقوى ثلاثة مقاطع مرتبطة بهذه المادة.','reg-meta'));
        card.append(matches);group.append(card);
      }
      results.append(group);
    }
  }
  async function search(){
    if(!lastText.trim())return;
    stop();const current=++generation;const text=lastText;
    controller=new AbortController();const signal=controller.signal;
    complete=false;results.replaceChildren();retry.hidden=true;cancel.hidden=false;button.disabled=true;open();
    status.textContent='جارٍ تجهيز النص والبحث عن المواد ذات الصلة…';
    let gotResult=false;
    try{
      const response=await fetch('/regulations/related',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text}),signal});
      if(!response.ok){const error=await response.json();throw new Error(error.error||'تعذّر بدء البحث.');}
      const reader=response.body.getReader(), decoder=new TextDecoder();let buffer='';
      function consume(line){
        if(!line.trim()||current!==generation)return;
        const event=JSON.parse(line);
        if(event.type==='error')throw new Error(event.error);
        if(event.type==='progress')status.textContent=`جارٍ البحث: ${digits(event.completed)} من ${digits(event.total)} مقطعًا…`;
        if(event.type==='result'){gotResult=true;showResult(event);}
      }
      while(true){
        const {value,done}=await reader.read();if(current!==generation){await reader.cancel();return;}
        buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});
        let cut;while((cut=buffer.indexOf('\n'))>=0){consume(buffer.slice(0,cut));buffer=buffer.slice(cut+1);}
        if(done){consume(buffer);break;}
      }
      if(!gotResult)throw new Error('انقطع البحث قبل اكتماله. أعد المحاولة.');
    }catch(error){
      if(current!==generation)return;
      if(error.name!=='AbortError'){status.textContent=error.message||'تعذّر البحث. أعد المحاولة.';retry.hidden=false;}
    }finally{
      if(current===generation){controller=null;cancel.hidden=true;button.disabled=!lastText.trim();}
    }
  }
  button.onclick=()=>{if(complete)open();else search();};retry.onclick=search;
  cancel.onclick=()=>{stop();status.textContent='أُلغي البحث.';retry.hidden=false;};
  el('regulations-close').onclick=()=>dialog.close();
  dialog.addEventListener('close',()=>{if(controller){stop();status.textContent='أُلغي البحث.';retry.hidden=false;}});
  window.addEventListener('beforeunload',stop);
  window.RegulationsUI={reset,ready:()=>{button.disabled=!lastText.trim();}};
})();
