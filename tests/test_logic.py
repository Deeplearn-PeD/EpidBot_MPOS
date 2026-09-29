"""CPython unit tests for the EpidBot MPOS app pure logic (api.py)."""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(HERE, "..", "com_kwarai_epidbot")
sys.path.insert(0, APP_DIR)

import api  # noqa: E402
import provision  # noqa: E402


class StripMarkdownTests(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(api.strip_markdown("Hello world"), "Hello world")

    def test_headers(self):
        self.assertEqual(api.strip_markdown("## Dengue in Brazil"), "Dengue in Brazil")
        self.assertEqual(api.strip_markdown("# Title\nbody"), "Title\nbody")

    def test_bold_italic_code(self):
        self.assertEqual(api.strip_markdown("**bold** and *ital*"), "bold and ital")
        self.assertEqual(api.strip_markdown("__b__ _i_"), "b i")
        self.assertEqual(api.strip_markdown("run `pip list` now"), "run pip list now")

    def test_images_removed_links_kept(self):
        self.assertEqual(api.strip_markdown("![plot](plots/x.png)"), "")
        self.assertEqual(
            api.strip_markdown("see [the report](https://x.y) now"),
            "see the report now",
        )

    def test_code_fence_content_kept(self):
        src = "before\n```python\nprint(1)\n```\nafter"
        self.assertEqual(api.strip_markdown(src), "before\nprint(1)\nafter")

    def test_horizontal_rules_removed(self):
        self.assertEqual(api.strip_markdown("a\n---\nb"), "a\nb")

    def test_table_pipes_replaced(self):
        self.assertEqual(api.strip_markdown("| a | b |\n| 1 | 2 |"), "a b\n1 2")

    def test_blank_lines_collapsed(self):
        self.assertEqual(api.strip_markdown("a\n\n\n\nb"), "a\n\nb")

    def test_empty(self):
        self.assertEqual(api.strip_markdown(""), "")
        self.assertEqual(api.strip_markdown(None), "")


class TruncateTests(unittest.TestCase):
    def test_noop(self):
        self.assertEqual(api.truncate("short", 10), "short")

    def test_caps(self):
        self.assertEqual(api.truncate("abcdefghij", 5), "abcde\n...")

    def test_empty_suffix(self):
        self.assertEqual(api.truncate("abcdef", 3, suffix=""), "abc")


class MultipartTests(unittest.TestCase):
    def test_structure(self):
        ctype, body = api.build_multipart(
            {"language": "pt"}, "file", "rec.wav", b"\x00\x01AUDIO"
        )
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1]
        self.assertIn(b'name="language"', body)
        self.assertIn(b"pt", body)
        self.assertIn(b'filename="rec.wav"', body)
        self.assertIn(b"Content-Type: audio/wav", body)
        self.assertIn(b"\x00\x01AUDIO", body)
        self.assertTrue(body.endswith(("--%s--\r\n" % boundary).encode()))
        self.assertEqual(body.count(b"--" + boundary.encode()), 3)


class FakeResponse:
    def __init__(self, status, body, cls="ClientResponse"):
        self.status = status
        self._body = body
        self.__class__.__name__ = cls

    async def read(self, sz=-1):
        if sz in (-1, None):
            return self._body
        out = self._body[:sz]
        self._body = self._body[len(out):]
        if not out:
            raise EOFError
        return out


