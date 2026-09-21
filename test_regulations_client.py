import io
import json
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from regulations_client import create_router,RegulationsClient,ServiceError


class FakeClient:
    def __init__(self,events=None,error=None):
        self.events=events or [{'type':'progress','completed':0,'total':1},{'type':'result','results':[]}]
        self.error=error;self.response=None;self.calls=[]
    def open(self,path,body=None):
        self.calls.append((path,body))
        if self.error:raise self.error
        data=b'%PDF-test' if path.endswith('/pdf') else ''.join(json.dumps(e)+'\n' for e in self.events).encode()
        self.response=io.BytesIO(data)
        return self.response


class RegulationsRouterTests(unittest.TestCase):
    def make(self,service=None):
        service=service or FakeClient();app=FastAPI();app.include_router(create_router(service))
        return TestClient(app),service

    def test_progress_result_and_exact_text_are_forwarded(self):
        client,service=self.make();text='--- Page 2 ---\nلا يجوز البيع إلا بشرط ١٢٣'
        response=client.post('/regulations/related',json={'text':text})
        self.assertEqual(response.status_code,200)
        self.assertEqual([json.loads(x)['type'] for x in response.text.splitlines()],['progress','result'])
        self.assertEqual(json.loads(service.calls[0][1])['text'],text)
        self.assertTrue(service.response.closed)

    def test_blank_text_never_calls_service(self):
        client,service=self.make();self.assertEqual(client.post('/regulations/related',json={'text':' '}).status_code,400)
        self.assertEqual(service.calls,[])

    def test_oversized_text_never_calls_service(self):
        client,service=self.make();self.assertEqual(client.post('/regulations/related',json={'text':'a'*200001}).status_code,413)
        self.assertEqual(service.calls,[])

    def test_oversized_body_is_rejected(self):
        client,service=self.make();self.assertEqual(client.post('/regulations/related',content=b'x'*1048577).status_code,413)
        self.assertEqual(service.calls,[])

    def test_service_failure_is_actionable(self):
        client,_=self.make(FakeClient(error=ServiceError('service unavailable')))
        self.assertEqual(client.post('/regulations/related',json={'text':'نص'}).status_code,503)

    def test_unfinished_stream_is_reported(self):
        client,_=self.make(FakeClient(events=[{'type':'progress'}]))
        events=[json.loads(x) for x in client.post('/regulations/related',json={'text':'نص'}).text.splitlines()]
        self.assertEqual(events[-1]['type'],'error')

    def test_source_pdf_uses_active_document_route(self):
        client,service=self.make();response=client.get('/regulations/source/9')
        self.assertEqual(response.content,b'%PDF-test')
        self.assertEqual(service.calls[0][0],'/documents/9/pdf')
        self.assertTrue(service.response.closed)

    def test_external_service_urls_are_rejected(self):
        for url in ['https://example.com','http://127.0.0.1/redirect','http://name:secret@127.0.0.1']:
            with self.assertRaises(ValueError):RegulationsClient(url)


if __name__=='__main__':unittest.main()
