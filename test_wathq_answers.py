"""How a 2xx answer from Wathq is decoded (compression, truncation, charsets,
empty and non-JSON bodies), and that every diagnostic describes an answer
without repeating any of it. No request leaves this machine."""
import codecs
import contextlib
import gzip
import io
import json
import os
import tempfile
import unittest
import zlib
from email.message import Message
from unittest import mock

import wathq_probe
from test_wathq_verify import CONTRACT
from wathq_client import (MAX_RESPONSE, MSG_EMPTY, MSG_FORBIDDEN, MSG_INVALID, MSG_NOT_FOUND,
                          MSG_TOO_LARGE, MSG_UPSTREAM, Answer, UnusableAnswer, WathqClient,
                          WathqError, decode_answer, describe, safe_key)
from wathq_probe import show, skeleton

KEY = "TESTKEY-0123456789abcdef"
UNIFIED = "7001272124"
PERSONAL = "1000000001"          # a partner's ID inside CONTRACT
NAME = "شريك تجريبي أول"         # a partner's name inside CONTRACT
BODY = json.dumps(CONTRACT, ensure_ascii=False).encode("utf-8")


def headers(**kw) -> Message:
    m = Message()
    for k, v in kw.items():
        m[k.replace("_", "-")] = v
    return m


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status=200, **kw):
        super().__init__(body)
        self.status, self.headers = status, headers(**kw)


class Opener:
    def __init__(self, response):
        self.response, self.requests = response, []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def call(response):
    """company_contract() against one fake response: (result or error, log, opener)."""
    opener = Opener(response)
    client = WathqClient(opener=opener, environ={"WATHQ_API_KEY": KEY})
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            result = client.company_contract(UNIFIED)
        except WathqError as exc:
            result = exc
    return result, out.getvalue(), opener


def no_data_in(test, text):
    for value in (PERSONAL, "1000000002", "1000000003", NAME, "شركة الاختبار", KEY, UNIFIED):
        test.assertNotIn(value, text)


