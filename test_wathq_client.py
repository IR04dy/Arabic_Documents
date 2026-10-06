"""WathqClient against a fake opener: no request ever leaves this machine."""
import contextlib
import io
import json
import os
import socket
import ssl
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from wathq_client import (MSG_BAD_KEY, MSG_CONVERT_FORBIDDEN, MSG_CONVERT_NOT_FOUND,
                          MSG_CONVERT_REJECTED, MSG_FORBIDDEN, MSG_INVALID, MSG_KEY_FORMAT,
                          MSG_NO_SANDBOX_CONVERSION, MSG_NOT_CONFIGURED, MSG_NOT_FOUND,
                          MSG_RATE, MSG_TIMEOUT, MSG_TLS, MSG_TOO_LARGE, MSG_UNREACHABLE,
                          MAX_RESPONSE, WathqClient, WathqError, _NoRedirect)

KEY = "TESTKEY-0123456789abcdef"
UNIFIED = "7001272124"
LEGACY = "1023236575"


class Response(io.BytesIO):
    status = 200


class FakeOpener:
    """Records each Request and plays back one scripted outcome per call."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, (dict, list)):
            outcome = json.dumps(outcome).encode()
        return Response(outcome)


def http_error(status, body=b""):
    return HTTPError("https://api.wathq.sa/x", status, "err", {}, io.BytesIO(body))


def make(*outcomes, env="production", environ=None, **kw):
    opener = FakeOpener(*outcomes)
    environ = {"WATHQ_API_KEY": KEY} if environ is None else environ
    return WathqClient(env=env, opener=opener, environ=environ, **kw), opener


class RequestShapeTests(unittest.TestCase):
    def test_contract_url_header_and_query(self):
        client, opener = make({"entity": {}})
        client.company_contract(UNIFIED, "en")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url,
                         "https://api.wathq.sa/company-contract/info/7001272124?language=en")
        self.assertEqual(request.get_method(), "GET")
        # The key rides as an UNREDIRECTED header: never forwarded anywhere.
        self.assertEqual(request.unredirected_hdrs.get("Apikey"), KEY)
        self.assertNotIn("Apikey", request.headers)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(timeout, client.timeout)

    def test_copy_number_is_sent_when_given(self):
        client, opener = make({"entity": {}})
        client.company_contract(UNIFIED, "ar", copy_number="3")
        self.assertTrue(opener.requests[0][0].full_url.endswith("?language=ar&copyNumber=3"))

    def test_sandbox_base_url(self):
        client, opener = make({"entity": {}}, env="sandbox")
        client.company_contract(UNIFIED)
        self.assertTrue(opener.requests[0][0].full_url.startswith(
            "https://api.wathq.sa/sandbox/company-contract/info/"))

    def test_conversion_endpoint_and_answer(self):
        client, opener = make({"crNationalNumber": "7001272475"})
        self.assertEqual(client.national_number(LEGACY), "7001272475")
        self.assertEqual(opener.requests[0][0].full_url,
                         "https://api.wathq.sa/commercial-registration/crNationalNumber/1023236575")

    def test_conversion_rejects_an_answer_without_a_unified_number(self):
        client, _ = make({"crNationalNumber": "1023236575"})
        with self.assertRaises(WathqError) as ctx:
            client.national_number(LEGACY)
        self.assertEqual(ctx.exception.message, MSG_INVALID)

    def test_conversion_is_refused_in_sandbox_without_a_call(self):
        client, opener = make(env="sandbox")
        with self.assertRaises(WathqError) as ctx:
            client.national_number(LEGACY)
        self.assertEqual(ctx.exception.message, MSG_NO_SANDBOX_CONVERSION)
        self.assertEqual(opener.requests, [])
        self.assertEqual(client.sent, 0)

    def test_invalid_numbers_never_reach_the_network(self):
        client, opener = make()
        for bad in ("1023236575", "700127212", "70012721245", "70/1272124", "", None):
            with self.assertRaises(ValueError):
                client.company_contract(bad)
        for bad in ("123", "0123456789", "../../x", "8001272124", "7101234567", "7001272124"):
            with self.assertRaises(ValueError):
                client.national_number(bad)
        with self.assertRaises(ValueError):
            client.company_contract(UNIFIED, "fr")
        with self.assertRaises(ValueError):
            client.company_contract(UNIFIED, copy_number="123456")
        self.assertEqual(opener.requests, [])

    def test_conversion_errors_name_the_conversion(self):
        cases = [
            (http_error(403), MSG_CONVERT_FORBIDDEN, 502),
            (http_error(403, b'{"code":"403.1.1","message":"x"}'), MSG_CONVERT_FORBIDDEN, 502),
            (http_error(400, b'{"code":"400.1.8","message":"x"}'), MSG_CONVERT_REJECTED, 400),
            (http_error(404, b'{"code":"404.2.1","message":"x"}'), MSG_CONVERT_NOT_FOUND, 404),
            (http_error(401), MSG_BAD_KEY, 502),               # not conversion-specific
        ]
        for outcome, message, status in cases:
            client, _ = make(outcome)
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(WathqError) as ctx:
                    client.national_number(LEGACY)
            self.assertEqual((ctx.exception.message, ctx.exception.status), (message, status))

    def test_failures_before_sending_are_not_counted(self):
        import socket as _s
        client, _ = make(URLError(_s.gaierror()), URLError(ConnectionRefusedError()),
                         URLError(ssl.SSLCertVerificationError("x")), URLError(ConnectionResetError()))
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(4):
                with self.assertRaises(WathqError):
                    client.company_contract(UNIFIED)
        self.assertEqual(client.sent, 1)                        # only the reset may have reached Wathq

    def test_sent_counts_requests(self):
        client, _ = make({"entity": {}}, http_error(404))
        client.company_contract(UNIFIED)
        with self.assertRaises(WathqError):
            client.company_contract(UNIFIED)
        self.assertEqual(client.sent, 2)


class KeyTests(unittest.TestCase):
    def test_missing_key_fails_before_any_request(self):
        client, opener = make(environ={})
        self.assertFalse(client.configured())
        self.assertEqual(client.key_problem(), MSG_NOT_CONFIGURED)
        with self.assertRaises(WathqError) as ctx:
            client.company_contract(UNIFIED)
        self.assertEqual((ctx.exception.message, ctx.exception.status), (MSG_NOT_CONFIGURED, 503))
        self.assertEqual((opener.requests, client.sent), ([], 0))

    def test_key_with_spaces_or_non_ascii_is_refused(self):
        for bad in ("has space inside", "مفتاح-عربي-123", "short"):
            client, opener = make(environ={"WATHQ_API_KEY": bad})
            self.assertEqual(client.key_problem(), MSG_KEY_FORMAT)
            self.assertEqual(opener.requests, [])

    def test_key_file_is_read_per_call_and_trimmed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wathq.key")
            with open(path, "w", encoding="utf-8-sig") as fh:
                fh.write(KEY + "\r\n")
            client, opener = make({"entity": {}}, environ={"WATHQ_API_KEY_FILE": path})
            client.company_contract(UNIFIED)
            self.assertEqual(opener.requests[0][0].unredirected_hdrs["Apikey"], KEY)
            os.remove(path)
            self.assertFalse(client.configured())

    def test_key_file_saved_by_windows_powershell_is_read(self):
        # `'KEY' > wathq.key` in Windows PowerShell 5.1 writes UTF-16 with a BOM.
        for encoding in ("utf-16", "utf-16-be", "utf-8-sig", "utf-8"):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "wathq.key")
                data = (KEY + "\r\n").encode(encoding)
                if encoding == "utf-16-be":
                    data = b"\xfe\xff" + data
                with open(path, "wb") as fh:
                    fh.write(data)
                client, opener = make({"entity": {}}, environ={"WATHQ_API_KEY_FILE": path})
                client.company_contract(UNIFIED)
                self.assertEqual(opener.requests[0][0].unredirected_hdrs["Apikey"], KEY, encoding)

    def test_env_key_wins_over_file(self):
        client, opener = make({"entity": {}}, environ={"WATHQ_API_KEY": KEY,
                                                       "WATHQ_API_KEY_FILE": "/nonexistent"})
        client.company_contract(UNIFIED)
        self.assertEqual(opener.requests[0][0].unredirected_hdrs["Apikey"], KEY)

    def test_key_never_leaks(self):
        """Not in repr, not in any error, not in anything printed."""
        outcomes = [http_error(s, json.dumps({"code": f"{s}.1.1", "message": KEY}).encode())
                    for s in (400, 401, 403, 404, 409, 429, 500, 502, 503)]
        outcomes += [URLError(OSError(KEY)), URLError(ssl.SSLError(KEY)), socket.timeout(KEY),
                     ConnectionResetError(KEY), b"not json " + KEY.encode()]
        client, _ = make(*outcomes)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(len(outcomes)):
                with self.assertRaises(WathqError) as ctx:
                    client.company_contract(UNIFIED)
                exc = ctx.exception
                for text in (str(exc), repr(exc), exc.message, exc.code, repr(exc.args)):
                    self.assertNotIn(KEY, text)
                self.assertIsNone(exc.__cause__)
        self.assertNotIn(KEY, out.getvalue())
        self.assertNotIn(UNIFIED, out.getvalue())
        self.assertNotIn(KEY, repr(client))


class ErrorMappingTests(unittest.TestCase):
    def status_of(self, outcome):
        client, _ = make(outcome)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(WathqError) as ctx:
                client.company_contract(UNIFIED)
        return ctx.exception

    def test_http_statuses(self):
        cases = {
            401: (MSG_BAD_KEY, 502),
            403: (MSG_FORBIDDEN, 502),
            404: (MSG_NOT_FOUND, 404),
            429: (MSG_RATE, 429),
        }
        for status, expected in cases.items():
            exc = self.status_of(http_error(status))
            self.assertEqual((exc.message, exc.status), expected, status)
        self.assertEqual(self.status_of(http_error(500)).status, 502)
        self.assertEqual(self.status_of(http_error(302)).status, 502)   # redirect refused

    def test_an_empty_500_is_explained_as_a_probable_no_match(self):
        from wathq_client import MSG_EMPTY_500, MSG_UPSTREAM
        empty = self.status_of(http_error(500, b""))
        self.assertEqual((empty.message, empty.status), (MSG_EMPTY_500, 502))
        self.assertEqual(self.status_of(http_error(500, b"<html>oops</html>")).message, MSG_UPSTREAM)
        self.assertEqual(self.status_of(http_error(503, b"")).message, MSG_UPSTREAM)

    def test_wathq_code_is_kept_and_404_code_inside_400_means_not_found(self):
        exc = self.status_of(http_error(400, b'{"code":"404.2.1","message":"No Results Found"}'))
        self.assertEqual((exc.message, exc.status, exc.code), (MSG_NOT_FOUND, 404, "404.2.1"))
        exc = self.status_of(http_error(400, b"<error><code>400.1.3</code><message>x</message></error>"))
        self.assertEqual((exc.status, exc.code), (400, "400.1.3"))

    def test_garbage_codes_are_dropped(self):
        exc = self.status_of(http_error(400, b'{"code":"<script>","message":"x"}'))
        self.assertEqual(exc.code, "")

    def test_transport_failures(self):
        self.assertEqual(self.status_of(URLError(ssl.SSLCertVerificationError("bad"))).message, MSG_TLS)
        self.assertEqual(self.status_of(URLError(socket.timeout())).message, MSG_TIMEOUT)
        self.assertEqual(self.status_of(socket.timeout()).message, MSG_TIMEOUT)
        self.assertEqual(self.status_of(URLError(ConnectionRefusedError())).message, MSG_UNREACHABLE)
        self.assertEqual(self.status_of(ConnectionResetError()).message, MSG_UNREACHABLE)

    def test_bad_bodies(self):
        self.assertEqual(self.status_of(b"<html>").message, MSG_INVALID)
        self.assertEqual(self.status_of(b"x" * (MAX_RESPONSE + 1)).message, MSG_TOO_LARGE)

    def test_wathq_wording_is_kept_for_the_user_with_numbers_redacted(self):
        from wathq_client import public_message
        exc = self.status_of(http_error(403, b'{"code":"403.1.1","message":"you do not have permission for giving resource"}'))
        self.assertEqual(exc.detail, "you do not have permission for giving resource")
        exc = self.status_of(http_error(400, b'{"code":"400.1.5","message":"No record for 7001234567 found"}'))
        self.assertEqual(exc.detail, "No record for ••••••4567 found")
        exc = self.status_of(http_error(400, b"<error><code>400.1.3</code><message>Bad input</message></error>"))
        self.assertEqual(exc.detail, "Bad input")
        exc = self.status_of(http_error(500, b"<html><body><h1>Internal error</h1></body></html>"))
        self.assertEqual(exc.detail, "")                     # no Wathq wording: nothing invented
        self.assertEqual(public_message("<b>x</b>\n ١٢٣٤٥٦٧٨٩٠ y", 12), "x ••••••7890")

    def test_debug_logs_the_error_body_only_when_asked(self):
        body = b'{"code":"403.1.1","message":"no permission"}'
        for flag, expected in (("", False), ("1", True)):
            client, _ = make(http_error(403, body), environ={"WATHQ_API_KEY": KEY, "WATHQ_DEBUG": flag})
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(WathqError):
                client.company_contract(UNIFIED)
            self.assertIn("HTTP 403 403.1.1 | no permission", out.getvalue())
            self.assertEqual("error body:" in out.getvalue(), expected, flag)
            self.assertNotIn(KEY, out.getvalue())


class OpenerTests(unittest.TestCase):
    def test_default_opener_ignores_env_proxies_and_refuses_redirects(self):
        saved = os.environ.get("HTTPS_PROXY")
        os.environ["HTTPS_PROXY"] = "http://evil.example:8080"
        try:
            client = WathqClient(environ={"WATHQ_API_KEY": KEY})
        finally:
            if saved is None:
                os.environ.pop("HTTPS_PROXY", None)
            else:
                os.environ["HTTPS_PROXY"] = saved
        handlers = client._opener.handlers
        # ProxyHandler({}) registers no *_open method, so the opener drops it —
        # and, being passed explicitly, it also keeps build_opener's default
        # env-reading ProxyHandler out. Either way: no proxy anywhere.
        proxies = [h.proxies for h in handlers if isinstance(h, ProxyHandler)]
        self.assertFalse(any(proxies), proxies)
        self.assertTrue(any(isinstance(h, _NoRedirect) for h in handlers))

    def test_explicit_proxy_is_used(self):
        client = WathqClient(environ={"WATHQ_PROXY_URL": "http://proxy.local:3128"})
        proxies = [h.proxies for h in client._opener.handlers if isinstance(h, ProxyHandler)]
        self.assertEqual(proxies, [{"https": "http://proxy.local:3128"}])

    def test_bad_settings_are_rejected_at_construction(self):
        for environ in ({"WATHQ_ENV": "staging"}, {"WATHQ_PROXY_URL": "ftp://x"},
                        {"WATHQ_PROXY_URL": "http://x/path?q"}, {"WATHQ_TIMEOUT": "0"},
                        {"WATHQ_TIMEOUT": "abc"}):
            with self.assertRaises(ValueError, msg=environ):
                WathqClient(environ=environ)

    def test_ca_bundle_adds_to_the_system_store(self):
        import ssl as _ssl
        default_count = len(_ssl.create_default_context().get_ca_certs())
        bundle = _ssl.get_default_verify_paths().cafile
        if not bundle or not os.path.exists(bundle):
            import certifi                      # a real PEM bundle to load
            bundle = certifi.where()
        client = WathqClient(environ={"WATHQ_CA_BUNDLE": bundle})
        from urllib.request import HTTPSHandler
        context = [h for h in client._opener.handlers if isinstance(h, HTTPSHandler)][0]._context
        self.assertGreaterEqual(len(context.get_ca_certs()), default_count)
        self.assertGreater(len(context.get_ca_certs()), 0)

    def test_bad_ca_bundle_is_a_setting_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk.pem")
            with open(junk, "w") as fh:
                fh.write("not a certificate")
            for path in (os.path.join(tmp, "missing.pem"), junk, tmp):
                with self.assertRaises(ValueError, msg=path):
                    WathqClient(environ={"WATHQ_CA_BUNDLE": path})

    def test_deeply_nested_answer_is_invalid_not_a_crash(self):
        client, _ = make(b"[" * 5000 + b"]" * 5000)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(WathqError) as ctx:
                client.company_contract(UNIFIED)
        self.assertEqual(ctx.exception.message, MSG_INVALID)

    def test_tls_verification_is_on(self):
        client = WathqClient(environ={})
        from urllib.request import HTTPSHandler
        https = [h for h in client._opener.handlers if isinstance(h, HTTPSHandler)]
        context = https[0]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertGreaterEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)


if __name__ == "__main__":
    unittest.main()
