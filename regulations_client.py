"""Loopback HTTP integration with the regulations service; no OCR/model imports."""
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request as URLRequest, build_opener, ProxyHandler, HTTPRedirectHandler

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse

MAX_BODY=1_048_576
MAX_TEXT=200_000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


class ServiceError(Exception):
    def __init__(self,message,status=503):
        self.message,self.status=message,status


class RegulationsClient:
    def __init__(self,base_url=None):
        self.base=(base_url or os.getenv('REGULATIONS_API_URL','http://127.0.0.1:8765')).rstrip('/')
        p=urlsplit(self.base)
        if p.scheme!='http' or p.hostname not in {'127.0.0.1','localhost','::1'} or p.username or p.password or p.path or p.query or p.fragment:
            raise ValueError('REGULATIONS_API_URL must be a localhost HTTP origin')
        self.opener=build_opener(ProxyHandler({}),NoRedirect())

    def open(self,path,body=None):
        req=URLRequest(self.base+path,data=body,headers={'Content-Type':'application/json'})
        try:
            return self.opener.open(req,timeout=90)
        except HTTPError as exc:
            status=exc.code
            exc.close()
            if status==429: raise ServiceError('يوجد بحث آخر قيد التنفيذ. أعد المحاولة بعد قليل.',429) from None
            if status==404: raise ServiceError('ملف اللائحة غير متاح في العينة الحالية.',404) from None
            if status==400: raise ServiceError('تعذّر قبول النص. تحقّق من حجمه ومحتواه.',400) from None
            raise ServiceError('تعذّر الاتصال بخدمة اللوائح. أعد المحاولة.',502) from None
        except (URLError,OSError):
            raise ServiceError('خدمة اللوائح غير متاحة. شغّل run-rag.ps1 ثم أعد المحاولة.') from None


def create_router(client=None):
    client=client or RegulationsClient()
    router=APIRouter(prefix='/regulations')

    @router.get('/ui.js')
    def javascript():
        return Response(Path(__file__).with_name('regulations_ui.js').read_text(encoding='utf8'),media_type='application/javascript')

    @router.post('/related')
    async def related(request:Request):
        content=bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>MAX_BODY:return JSONResponse({'error':'حجم الطلب يتجاوز الحد المسموح.'},status_code=413)
        try:
            payload=json.loads(content)
            if not isinstance(payload,dict) or not isinstance(payload.get('text'),str):raise ValueError()
            text=payload['text']
            if not text.strip():return JSONResponse({'error':'لا يوجد نص مستخرج للبحث.'},status_code=400)
            if len(text)>MAX_TEXT:return JSONResponse({'error':'يتجاوز النص ٢٠٠٬٠٠٠ حرف. قسّم المستند إلى أجزاء أصغر؛ لم يتم اقتطاع النص أو البحث فيه.'},status_code=413)
        except (ValueError,UnicodeError):
            return JSONResponse({'error':'طلب غير صالح.'},status_code=400)
        try:
            upstream=await run_in_threadpool(client.open,'/related',json.dumps({'text':text},ensure_ascii=False).encode('utf8'))
        except ServiceError as exc:return JSONResponse({'error':exc.message},status_code=exc.status)

        async def events():
            finished=False
            try:
                while True:
                    line=await run_in_threadpool(upstream.readline,8_388_609)
                    if not line:break
                    if len(line)>8_388_608:raise ValueError('Oversized response')
                    event=json.loads(line)
                    if event.get('type')=='result':finished=True
                    if event.get('type')=='error':
                        finished=True
                        event['error']='تعذّر إكمال البحث. تحقّق من خدمة اللوائح وأعد المحاولة.'
                    yield (json.dumps(event,ensure_ascii=False)+'\n').encode('utf8')
                if not finished:
                    yield (json.dumps({'type':'error','error':'انقطع البحث قبل اكتماله. أعد المحاولة.'},ensure_ascii=False)+'\n').encode('utf8')
            except (OSError,ValueError):
                yield (json.dumps({'type':'error','error':'تعذّر إكمال البحث. أعد المحاولة.'},ensure_ascii=False)+'\n').encode('utf8')
            finally:
                upstream.close()
        return StreamingResponse(events(),media_type='application/x-ndjson',headers={'Cache-Control':'no-store','X-Accel-Buffering':'no'})

    @router.get('/source/{document}')
    async def source(document:int):
        if document<1:return JSONResponse({'error':'رقم لائحة غير صالح.'},status_code=404)
        try:upstream=await run_in_threadpool(client.open,f'/documents/{document}/pdf')
        except ServiceError as exc:return JSONResponse({'error':exc.message},status_code=exc.status)
        async def blocks():
            try:
                while data:=await run_in_threadpool(upstream.read,65536):yield data
            finally:upstream.close()
        return StreamingResponse(blocks(),media_type='application/pdf',headers={'Content-Disposition':f'inline; filename="regulation-{document}.pdf"','X-Content-Type-Options':'nosniff'})
    return router
