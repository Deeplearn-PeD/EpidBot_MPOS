"""Browser-based provisioning for the EpidBot app.

Runs a tiny HTTP server on the device so the API key can be pasted from a
phone/laptop browser instead of typed on a touchscreen.

Deliberately uses a plain blocking socket in its own thread instead of the
system asyncio loop: the OS UI loop can starve asyncio tasks for seconds,
while blocking socket calls release the GIL and answer immediately.
Free of lvgl/mpos imports so it is unit-testable on CPython.
"""

import socket as _socket

try:
    import _thread
except ImportError:
    _thread = None

DEFAULT_PORT = 8126
MAX_BODY = 16384
TIMEOUT_S = 300

_PAGE_HEAD = (
    "HTTP/1.0 %s\r\n"
    "Content-Type: text/html; charset=utf-8\r\n"
    "Connection: close\r\n"
    "Content-Length: %d\r\n"
    "\r\n"
)

_FORM = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>EpidBot setup</title><style>
body{font-family:sans-serif;max-width:460px;margin:2em auto;padding:0 1em;color:#1b1f24}
input,select{width:100%;padding:11px;margin:4px 0 14px;box-sizing:border-box;font-size:16px;border:1px solid #ccd;border-radius:6px}
button{width:100%;padding:13px;font-size:17px;background:#1e88e5;color:#fff;border:0;border-radius:6px}
code{background:#eef;padding:1px 4px;border-radius:4px}
</style></head><body><h2>EpidBot setup</h2>
<p>Paste your API key (starts with <code>ek_</code>) and the 4-digit PIN shown on the device screen.</p>
<form method="POST" action="/submit">
<label>API key</label><input name="api_key" placeholder="ek_..." autocomplete="off" required>
<label>Server URL</label><input name="server_url" value="@SERVER_URL@">
<label>Language</label><select name="locale">@LOCALES@</select>
<label>PIN (on device screen)</label><input name="pin" inputmode="numeric" autocomplete="off" required>
<button>Save to device</button></form></body></html>"""

_SAVED = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>EpidBot</title><style>body{font-family:sans-serif;max-width:460px;margin:3em auto;padding:0 1em;text-align:center}h2{color:@COLOR@}</style></head><body><h2>@MSG@</h2><p>@DETAIL@</p></body></html>"""

LOCALES = [("English", "en"), ("Portugues", "pt"), ("Espanol", "es")]

_ACCEPT_TIMEOUT_S = 2
_REQUEST_TIMEOUT_S = 8


def urldecode(text):
    if not text:
        return ""
    out = bytearray()
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "+":
            out.append(0x20)
            i += 1
        elif c == "%" and i + 2 < n:
            try:
                out.append(int(text[i + 1 : i + 3], 16))
                i += 3
            except ValueError:
                out.append(ord(c))
                i += 1
        else:
            out.append(ord(c) & 0xFF)
            i += 1
    try:
        return bytes(out).decode("utf-8")
    except Exception:
        return bytes(out).decode("latin-1")


def parse_form(body):
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8")
        except Exception:
            body = body.decode("latin-1")
    fields = {}
    for pair in body.split("&"):
        if not pair:
            continue
        if "=" in pair:
            key, value = pair.split("=", 1)
        else:
            key, value = pair, ""
        fields[urldecode(key)] = urldecode(value)
    return fields


def html_escape(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_form(server_url, locale="en"):
    options = []
    for label, code in LOCALES:
        selected = " selected" if code == locale else ""
        options.append('<option value="%s"%s>%s</option>' % (code, selected, label))
    return _FORM.replace("@SERVER_URL@", html_escape(server_url)).replace(
        "@LOCALES@", "".join(options)
    )


def build_page(ok, message, detail=""):
    color = "#2e7d32" if ok else "#b00020"
    return (
        _SAVED.replace("@COLOR@", color)
        .replace("@MSG@", html_escape(message))
        .replace("@DETAIL@", html_escape(detail))
    )


def _is_private_ip(ip):
    if ip.startswith("192.168.") or ip.startswith("10."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (ValueError, IndexError):
            return False
    return False


def _local_ip_from_proc():
    with open("/proc/net/route") as f:
        route_lines = f.read().split("\n")
    has_default = False
    for line in route_lines[1:]:
        parts = line.split()
        if len(parts) > 2 and parts[1] == "00000000":
            has_default = True
            break
    if not has_default:
        return None
    local_ips = []
    with open("/proc/net/fib_trie") as f:
        prev = None
        for line in f:
            line = line.strip()
            if line.startswith("|--"):
                prev = line[3:].strip()
            elif "/32 host LOCAL" in line and prev:
                local_ips.append(prev)
                prev = None
    best = None
    for ip in local_ips:
        if ":" in ip or ip.startswith("127.") or not ip:
            continue
        if ip.startswith("192.168."):
            return ip
        if best is None:
            best = ip
    return best


def get_local_ip():
    try:
        import network

        wlan = network.WLAN(network.STA_IF)
        if wlan.isconnected():
            return wlan.ifconfig()[0]
    except Exception:
        pass
    try:
        return _local_ip_from_proc()
    except Exception:
        pass
    return None


def _errno(e):
    errno = getattr(e, "errno", None)
    if errno is None and e.args:
        errno = e.args[0]
    return errno


class ProvisionServer:
    """Serves the setup form; on valid POST stores the result and stops."""

    def __init__(self, pin, port=DEFAULT_PORT, server_url="", locale="en"):
        self.pin = str(pin)
        self.port = port
        self.server_url = server_url
        self.locale = locale
        self.result = None
        self.active = False
        self._sock = None

    def start(self):
        """Bind, listen and spawn the serving thread. Returns the URL."""
        self._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self._sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        addr = _socket.getaddrinfo("0.0.0.0", self.port)[0][4]
        self._sock.bind(addr)
        self._sock.listen(2)
        self._sock.settimeout(_ACCEPT_TIMEOUT_S)
        self.active = True
        if _thread is not None:
            try:
                try:
                    from mpos import TaskManager as _TM

                    _thread.stack_size(_TM.good_stack_size())
                except Exception:
                    pass
                _thread.start_new_thread(self._serve, ())
            except Exception:
                self.active = False
                raise
        return self.url()

    def url(self, host=None):
        host = host or get_local_ip() or "127.0.0.1"
        return "http://%s:%d/" % (host, self.port)

    def stop(self):
        self.active = False
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _serve(self):
        while self.active:
            try:
                conn, _addr = self._sock.accept()
            except OSError as e:
                if not self.active:
                    break
                if _errno(e) in (11, 110, 10060, 10035):
                    continue
                break
            try:
                self._handle_conn(conn)
            except OSError as e:
                if _errno(e) not in (104, 32, 107):
                    try:
                        print("ProvisionServer request error:", e)
                    except Exception:
                        pass
            except Exception as e:
                try:
                    print("ProvisionServer request error:", e)
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
        try:
            self._sock.close()
        except Exception:
            pass

    @staticmethod
    def _readline(conn):
        buf = b""
        while len(buf) < 2048:
            c = conn.recv(1)
            if not c:
                break
            buf += c
            if c == b"\n":
                break
        return buf

    def _handle_conn(self, conn):
        conn.settimeout(_REQUEST_TIMEOUT_S)
        line = self._readline(conn)
        if not line:
            return
        parts = line.split()
        if len(parts) < 2:
            return
        method = parts[0].upper()
        path = parts[1]
        content_length = 0
        for _ in range(16):
            header = self._readline(conn)
            if not header or header in (b"\r\n", b"\n"):
                break
            if header[:15].lower() == b"content-length:":
                try:
                    content_length = int(header.split(b":", 1)[1])
                except ValueError:
                    content_length = 0
        body = b""
        if content_length > 0:
            content_length = min(content_length, MAX_BODY)
            while len(body) < content_length:
                chunk = conn.recv(content_length - len(body))
                if not chunk:
                    break
                body += chunk

        if method == b"GET" and path in (b"/", b"/index.html"):
            self._send(conn, "200 OK", build_form(self.server_url, self.locale))
        elif method == b"POST" and path in (b"/submit", b"/submit?"):
            fields = parse_form(body)
            if fields.get("pin", "").strip() != self.pin:
                self._send(
                    conn,
                    "403 Forbidden",
                    build_page(
                        False, "Wrong PIN", "Check the 4-digit PIN on the device screen."
                    ),
                )
            elif not fields.get("api_key", "").strip():
                self._send(
                    conn,
                    "400 Bad Request",
                    build_page(False, "Missing API key", "The API key field is required."),
                )
            else:
                locale = fields.get("locale", self.locale)
                if locale not in [code for _, code in LOCALES]:
                    locale = self.locale
                self.result = {
                    "api_key": fields["api_key"].strip(),
                    "server_url": fields.get("server_url", "").strip(),
                    "locale": locale,
                }
                self.active = False
                self._send(conn, "200 OK", build_page(True, "Saved!", "You can close this page."))
        else:
            self._send(conn, "404 Not Found", build_page(False, "Not found"))

    @staticmethod
    def _send(conn, code, html):
        body = html.encode("utf-8")
        payload = (_PAGE_HEAD % (code, len(body))).encode("ascii") + body
        send = getattr(conn, "sendall", None)
        if send is None:
            send = getattr(conn, "write", None) or conn.send
        view = memoryview(payload)
        while view:
            sent = send(view)
            view = view[sent:]