def patch_http(requests_map):
    """requests_map: {(method, url_suffix): [(status, body_dict_or_bytes), ...]}"""
    calls = []

    async def fake_http_request(method, url, headers=None, body=None, timeout_s=30):
        calls.append((method, url, headers, body))
        for (m, suffix), results in requests_map.items():
            if m == method and url.endswith(suffix):
                status, payload = results.pop(0)
                if isinstance(payload, (bytes, bytearray)):
                    return status, bytes(payload)
                import json as _json

                return status, _json.dumps(payload).encode()
        raise AssertionError("unexpected request: %s %s" % (method, url))

    original = api.http_request
    api.http_request = fake_http_request
    return calls, lambda: setattr(api, "http_request", original)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = api.EpidBotClient("ek_test", "https://srv.example", "pt")
        api.POLL_INTERVAL_S = 0

    def tearDown(self):
        api.POLL_INTERVAL_S = 2

    def test_chat_and_wait_completed(self):
        calls, restore = patch_http(
            {
                ("POST", "/api/v1/chat"): [
                    (200, {"job_id": "j1", "session_id": 42, "status": "processing"})
                ],
                ("GET", "/api/v1/chat/j1"): [
                    (200, {"status": "processing"}),
                    (
                        200,
                        {
                            "status": "completed",
                            "content": "Hi **there**",
                            "images": ["![p](plots/x.png)"],
                        },
                    ),
                ],
            }
        )
        statuses = []

        async def run():
            return await self.client.chat_and_wait(
                "hello",
                session_id=None,
                on_status=statuses.append,
                should_cancel=lambda: False,
            )

        try:
            result = asyncio_run(run())
        finally:
            restore()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["session_id"], 42)
        self.assertIn("job_id", result)
        self.assertIn("submit", statuses)
        self.assertIn("think", statuses)
        method, url, headers, body = calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/api/v1/chat"))
        self.assertEqual(headers["X-API-Key"], "ek_test")
        self.assertIsInstance(body, dict)
        self.assertEqual(body["message"], "hello")
        self.assertEqual(body["locale"], "pt")
        self.assertNotIn("session_id", body)

    def test_chat_and_wait_failed_status(self):
        _, restore = patch_http(
            {
                ("POST", "/api/v1/chat"): [
                    (200, {"job_id": "j2", "session_id": 7, "status": "processing"})
                ],
                ("GET", "/api/v1/chat/j2"): [
                    (200, {"status": "failed", "error": "LLM offline"}),
                ],
            }
        )

        async def run():
            return await self.client.chat_and_wait("x")

        try:
            result = asyncio_run(run())
        finally:
            restore()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "LLM offline")

    def test_chat_and_wait_user_cancel(self):
        _, restore = patch_http(
            {
                ("POST", "/api/v1/chat"): [
                    (200, {"job_id": "j3", "session_id": None, "status": "processing"})
                ],
                ("GET", "/api/v1/chat/j3"): [
                    (200, {"status": "processing"}),
                    (200, {"status": "completed", "content": "late"}),
                ],
            }
        )
        state = {"n": 0}

        def cancel():
            state["n"] += 1
            return state["n"] > 1

        async def run():
            return await self.client.chat_and_wait("x", should_cancel=cancel)

        try:
            result = asyncio_run(run())
        finally:
            restore()
        self.assertEqual(result["status"], "cancelled")

    def test_auth_error(self):
        _, restore = patch_http(
            {("GET", "/api/v1/voice/status"): [(401, {"detail": "bad key"})]}
        )

        async def run():
            await self.client.validate()

        try:
            with self.assertRaises(api.AuthError):
                asyncio_run(run())
        finally:
            restore()

    def test_quota_error(self):
        _, restore = patch_http(
            {("POST", "/api/v1/chat"): [(429, {"detail": "quota exceeded"})]}
        )

        async def run():
            await self.client.post_chat("x")

        try:
            with self.assertRaises(api.QuotaError):
                asyncio_run(run())
        finally:
            restore()

    def test_server_error(self):
        _, restore = patch_http({("POST", "/api/v1/chat"): [(500, b"boom")]})

        async def run():
            await self.client.post_chat("x")

        try:
            with self.assertRaises(api.ServerError):
                asyncio_run(run())
        finally:
            restore()

    def test_transcribe(self):
        calls = []

        async def fake_upload(method, url, headers=None, body=None, timeout_s=120):
            calls.append((method, url, headers, body))
            return 200, b'{"text": "casos de dengue"}'

        original = api.http_upload
        api.http_upload = fake_upload

        async def run():
            return await self.client.transcribe(b"RIFFxxxxWAVE", language="pt")

        try:
            text = asyncio_run(run())
        finally:
            api.http_upload = original
        self.assertEqual(text, "casos de dengue")
        method, url, headers, body = calls[0]
        self.assertIn("multipart/form-data", headers["Content-Type"])
        self.assertIn(b"RIFFxxxxWAVE", body)

    def test_tts_to_file(self):
        captured = {}

        async def fake_download(method, url, headers=None, body=None, path=None, timeout_s=120):
            captured["url"] = url
            captured["body"] = body
            captured["path"] = path
            return 1234

        original = api.http_download_to_file
        api.http_download_to_file = fake_download

        async def run():
            return await self.client.tts_to_file(
                "a" * 100, "/tmp/out.wav", voice="coral", max_chars=10
            )

        try:
            n = asyncio_run(run())
        finally:
            api.http_download_to_file = original

        self.assertEqual(n, 1234)
        self.assertEqual(captured["url"], "https://srv.example/api/v1/voice/tts")
        self.assertEqual(captured["path"], "/tmp/out.wav")
        self.assertEqual(captured["body"]["voice"], "coral")
        self.assertEqual(captured["body"]["format"], "wav")
        self.assertEqual(len(captured["body"]["text"]), 10)

    def test_trailing_slash_base_url(self):
        client = api.EpidBotClient("ek_x", "https://srv.example/")
        self.assertEqual(client._url("/chat"), "https://srv.example/api/v1/chat")