class CompressionTests(unittest.TestCase):
    def test_identity_is_requested(self):
        _, _, opener = call(FakeResponse(BODY))
        self.assertEqual(opener.requests[0].headers.get("Accept-encoding"), "identity")

    def test_gzip_with_and_without_a_label(self):
        for kw in ({"Content_Encoding": "gzip"}, {}):
            data, log, _ = call(FakeResponse(gzip.compress(BODY), **kw))
            self.assertEqual(data, CONTRACT)
            self.assertIn("decoded with: gzip", log)

    def test_deflate_zlib_and_raw(self):
        data, _, _ = call(FakeResponse(zlib.compress(BODY), Content_Encoding="deflate"))
        self.assertEqual(data, CONTRACT)
        raw = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        data, _, _ = call(FakeResponse(raw.compress(BODY) + raw.flush(), Content_Encoding="deflate"))
        self.assertEqual(data, CONTRACT)

    def test_double_gzip_and_two_members(self):
        data, _, _ = call(FakeResponse(gzip.compress(gzip.compress(BODY)), Content_Encoding="gzip"))
        self.assertEqual(data, CONTRACT)
        half = len(BODY) // 2
        data, _, _ = call(FakeResponse(gzip.compress(BODY[:half]) + gzip.compress(BODY[half:]),
                                       Content_Encoding="gzip"))
        self.assertEqual(data, CONTRACT)

    def test_truncated_gzip_is_cut_off_not_empty(self):
        whole = gzip.compress(BODY)
        for cut in (whole[:10], whole[: len(whole) // 2]):
            exc, log, _ = call(FakeResponse(cut, Content_Encoding="gzip"))
            self.assertEqual(exc.message, MSG_UPSTREAM, len(cut))
            self.assertIn("compressed answer is cut off", log)

    def test_bytes_after_the_stream_are_refused(self):
        exc, log, _ = call(FakeResponse(gzip.compress(BODY) + b"JUNK", Content_Encoding="gzip"))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertIn("bytes after the compressed answer", log)

    def test_mislabelled_plain_json_is_accepted(self):
        for encoding in ("gzip", "br"):
            data, log, _ = call(FakeResponse(BODY, Content_Encoding=encoding))
            self.assertEqual(data, CONTRACT, encoding)
            self.assertIn("ignored a Content-Encoding label", log)

    def test_corrupt_or_unsupported(self):
        exc, _, _ = call(FakeResponse(b"\x1f\x8bgarbage", Content_Encoding="gzip"))
        self.assertEqual(exc.message, MSG_INVALID)
        exc, log, _ = call(FakeResponse(b"xxxx", Content_Encoding="br"))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertIn("unsupported encoding", log)

    def test_decompression_bomb_is_capped(self):
        bomb = gzip.compress(b"{" + b" " * (MAX_RESPONSE + 10) + b"}")
        exc, _, _ = call(FakeResponse(bomb, Content_Encoding="gzip"))
        self.assertEqual(exc.message, MSG_TOO_LARGE)


class TruncationTests(unittest.TestCase):
    def test_short_content_length_is_reported(self):
        exc, log, _ = call(FakeResponse(BODY[:100], Content_Length=str(len(BODY))))
        self.assertEqual(exc.message, MSG_UPSTREAM)
        self.assertIn("answer cut off", log)
        self.assertIn(f"length={len(BODY)}", log)
        self.assertIn("wire-bytes=100", log)

    def test_incomplete_read_is_reported(self):
        import http.client
        exc, log, _ = call(http.client.IncompleteRead(b"x" * 10, 90))
        self.assertEqual(exc.message, MSG_UPSTREAM)
        self.assertIn("cut off in transit", log)


class CharsetTests(unittest.TestCase):
    def test_declared_windows_1256_is_honoured(self):
        body = json.dumps(CONTRACT, ensure_ascii=False).encode("cp1256")
        data, log, _ = call(FakeResponse(body, Content_Type="application/json; charset=windows-1256"))
        self.assertEqual(data["entity"]["name"], CONTRACT["entity"]["name"])
        self.assertIn("declared charset windows-1256", log)

    def test_a_wrong_label_never_garbles_valid_utf8(self):
        for label in ("iso-8859-1", "windows-1256", "iso-8859-6"):
            data, log, _ = call(FakeResponse(BODY, Content_Type=f"application/json; charset={label}"))
            self.assertEqual(data, CONTRACT, label)
            self.assertNotIn("declared charset", log)

    def test_undeclared_non_utf8_is_reported_with_where(self):
        body = json.dumps(CONTRACT, ensure_ascii=False).encode("cp1256")
        exc, log, _ = call(FakeResponse(body, Content_Type="application/json"))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertRegex(log, r"UnicodeDecodeError\(utf-8\)@\d+ byte=[0-9a-f]{2}")

    def test_unknown_charset_is_reported(self):
        body = json.dumps(CONTRACT, ensure_ascii=False).encode("cp1256")
        exc, log, _ = call(FakeResponse(body, Content_Type="application/json; charset=klingon"))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertIn("error=LookupError", log)

    def test_bom_utf16_and_double_encoding(self):
        data, _, _ = call(FakeResponse(codecs.BOM_UTF8 + BODY))
        self.assertEqual(data, CONTRACT)
        data, _, _ = call(FakeResponse(json.dumps(CONTRACT, ensure_ascii=False).encode("utf-16")))
        self.assertEqual(data, CONTRACT)
        data, log, _ = call(FakeResponse(json.dumps(json.dumps(CONTRACT)).encode()))
        self.assertEqual(data, CONTRACT)
        self.assertIn("double-encoded JSON", log)


class UnusableAnswerTests(unittest.TestCase):
    def test_empty_and_204(self):
        for response in (FakeResponse(b""), FakeResponse(b"  \r\n"), FakeResponse(b"", status=204)):
            exc, log, _ = call(response)
            self.assertEqual(exc.message, MSG_EMPTY)
            self.assertIn("contract answer is empty", log)

    def test_html_title_is_logged_without_data(self):
        page = ("<!DOCTYPE html><html><head><title>Request Rejected 1000000001 شركة الاختبار"
                "</title></head><body>secret</body></html>").encode()
        exc, log, _ = call(FakeResponse(page, Content_Type="text/html",
                                        **{"THIQAH-API-ApiMsgRef": "3f2a9c1e-0000-4000-8000-1234567890ab"}))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertIn("html-title='Request Rejected #'", log)
        self.assertIn("chars not shown", log)
        self.assertIn("wathq-ref=3f2a9c1e-0000-4000-8000-1234567890ab", log)
        no_data_in(self, log)
        self.assertNotIn("secret", log)

    def test_xml_title_elements_are_data_and_never_logged(self):
        for body in (b'<?xml version="1.0"?><contract><additionalArticles><title>Partner '
                     b'1000000001</title></additionalArticles></contract>',
                     b"<partner><titleName>1000000001</titleName></partner>"):
            exc, log, _ = call(FakeResponse(body, Content_Type="application/xml"))
            self.assertEqual(exc.message, MSG_INVALID)
            self.assertNotIn("1000000001", log)
            self.assertNotIn("Partner", log)
            self.assertIn("xml-root=", log)

    def test_text_after_an_xml_declaration_is_not_a_root(self):
        exc, log, _ = call(FakeResponse(b'<?xml version="1.0"?>Mohammed_1000000001 is not registered'))
        self.assertNotIn("Mohammed", log)
        self.assertNotIn("1000000001", log)

    def test_xml_root_after_doctype_and_comments(self):
        body = b'<?xml version="1.0"?><!-- c --><!DOCTYPE x><a:contract/>'
        self.assertIn("xml-root=a_contract", describe(Answer(200, headers(), body)).replace(":", "_"))

    def test_text_bodies_show_a_label_not_bytes(self):
        for body, label in ((b"1000000001 Mohammed", "starts=digits"),
                            ("محمد".encode(), "starts=non-ascii-text"),
                            (b"Mohammed 1000000001", "starts=ascii-text")):
            exc, log, _ = call(FakeResponse(body))
            self.assertIn(label, log)
            self.assertNotIn("first-bytes", log)
            self.assertNotIn("1000000001", log)
            self.assertNotIn("Mohammed", log)

    def test_binary_shows_four_bytes(self):
        exc, log, _ = call(FakeResponse(b"\x00\x01\x02\x03 1000000001"))
        self.assertIn("binary first-bytes=00010203", log)
        self.assertNotIn("1000000001", log)

    def test_broken_json_logs_the_parser_complaint_only(self):
        exc, log, _ = call(FakeResponse(BODY + b"\n{trailing", Content_Type="application/json"))
        self.assertEqual(exc.message, MSG_INVALID)
        self.assertIn("json-error='Extra data'", log)
        no_data_in(self, log)

    def test_the_request_key_is_never_logged(self):
        for body in (b"", b"<html><title>Error</title></html>", b"{broken", b"\x00\x01junk", b"<root/>"):
            _, log, _ = call(FakeResponse(body))
            self.assertNotIn(KEY, log)


class ErrorBodyTests(unittest.TestCase):
    def test_conversion_keys_are_logged_safely(self):
        client = WathqClient(opener=Opener(FakeResponse(
            json.dumps({"1010123456": "7001272124", "محمد بن سعيد": 1}, ensure_ascii=False).encode())),
            environ={"WATHQ_API_KEY": KEY})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(WathqError):
            client.national_number("1010123456")
        self.assertNotIn("1010123456", out.getvalue())
        self.assertNotIn("7001272124", out.getvalue())
        self.assertNotIn("محمد", out.getvalue())
        self.assertNotIn("\\u0645", out.getvalue())
        self.assertIn("10-char key, 10 digits", out.getvalue())

    def test_safe_key(self):
        self.assertEqual(safe_key("crNationalNumber"), "crNationalNumber")
        self.assertEqual(safe_key("partner_share_for_identity_number_of_the_partner_x_1000000001"),
                         "<61-char key, 10 digits>")
        self.assertEqual(safe_key("محمد بن سعيد"), "<12-char key, 0 digits, non-ascii>")


class ProxyTests(unittest.TestCase):
    def test_a_proxy_password_never_reaches_a_message(self):
        for url in ("http://user:S3cretPw@[bad:8080", "http://user:S3cretPw@proxy:99999",
                    "ftp://user:S3cretPw@proxy:8080"):
            with self.assertRaises(ValueError) as ctx:
                WathqClient(environ={"WATHQ_PROXY_URL": url})
            self.assertNotIn("S3cretPw", str(ctx.exception))

    def test_router_shows_no_password_either(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from wathq_api import create_router
        app = FastAPI()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            app.include_router(create_router(environ={"WATHQ_PROXY_URL": "http://u:S3cretPw@[bad:1"}))
        http = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1))
        body = http.get("/wathq/status").text
        self.assertNotIn("S3cretPw", body)
        self.assertNotIn("S3cretPw", out.getvalue())


class ProbeTests(unittest.TestCase):
    def test_skeleton_has_no_values(self):
        shape = json.dumps(skeleton(CONTRACT), ensure_ascii=False)
        no_data_in(self, shape)
        self.assertNotIn("2023-01-24", shape)
        self.assertIn('"crNumber": "str"', shape)
        self.assertIn('"managers": {', shape)          # one object where the schema says list

    def test_skeleton_merges_list_items(self):
        shape = skeleton({"rows": [{"a": 1}, {"a": "x", "b": None}, None]})
        (label, merged), = shape["rows"].items()
        self.assertEqual(label, "list[3] of null/object")
        self.assertEqual(merged["a"], "int|str")
        self.assertEqual(merged["b (in 1 of 2)"], "null")

    def test_skeleton_masks_data_shaped_keys(self):
        shape = skeleton({"1000000001": 1, "محمد بن سعيد القحطاني": 2,
                          "partner_share_for_identity_number_of_the_partner_xxxxxxxx_1000000001": 3})
        text = json.dumps(shape, ensure_ascii=False)
        self.assertNotIn("1000000001", text)
        self.assertNotIn("محمد", text)
        self.assertNotIn("_10", text)

    def test_show_matches_the_app(self):
        """The probe and the app must reach the same verdict on the same bytes."""
        cases = [(BODY, {"Content_Type": "application/json; charset=iso-8859-1"}, True),
                 (json.dumps(CONTRACT, ensure_ascii=False).encode("cp1256"),
                  {"Content_Type": "application/json; charset=windows-1256"}, True),
                 (b"<html><title>x</title></html>", {}, False),
                 (b"", {}, False),
                 (gzip.compress(BODY), {"Content_Encoding": "gzip"}, True)]
        for body, kw, usable in cases:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                ok, _ = show("x", Answer(200, headers(**kw), body))
            app_result, _, _ = call(FakeResponse(body, **kw))
            self.assertEqual(ok, usable, kw)
            self.assertEqual(not isinstance(app_result, WathqError), usable, kw)
            no_data_in(self, out.getvalue())

    def run_probe(self, argv, response, environ=None, stdin="y\n", secret=None):
        environ = {"WATHQ_API_KEY": KEY} if environ is None else environ
        opener = Opener(response)
        real = WathqClient

        def build(**kw):
            return real(opener=opener, environ=environ)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wathq_probe, "WathqClient", build), \
                mock.patch.object(wathq_probe.getpass, "getpass", return_value=secret or ""), \
                mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = wathq_probe.main(argv)
        return code, out.getvalue() + err.getvalue(), opener

    def test_probe_end_to_end_prints_no_data(self):
        code, text, opener = self.run_probe([], FakeResponse(BODY), secret="٧٠٠١٢٧٢١٢٤")
        self.assertEqual(code, 0)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("skeleton", text)
        self.assertIn("requests sent to Wathq: 1", text)
        no_data_in(self, text)

    def test_probe_asks_and_respects_no(self):
        code, text, opener = self.run_probe([UNIFIED], FakeResponse(BODY), stdin="n\n")
        self.assertEqual((code, opener.requests), (1, []))
        code, text, opener = self.run_probe([UNIFIED], FakeResponse(BODY), stdin="")
        self.assertEqual((code, opener.requests), (1, []))              # EOF = no

    def test_probe_exit_code_for_an_unusable_answer(self):
        code, text, _ = self.run_probe([UNIFIED, "--yes"], FakeResponse(b"<html><title>x</title></html>"))
        self.assertEqual(code, 3)
        self.assertIn("the app shows this as", text)

    def test_probe_sandbox_cr_sends_nothing(self):
        code, text, opener = self.run_probe(["1010123456", "--yes"], FakeResponse(BODY),
                                            environ={"WATHQ_API_KEY": KEY, "WATHQ_ENV": "sandbox"})
        self.assertEqual((code, opener.requests), (2, []))

    def test_probe_names_the_failed_step_with_conversion_wording(self):
        from urllib.error import HTTPError
        err = HTTPError("https://x", 403, "f", {}, io.BytesIO(b'{"code":"403.1.1","message":"x"}'))
        code, text, _ = self.run_probe(["1010123456", "--yes"], err)
        self.assertEqual(code, 1)
        self.assertIn("conversion call failed", text)
        self.assertIn("السجل التجاري", text)

    def test_probe_hint_when_the_key_is_missing(self):
        code, text, opener = self.run_probe([UNIFIED, "--yes"], FakeResponse(BODY), environ={})
        self.assertEqual((code, opener.requests), (2, []))
        self.assertIn("GetEnvironmentVariable", text)


