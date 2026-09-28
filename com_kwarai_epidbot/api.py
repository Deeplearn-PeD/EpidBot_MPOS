"""EpidBot REST API client for MicroPythonOS (async, aiohttp-based).

Kept free of mpos imports so the pure logic is unit-testable on CPython.
"""

try:
    import ujson as json
except ImportError:
    import json

import os

API_PREFIX = "/api/v1"
DEFAULT_SERVER = "https://api.epidbot.kwar-ai.com.br"

CHAT_TIMEOUT_S = 30
VOICE_TIMEOUT_S = 120
POLL_INTERVAL_S = 2
POLL_TIMEOUT_S = 180

DEFAULT_TTS_MAX_CHARS = 800
MAX_AUDIO_BYTES = 6_000_000
MAX_BODY_BYTES = 4_000_000
DISPLAY_CAP = 2000

TERMINAL_STATUSES = ("completed", "failed", "cancelled")


class EpidBotError(Exception):
    pass


class NetworkError(EpidBotError):
    pass


class AuthError(EpidBotError):
    pass


class QuotaError(EpidBotError):
    pass


class ServerError(EpidBotError):
    pass


def _ssl_context():
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.verify_mode = ssl.CERT_OPTIONAL
    return ctx


async def http_request(method, url, headers=None, body=None, timeout_s=CHAT_TIMEOUT_S):
    """Free-of-blocking HTTP request; returns (status_code, body_bytes).

    A dict body is sent via the bundled aiohttp's native json= parameter
    (passing bytes would get Content-Type overridden to octet-stream by the
    library, which FastAPI rejects with 422).
    """
    import aiohttp

    kw = {"headers": headers or {}, "timeout": timeout_s}
    if url.lower().startswith("https"):
        kw["ssl"] = _ssl_context()
    if isinstance(body, dict):
        kw["json"] = body
    elif body is not None:
        kw["data"] = body
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, **kw) as response:
                return response.status, await _read_body(response)
    except EpidBotError:
        raise
    except Exception as e:
        raise NetworkError(str(e) or e.__class__.__name__)


async def _close_writer(writer):
    aclose = getattr(writer, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:
            pass
        return
    close = getattr(writer, "close", None)
    if close is not None:
        try:
            close()
        except Exception:
            pass


async def http_upload(method, url, headers, body, timeout_s=VOICE_TIMEOUT_S):
    """Raw HTTP exchange for pre-encoded bodies (multipart) whose
    Content-Type must survive; the bundled aiohttp cannot send those.
    Returns (status_code, body_bytes)."""
    try:
        import uasyncio as asyncio
    except ImportError:
        import asyncio

    proto, rest = url.split("//", 1)
    secure = proto.lower() == "https:"
    host, _, tail = rest.partition("/")
    path = "/" + tail
    if ":" in host:
        host, _, port_str = host.partition(":")
        port = int(port_str)
    else:
        port = 443 if secure else 80

    async def exchange():
        kw = {"ssl": _ssl_context()} if secure else {}
        reader, writer = await asyncio.open_connection(host, port, **kw)
        try:
            head = "%s %s HTTP/1.0\r\n" % (method, path)
            for key in headers:
                head += "%s: %s\r\n" % (key, headers[key])
            head += "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
            payload = head.encode("ascii") + body
            write = getattr(writer, "write", None)
            if write is not None:
                write(payload)
                drain = getattr(writer, "drain", None)
                if drain is not None:
                    await drain()
            else:
                await writer.awrite(payload)

            status_line = await reader.readline()
            parts = status_line.split(None, 2)
            status = int(parts[1]) if len(parts) > 1 else 0
            content_length = -1
            while True:
                header = await reader.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break
                if header[:15].lower() == b"content-length:":
                    try:
                        content_length = int(header.split(b":", 1)[1])
                    except ValueError:
                        content_length = -1
            out = b""
            if content_length >= 0:
                remaining = content_length
                while remaining > 0:
                    chunk = await reader.read(min(remaining, 4096))
                    if not chunk:
                        break
                    out += chunk
                    remaining -= len(chunk)
            else:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    out += chunk
            return status, out
        finally:
            await _close_writer(writer)

    try:
        return await asyncio.wait_for(exchange(), timeout_s)
    except EpidBotError:
        raise
    except Exception as e:
        raise NetworkError(str(e) or e.__class__.__name__)


async def _read_body(response):
    cls_name = response.__class__.__name__
    if cls_name != "ChunkedClientResponse":
        return await response.read()
    chunks = []
    total = 0
    while True:
        chunk = await response.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            break
    return b"".join(chunks)


async def http_download_to_file(
    method, url, headers=None, body=None, path=None, timeout_s=VOICE_TIMEOUT_S
):
    """Stream an HTTP response body to a file; returns bytes written."""
    import aiohttp

    kw = {"headers": headers or {}, "timeout": timeout_s}
    if url.lower().startswith("https"):
        kw["ssl"] = _ssl_context()
    if isinstance(body, dict):
        kw["json"] = body
    elif body is not None:
        kw["data"] = body
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, **kw) as response:
                if response.status < 200 or response.status >= 300:
                    try:
                        content = await _read_body(response)
                    except Exception:
                        content = b""
                    raise_for_status(response.status, content)
                chunked = response.__class__.__name__ == "ChunkedClientResponse"
                total = 0
                tmp_path = path + ".part"
                f = open(tmp_path, "wb")
                try:
                    while True:
                        if chunked:
                            chunk = await response.read(4096)
                        else:
                            chunk = await response.content.read(4096)
                        if not chunk:
                            break
                        f.write(chunk)
                        total += len(chunk)
                        if total > MAX_AUDIO_BYTES:
                            raise ServerError("Audio response too large")
                finally:
                    f.close()
                if total == 0:
                    raise ServerError("Empty audio response")
                try:
                    os.remove(path)
                except OSError:
                    pass
                os.rename(tmp_path, path)
                return total
    except EpidBotError:
        raise
    except Exception as e:
        try:
            os.remove(path + ".part")
        except OSError:
            pass
        raise NetworkError(str(e) or e.__class__.__name__)