class ProvisionTests(unittest.TestCase):
    def test_urldecode(self):
        self.assertEqual(provision.urldecode("ek%5Fabc+123"), "ek_abc 123")
        self.assertEqual(provision.urldecode("%C3%A1"), "á")
        self.assertEqual(provision.urldecode(""), "")
        self.assertEqual(provision.urldecode("100%"), "100%")

    def test_parse_form(self):
        fields = provision.parse_form(
            b"api_key=ek_abc&server_url=https%3A%2F%2Fnew.example&locale=pt&pin=1234"
        )
        self.assertEqual(
            fields,
            {
                "api_key": "ek_abc",
                "server_url": "https://new.example",
                "locale": "pt",
                "pin": "1234",
            },
        )
        self.assertEqual(provision.parse_form(b""), {})

    def test_build_form(self):
        html = provision.build_form("https://api.example", "pt")
        self.assertIn('value="https://api.example"', html)
        self.assertIn('value="pt" selected', html)
        self.assertIn('name="pin"', html)
        self.assertIn('name="api_key"', html)

    def test_build_page(self):
        ok_page = provision.build_page(True, "Saved!", "Close me")
        self.assertIn("#2e7d32", ok_page)
        self.assertIn("Saved!", ok_page)
        err_page = provision.build_page(False, "Wrong PIN", "try again")
        self.assertIn("#b00020", err_page)
        self.assertIn("try again", err_page)

    def test_html_escape(self):
        self.assertEqual(
            provision.html_escape('a&b<c>"d"'), "a&amp;b&lt;c&gt;&quot;d&quot;"
        )

    def test_http_flow(self):
        import http.client
        import time

        port = 8600 + (os.getpid() % 400)
        bound_host = ["127.0.0.1"]  # updated once the server reports its URL

        def request(method, path, body=None):
            conn = http.client.HTTPConnection(bound_host[0], port, timeout=5)
            headers = {"Content-Length": str(len(body))} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            status = resp.status
            conn.close()
            return status, data

        srv = provision.ProvisionServer(
            "1234", port=port, server_url="https://api.example", locale="en"
        )
        url = srv.start()
        self.assertTrue(url.startswith("http://"))
        bound_host[0] = url.split("//", 1)[1].split(":", 1)[0]
        try:
            deadline = time.time() + 3
            while time.time() < deadline:
                try:
                    status, data = request("GET", "/")
                    break
                except OSError:
                    time.sleep(0.05)
            self.assertEqual(status, 200)
            self.assertIn(b'name="api_key"', data)
            self.assertIn(b"https://api.example", data)
            self.assertIsNone(srv.result)

            status, data = request("POST", "/submit", b"api_key=ek_x&pin=9999")
            self.assertEqual(status, 403)
            self.assertIsNone(srv.result)

            status, data = request("POST", "/submit", b"pin=1234")
            self.assertEqual(status, 400)
            self.assertIsNone(srv.result)

            status, data = request("GET", "/nope")
            self.assertEqual(status, 404)

            status, data = request(
                "POST",
                "/submit",
                b"api_key=ek_abc&server_url=https%3A%2F%2Fnew.example"
                b"&locale=pt&pin=1234",
            )
            self.assertEqual(status, 200)
            self.assertIn(b"Saved!", data)
            self.assertEqual(
                srv.result,
                {
                    "api_key": "ek_abc",
                    "server_url": "https://new.example",
                    "locale": "pt",
                },
            )
            self.assertFalse(srv.active)
        finally:
            srv.stop()


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    unittest.main(verbosity=2)
