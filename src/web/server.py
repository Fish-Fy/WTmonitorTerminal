"""只读 Web 镜像服务(stdlib http.server, 零新依赖; 用户 2026-10-07 定稿只读镜像)。

安全边界(硬性):
  * 只读: 没有任何写路由(POST/PUT/DELETE 一律 405), 手机端改不了任何东西;
  * 不转发 8111 原始端点: 那个接口本来就无鉴权, 这里绝不再暴露一层;
  * 默认关(config web.enabled=false), 开也只建议家庭局域网; 绑定地址由用户自己负责。
  * 告警音频文件路径来自本机 config —— 只读该文件本身, 不接受路径参数(防遍历)。

路由:
  GET /                单页(index.html, 注入 theme 的 :root 色板)
  GET /static/app.js   前端逻辑   GET /static/style.css  前端样式
  GET /api/snapshot    当前全量 JSON(中途进场的补帧)
  GET /api/stream      SSE 事件流(objects 2Hz 节流 / roster / threat / combat / reset)
  GET /map.png         当前底图(无底图 404)
  GET /frame.jpg       电脑地图视口的当前 JPEG 画面（含标点、轨迹和视口叠加项）
  GET /api/sound       告警音频字节(未配置 404)
"""
from __future__ import annotations

import json
import logging
import threading
import queue
import time
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from ..ui.theme import css_variables
from .broadcaster import LiveBus
from .copy import render_copy

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_THEME_MARK = "<!--THEME-->"
_KEEPALIVE = b": keep-alive\n\n"


class _MirrorHTTPServer(ThreadingHTTPServer):
    # Windows 的 SO_REUSEADDR 允许另一进程抢占已监听的端口，必须使用独占绑定。
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _make_handler(bus: LiveBus, sound_file: Callable[[], str], stopped: threading.Event,
                  clients: set, clients_lock: threading.Lock, locale: str):
    static_files = {
        "/static/app.js": (STATIC_DIR / "app.js", "text/javascript; charset=utf-8"),
        "/static/style.css": (STATIC_DIR / "style.css", "text/css; charset=utf-8"),
        "/static/index.html": (STATIC_DIR / "index.html", "text/html; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "WTMirror/1.0"
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(5)
            with clients_lock:
                clients.add(self.connection)

        def finish(self):
            try:
                super().finish()
            finally:
                with clients_lock:
                    clients.discard(self.connection)

        # ------------------------------------------------------------- 基础
        def log_message(self, fmt, *args):              # 降噪音: 不往 stderr 刷访问日志
            log.debug("mirror %s", fmt % args)

        def _reply(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                   extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.close_connection = True
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._reply(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

        # ------------------------------------------------------------- 路由
        def do_GET(self) -> None:  # noqa: N802 (stdlib 命名)
            try:
                path = urlsplit(self.path).path
                if path == "/" or path == "/index.html":
                    self._index()
                elif path in static_files and path != "/static/index.html":
                    self._static(static_files[path])
                elif path == "/api/snapshot":
                    self._json(bus.snapshot())
                elif path == "/api/stream":
                    self._stream()
                elif path == "/frame.jpg":
                    frame = bus.frame()
                    self._reply(200 if frame else 404, frame or b"no frame yet", "image/jpeg")
                elif path == "/map.png":
                    self._map_png()
                elif path == "/api/sound":
                    self._sound()
                else:
                    self._reply(404, b"not found")
            except (ConnectionError, TimeoutError):
                pass                                    # 客户端中途断开: 正常
            except Exception:
                log.exception("镜像请求处理失败: %s", self.path)
                try:
                    self._reply(500, b"internal error")
                except Exception:
                    pass

        def do_POST(self) -> None:  # noqa: N802
            self.close_connection = True
            self._reply(405, b"read-only mirror", extra={"Allow": "GET"})

        do_PUT = do_DELETE = do_PATCH = do_POST

        # ------------------------------------------------------------- 页面/静态
        def _index(self) -> None:
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            # 色板唯一来源是桌面 theme.py: 页面加载即拿到同一套令牌, 两边不漂移
            html = html.replace(_THEME_MARK, f"<style>{css_variables()}</style>")
            html = render_copy(html, locale)
            self._reply(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def _static(self, entry) -> None:
            path, ctype = entry
            self._reply(200, path.read_bytes(), ctype)

        # ------------------------------------------------------------- 数据
        def _map_png(self) -> None:
            png = bus.map_png()
            if not png:
                self._reply(404, b"no map yet")
                return
            self._reply(200, png, "image/png")

        def _sound(self) -> None:
            path = (sound_file() or "").strip()
            if not path:
                self._reply(404, b"no sound configured")
                return
            p = Path(path)
            if not p.is_file():
                self._reply(404, b"sound file missing")
                return
            ctype = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                     ".ogg": "audio/ogg"}.get(p.suffix.lower(), "application/octet-stream")
            self._reply(200, p.read_bytes(), ctype)

        # ------------------------------------------------------------- SSE
        def _stream(self) -> None:
            try:
                q = bus.subscribe()
            except OverflowError:
                self._reply(429, b"too many viewers")
                return
            try:
                self.connection.settimeout(5)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                # 新客户端先收一份快照, 之后全是增量
                self._sse({"kind": "snapshot", **bus.snapshot()})
                last_heartbeat = time.monotonic()
                while not stopped.is_set():
                    try:
                        msg = q.get(timeout=0.5)
                    except queue.Empty:
                        if time.monotonic() - last_heartbeat >= 10:
                            self.wfile.write(_KEEPALIVE)
                            self.wfile.flush()
                            last_heartbeat = time.monotonic()
                        continue
                    self._sse(msg)
            finally:
                bus.unsubscribe(q)
                self.close_connection = True

        def _sse(self, msg) -> None:
            self.wfile.write(b"data: " + json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.flush()

    return Handler


class MirrorServer:
    """生命周期包装: start() 起后台 serve 线程, stop() 干净退出。"""

    def __init__(self, bus: LiveBus, host: str = "127.0.0.1", port: int = 8321,
                 sound_file: Callable[[], str] | None = None, *, locale: str = "zh-CN"):
        self.bus = bus
        self.host = host
        self.port = int(port)
        self._sound_file = sound_file or (lambda: "")
        self.locale = locale
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()
        self._clients = set()
        self._clients_lock = threading.Lock()

    @property
    def bound_port(self) -> int:
        """实际绑定端口(port=0 时回真实值, 测试用)。"""
        return self._http.server_address[1] if self._http else self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.bound_port}/"

    def start(self) -> None:
        if self._http is not None:
            return
        self._stopped.clear()
        handler = _make_handler(self.bus, self._sound_file, self._stopped,
                                self._clients, self._clients_lock, self.locale)
        self._http = _MirrorHTTPServer((self.host, self.port), handler)
        self._http.daemon_threads = True
        self._thread = threading.Thread(target=self._http.serve_forever,
                                        kwargs={"poll_interval": 0.3},
                                        name="wt-mirror", daemon=True)
        self._thread.start()
        log.info("只读镜像已启动: %s", self.url)

    def stop(self) -> None:
        if self._http is None:
            return
        self._stopped.set()
        self._http.shutdown()
        self._http.server_close()
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
                client.close()
            except OSError:
                pass
        self._http = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