def _detail(content):
    try:
        data = json.loads(content)
    except Exception:
        return None
    if isinstance(data, dict):
        d = data.get("detail") or data.get("message") or data.get("error")
        if isinstance(d, list):
            parts = []
            for item in d[:3]:
                if isinstance(item, dict):
                    loc = ".".join(str(x) for x in item.get("loc", [])[1:])
                    msg = item.get("msg", "")
                    parts.append(("%s: %s" % (loc, msg)) if loc else str(msg))
                else:
                    parts.append(str(item))
            return "; ".join(parts)
        if isinstance(d, dict):
            d = d.get("message")
        if isinstance(d, str):
            return d
    return None


def raise_for_status(status, content=b""):
    if 200 <= status < 300:
        return
    msg = _detail(content)
    if status in (401, 403):
        raise AuthError(msg or "Unauthorized (HTTP %d)" % status)
    if status == 429:
        raise QuotaError(msg or "Daily quota exceeded")
    raise ServerError(msg or "HTTP %d" % status)


def build_multipart(fields, file_field, filename, file_bytes, content_type="audio/wav"):
    try:
        import binascii

        boundary = "mpos" + binascii.hexlify(os.urandom(8)).decode()
    except Exception:
        boundary = "mpos7f3a9c1e2b4d5f60"
    lines = []
    for name in fields:
        lines.append(
            '--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n'
            % (boundary, name, fields[name])
        )
    lines.append(
        '--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
        "Content-Type: %s\r\n\r\n" % (boundary, file_field, filename, content_type)
    )
    head = "".join(lines).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    if not isinstance(file_bytes, (bytes, bytearray)):
        file_bytes = bytes(file_bytes)
    body = b"".join([head, bytes(file_bytes), tail])
    return "multipart/form-data; boundary=%s" % boundary, body


def truncate(text, cap, suffix="\n..."):
    if cap and len(text) > cap:
        return text[:cap] + suffix
    return text


def _strip_inline(s):
    res = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "!" and i + 1 < n and s[i + 1] == "[":
            j = s.find("](", i + 2)
            if j != -1:
                k = s.find(")", j + 2)
                if k != -1:
                    i = k + 1
                    continue
        if c == "[":
            j = s.find("](", i + 1)
            if j != -1:
                k = s.find(")", j + 2)
                if k != -1:
                    res.append(s[i + 1 : j])
                    i = k + 1
                    continue
        if c == "`":
            j = s.find("`", i + 1)
            if j != -1:
                res.append(s[i + 1 : j])
                i = j + 1
                continue
        if c in ("*", "_"):
            if s[i : i + 3] == c * 3:
                i += 3
                continue
            if s[i : i + 2] == c * 2:
                i += 2
                continue
            i += 1
            continue
        res.append(c)
        i += 1
    return "".join(res)


