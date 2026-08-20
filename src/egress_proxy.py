# src/egress_proxy.py
"""
Tiny local HTTP/HTTPS forward proxy used to prevent the sandboxed Claude
subprocess from reaching cluster-internal services (横向移动 / lateral move).

Design:
- Listens on 127.0.0.1 only — never exposed beyond the pod.
- Default-allow public destinations; default-deny anything that resolves into
  RFC1918 / link-local / CGNAT / K8s service domains / cloud metadata.
- DNS is resolved *once* by the proxy, then we connect to that resolved IP —
  prevents DNS-rebind attacks where the second lookup returns an internal IP.
- Loopback (127.0.0.0/8) is still allowed because the Claude subprocess needs
  to reach the internal API server (/lark-reauth, /session-reset, etc.).
- Logs every block decision; a few proxied requests at DEBUG level.

This is a *defense in depth* layer:
- bwrap (#B) isolates the filesystem and PID/IPC/UTS.
- This proxy narrows the network blast radius.
- A K8s NetworkPolicy from ops is still the recommended outer perimeter.
"""
from __future__ import annotations

import ipaddress
import logging
import select
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

_BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local + cloud metadata (AWS/GCP/Azure)
    ipaddress.ip_network("100.64.0.0/10"),    # carrier-grade NAT
    ipaddress.ip_network("fc00::/7"),         # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]
_BLOCKED_HOSTNAME_SUFFIXES = (
    ".svc.cluster.local",
    ".cluster.local",
    ".internal",                              # GCP internal DNS
)
_BLOCKED_HOSTNAMES = {
    "kubernetes.default",
    "kubernetes.default.svc",
    "metadata.google.internal",
    "metadata",
}


def _resolve_and_check(host: str, port: int) -> tuple[str | None, str | None]:
    """
    Resolve `host` and decide whether the destination is allowed.
    Returns (resolved_ip, block_reason). If block_reason is not None, the
    connection MUST be refused. If resolved_ip is None and reason is None,
    let the caller surface a 502 (DNS failure) to the client.
    """
    host_lower = host.lower().strip("[]")  # strip IPv6 brackets if present

    if host_lower in _BLOCKED_HOSTNAMES:
        return None, f"hostname:{host_lower}"
    for suffix in _BLOCKED_HOSTNAME_SUFFIXES:
        if host_lower.endswith(suffix):
            return None, f"suffix:{suffix}"

    # Caller may pass a literal IP — accept without DNS.
    try:
        ip = ipaddress.ip_address(host_lower)
        for net in _BLOCKED_NETS:
            if ip.version == net.version and ip in net:
                return None, f"ip-literal:{ip} in {net}"
        return str(ip), None
    except ValueError:
        pass  # not a literal — resolve

    try:
        addrs = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        logger.debug(f"[egress-proxy] DNS failed for {host}: {e}")
        return None, None  # caller returns 502, not a security block

    # Pick the first public address. If ANY resolved address is internal we
    # refuse the whole hostname (paranoid — could be DNS rebind setup).
    public_ip = None
    for fam, _t, _p, _c, sa in addrs:
        ip_str = sa[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        for net in _BLOCKED_NETS:
            if ip.version == net.version and ip in net:
                return None, f"ip-resolved:{ip_str} in {net} (host={host})"
        if public_ip is None:
            public_ip = ip_str
    if public_ip is None:
        return None, None
    return public_ip, None


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    # --- HTTPS via CONNECT ---------------------------------------------------
    def do_CONNECT(self):
        try:
            host, port_s = self.path.rsplit(":", 1)
            port = int(port_s)
        except ValueError:
            self.send_error(400, "Bad CONNECT target")
            return

        ip, reason = _resolve_and_check(host, port)
        if reason:
            logger.warning(f"[egress-proxy] BLOCK CONNECT {host}:{port} ({reason})")
            self.send_error(403, "Forbidden by egress policy")
            return
        if ip is None:
            self.send_error(502, "DNS resolution failed")
            return

        try:
            upstream = socket.create_connection((ip, port), timeout=30)
        except OSError as e:
            self.send_error(502, f"Upstream connect failed: {e}")
            return

        self.send_response(200, "Connection Established")
        self.end_headers()

        logger.debug(f"[egress-proxy] CONNECT {host}:{port} → {ip}:{port}")
        self._tunnel(self.connection, upstream)
        try:
            upstream.close()
        except OSError:
            pass

    @staticmethod
    def _tunnel(a: socket.socket, b: socket.socket) -> None:
        a.setblocking(False)
        b.setblocking(False)
        socks = [a, b]
        while True:
            try:
                r, _, x = select.select(socks, [], socks, 300)
            except (OSError, ValueError):
                return
            if x or not r:
                return
            for s in r:
                other = b if s is a else a
                try:
                    data = s.recv(65536)
                except (BlockingIOError, ConnectionResetError, OSError):
                    return
                if not data:
                    return
                try:
                    other.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return

    # --- Plain HTTP ----------------------------------------------------------
    def _proxy_http(self):
        parsed = urllib.parse.urlsplit(self.path)
        host = parsed.hostname or ""
        port = parsed.port or 80
        ip, reason = _resolve_and_check(host, port)
        if reason:
            logger.warning(f"[egress-proxy] BLOCK {self.command} {self.path} ({reason})")
            self.send_error(403, "Forbidden by egress policy")
            return
        if ip is None:
            self.send_error(502, "DNS resolution failed")
            return

        req_path = parsed.path or "/"
        if parsed.query:
            req_path += "?" + parsed.query
        try:
            data_len = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            data_len = 0
        body = self.rfile.read(data_len) if data_len else None
        headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("proxy-connection", "host", "connection")
        }
        headers["Host"] = host if port == 80 else f"{host}:{port}"

        # Connect to the pre-resolved IP, but keep Host header = original hostname.
        url = f"http://{ip}:{port}{req_path}"
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "connection"):
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except Exception as e:
            try:
                self.send_error(502, f"Upstream error: {e}")
            except (BrokenPipeError, ConnectionResetError):
                pass

    do_GET = _proxy_http
    do_POST = _proxy_http
    do_PUT = _proxy_http
    do_DELETE = _proxy_http
    do_HEAD = _proxy_http
    do_OPTIONS = _proxy_http
    do_PATCH = _proxy_http


def start_egress_proxy(port: int = 7890, host: str = "127.0.0.1") -> int:
    """Start the proxy in a daemon thread. Returns the bound port."""
    server = ThreadingHTTPServer((host, port), _ProxyHandler)
    actual = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True, name="egress-proxy")
    t.start()
    logger.info(f"[egress-proxy] listening on http://{host}:{actual} "
                f"(blocking {len(_BLOCKED_NETS)} CIDRs + K8s/metadata hostnames)")
    return actual