class RouterAnswerTests(unittest.TestCase):
    def make(self, answer):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from wathq_api import create_router
        client = WathqClient(opener=Opener(FakeResponse(answer)), environ={"WATHQ_API_KEY": KEY})
        app = FastAPI()
        app.include_router(create_router(client, environ={}))
        return TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 1))

    def post(self, http):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = http.post("/wathq/company-contract", json={"number": UNIFIED},
                          headers={"X-Wathq-Request": "1"})
        return r, out.getvalue()

    def test_a_2xx_error_body_maps_like_an_http_error(self):
        r, log = self.post(self.make(b'{"code":"404.2.1","message":"No Results Found"}'))
        self.assertEqual((r.status_code, r.json()["error"], r.json()["code"]), (404, MSG_NOT_FOUND, "404.2.1"))
        r, log = self.post(self.make(b'{"code":"403.1.1","message":"x"}'))
        self.assertEqual((r.status_code, r.json()["error"]), (502, MSG_FORBIDDEN))

    def test_an_envelope_without_entity_logs_its_keys_only(self):
        r, log = self.post(self.make(json.dumps({"data": CONTRACT}, ensure_ascii=False).encode()))
        self.assertEqual(r.status_code, 502)
        self.assertIn("keys: [data]", log)
        no_data_in(self, log)

    def test_lone_surrogates_do_not_500_or_poison_the_cache(self):
        raw = BODY.replace("شركة الاختبار للتجارة".encode(), b"Test \\ud83d Co")
        http = self.make(raw)
        for _ in range(2):
            r, _ = self.post(http)
            self.assertEqual(r.status_code, 200)
            self.assertIn("Test", r.json()["entity"]["name"])


if __name__ == "__main__":
    unittest.main()