def _collapse_blank(t):
    out = []
    blank = False
    for ln in t.split("\n"):
        if ln.strip() == "":
            if blank:
                continue
            blank = True
            out.append("")
        else:
            blank = False
            out.append(ln)
    return "\n".join(out)


def strip_markdown(text):
    if not text:
        return ""
    out = []
    in_fence = False
    for raw_line in text.split("\n"):
        s = raw_line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            out.append(s)
            continue
        if not s:
            out.append("")
            continue
        if s in ("---", "***", "___"):
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        if s[:2] in ("- ", "* ", "+ "):
            s = "- " + s[2:].lstrip()
        elif len(s) > 2 and s[0].isdigit() and s[1:3] == ". ":
            s = s
        if "|" in s and "`" not in s:
            s = " ".join(s.replace("|", " ").split())
        out.append(_strip_inline(s))
    return _collapse_blank("\n".join(out)).strip()


class EpidBotClient:
    def __init__(self, api_key, base_url=DEFAULT_SERVER, locale="en"):
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_SERVER).rstrip("/")
        self.locale = locale or "en"

    def _url(self, path):
        return self.base_url + API_PREFIX + path

    def _headers(self, extra=None):
        h = {"X-API-Key": self.api_key}
        if extra:
            for k in extra:
                h[k] = extra[k]
        return h

    async def validate(self):
        status, content = await http_request(
            "GET", self._url("/voice/status"), headers=self._headers(), timeout_s=20
        )
        raise_for_status(status, content)
        try:
            return json.loads(content)
        except Exception:
            return {}

    async def post_chat(self, message, session_id=None, locale=None):
        payload = {"message": message, "locale": locale or self.locale}
        if session_id:
            payload["session_id"] = session_id
        status, content = await http_request(
            "POST",
            self._url("/chat"),
            headers=self._headers({"Content-Type": "application/json"}),
            body=payload,
            timeout_s=CHAT_TIMEOUT_S,
        )
        raise_for_status(status, content)
        return json.loads(content)

    async def get_job(self, job_id):
        status, content = await http_request(
            "GET", self._url("/chat/" + job_id), headers=self._headers(), timeout_s=30
        )
        raise_for_status(status, content)
        return json.loads(content)

    async def chat_and_wait(
        self, message, session_id=None, on_status=None, should_cancel=None
    ):
        try:
            import uasyncio as asyncio
        except ImportError:
            import asyncio

        def _st(s):
            if on_status:
                try:
                    on_status(s)
                except Exception:
                    pass

        _st("submit")
        job = await self.post_chat(message, session_id)
        job_id = job.get("job_id")
        sid = job.get("session_id", session_id)
        _st("think")
        waited = 0
        while True:
            if should_cancel and should_cancel():
                return {"status": "cancelled", "session_id": sid, "job_id": job_id}
            if waited >= POLL_TIMEOUT_S:
                return {"status": "timeout", "session_id": sid, "job_id": job_id}
            await asyncio.sleep(POLL_INTERVAL_S)
            waited += POLL_INTERVAL_S
            data = await self.get_job(job_id)
            data.setdefault("session_id", sid)
            data.setdefault("job_id", job_id)
            if data.get("status") in TERMINAL_STATUSES:
                return data
            _st("think")

    async def transcribe(self, wav_bytes, language=None):
        fields = {}
        if language:
            fields["language"] = language
        ctype, body = build_multipart(fields, "file", "recording.wav", wav_bytes)
        status, content = await http_upload(
            "POST",
            self._url("/voice/transcribe"),
            headers=self._headers({"Content-Type": ctype}),
            body=body,
            timeout_s=VOICE_TIMEOUT_S,
        )
        raise_for_status(status, content)
        try:
            data = json.loads(content)
            return (data or {}).get("text", "") if isinstance(data, dict) else ""
        except Exception:
            return ""

    async def tts_to_file(self, text, path, voice=None, max_chars=None):
        payload = {
            "text": truncate(text, max_chars or DEFAULT_TTS_MAX_CHARS, suffix=""),
            "format": "wav",
        }
        if voice:
            payload["voice"] = voice
        return await http_download_to_file(
            "POST",
            self._url("/voice/tts"),
            headers=self._headers({"Content-Type": "application/json"}),
            body=payload,
            path=path,
            timeout_s=VOICE_TIMEOUT_S,
        )
