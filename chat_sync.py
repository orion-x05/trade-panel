#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六Agent交易面板 · 群聊多端同步服务（局域网）

用途：让手机和电脑都能打开交易面板，并且群聊消息双向同步。
  - 静态托管当前目录：手机浏览器直接访问 http://<电脑IP>:8377/ 即可操作
  - GET  /chat.json  读取群聊快照（前端 pullChatSync 每 30 秒轮询）
  - POST /api/chat   前端把本机新消息推上来，按 id 合并后原子写回 chat.json

服务 index.html 时会注入 window.CHAT_SYNC_ENDPOINT，前端自动开启写入通道；
纯静态部署（GitHub Pages 等）不注入，前端自动降级为"仅本地 + 只读同步"。

仅用标准库，无第三方依赖。设计为局域网工具：不鉴权，请勿直接暴露到公网。
"""
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
CHAT_FILE = os.path.join(ROOT, "chat.json")
MAX_MESSAGES = 500
MAX_BODY = 1 << 20  # 单次请求体上限 1MB
PORT = int(os.environ.get("TRADE_PANEL_PORT", "8377"))

_lock = threading.Lock()

CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def load_chat():
    """读取 chat.json，任何异常都按空历史处理（前端静默降级）"""
    try:
        with open(CHAT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    msgs = data.get("messages") if isinstance(data, dict) else data
    return msgs if isinstance(msgs, list) else []


def save_chat(msgs):
    """原子写：先写临时文件再 rename，避免前端读到半截 JSON"""
    tmp = CHAT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"messages": msgs}, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CHAT_FILE)


def merge_messages(incoming, local):
    """按 id 去重（同一 id 保留 ts 较新的一条），按 ts 升序，截断到 MAX_MESSAGES。
    与前端 mergeChatHistories 保持同一套规则，两端合并结果一致。"""
    by_id = {}
    for m in (local or []) + (incoming or []):
        if not isinstance(m, dict) or not m.get("id"):
            continue
        mid = m["id"]
        if mid not in by_id or (m.get("ts") or 0) >= (by_id[mid].get("ts") or 0):
            by_id[mid] = m
    return sorted(by_id.values(), key=lambda m: m.get("ts") or 0)[-MAX_MESSAGES:]


SYNC_INJECT = (
    b'<script>window.CHAT_SYNC_ENDPOINT="/api/chat";'
    b'window.CHAT_SYNC_URL="/chat.json";</script>'
)


class Handler(BaseHTTPRequestHandler):
    server_version = "TradePanelSync/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stdout.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))
        sys.stdout.flush()

    # ---------- 响应工具 ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        if path.endswith("index.html"):
            # 紧跟 <head> 注入，确保早于页面自身脚本执行
            body = body.replace(b"<head>", b"<head>" + SYNC_INJECT, 1)
        ext = os.path.splitext(path)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", CTYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/chat.json":
            with _lock:
                msgs = load_chat()
            self._send_json({"messages": msgs})
            return
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(ROOT, "index.html"))
            return
        # 静态文件：规范化后必须仍在 ROOT 内，防目录穿越
        safe = os.path.normpath(os.path.join(ROOT, path.lstrip("/")))
        if not safe.startswith(ROOT + os.sep) and safe != ROOT:
            self._send_json({"error": "forbidden"}, 403)
            return
        if os.path.isfile(safe):
            self._send_file(safe)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/chat":
            self._send_json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(min(length, MAX_BODY)) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "bad json"}, 400)
            return
        incoming = payload.get("messages") if isinstance(payload, dict) else payload
        if not isinstance(incoming, list) or not incoming:
            self._send_json({"error": "messages required"}, 400)
            return
        with _lock:
            merged = merge_messages(incoming, load_chat())
            save_chat(merged)
        self._send_json({"ok": True, "count": len(merged)})


def lan_urls(port):
    urls = ["http://127.0.0.1:%d/" % port]
    try:
        import socket
        for info in socket.getaddrinfo(socket.gethostname(), port, socket.AF_INET):
            ip = info[4][0]
            if ip != "127.0.0.1" and not ip.startswith("169.254."):
                urls.append("http://%s:%d/" % (ip, port))
    except OSError:
        pass
    return urls


class SyncServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器/手机端频繁开关连接属于正常现象（keep-alive 被主动断开），
        # 不打 traceback，避免日志被红色异常刷屏
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main():
    if not os.path.isfile(os.path.join(ROOT, "index.html")):
        sys.exit("未找到 index.html，请在 trade-panel 目录内运行本服务")
    if not os.path.isfile(CHAT_FILE):
        save_chat([])
    srv = SyncServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print("=" * 58)
    print(" 六Agent交易面板 · 多端同步服务已启动")
    print(" 手机/电脑请访问（同一局域网内）：")
    for u in lan_urls(PORT):
        print("   " + u)
    print(" 群聊写入通道已自动开启（POST /api/chat -> chat.json）")
    print(" 停止：Ctrl+C    换端口：set TRADE_PANEL_PORT=9000（环境变量）")
    print("=" * 58)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
