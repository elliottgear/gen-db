"""
Rate limiting: who a request is from (client_ip) and sliding-window counters (SlidingWindow).

Used by the login limit (server.py) and the public request form (appointment_requests.py).
"""
import ipaddress
import os
import threading
import time

# ---------------------------------------------------------------- client IP --
# On Railway the app only ever talks to Railway's edge proxy, which puts the address that
# connected to it in X-Real-IP (and overwrites any value a client sends: checked 2026-11-21).
# For gen.bridgeselec.com that address is a Cloudflare edge server, and Cloudflare puts the
# visitor in CF-Connecting-IP.
#
#   1. Start from the socket peer.
#   2. Behind Railway (TRUST_PROXY, on by default when PORT is set), use X-Real-IP instead.
#   3. Only if that address is one of Cloudflare's published ranges, use CF-Connecting-IP.
#
# X-Forwarded-For is never used: a client can put anything in it. CF-Connecting-IP is ignored unless
# the request really came from Cloudflare, because the app is also reachable without Cloudflare
# (the *.up.railway.app domain), where anyone can send that header.
TRUST_PROXY = os.environ.get('TRUST_PROXY', '1' if 'PORT' in os.environ else '0') == '1'

# https://www.cloudflare.com/ips-v4 and /ips-v6, fetched 2026-11-20. Refresh if Cloudflare changes them.
CLOUDFLARE_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    '173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22', '141.101.64.0/18',
    '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20', '197.234.240.0/22', '198.41.128.0/17',
    '162.158.0.0/15', '104.16.0.0/13', '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22',
    '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32', '2405:8100::/32',
    '2a06:98c0::/29', '2c0f:f248::/32',
))


def _parse_ip(value):
    try:
        ip = ipaddress.ip_address((value or '').strip())
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if ip.version == 6 else ip


def is_cloudflare(ip):
    return ip is not None and any(ip in net for net in CLOUDFLARE_NETWORKS if net.version == ip.version)


def client_ip(headers, socket_peer):
    """(ip, source) for a request: source is 'cloudflare', 'proxy' or 'socket'. ip may be None."""
    ip, source = _parse_ip(socket_peer), 'socket'
    if TRUST_PROXY:
        real = _parse_ip(headers.get('X-Real-IP'))
        if real:
            ip, source = real, 'proxy'
    if is_cloudflare(ip):
        visitor = _parse_ip(headers.get('CF-Connecting-IP'))
        if visitor:
            ip, source = visitor, 'cloudflare'
    return ip, source


def rate_limit_key(ip):
    """IPv4 addresses count alone; IPv6 by /64, since one household or phone gets a whole /64."""
    if ip is None:
        return 'unknown'
    if ip.version == 6:
        return str(ipaddress.ip_network(f'{ip}/64', strict=False))
    return str(ip)


# ----------------------------------------------------------- sliding window --

class SlidingWindow:
    """Counts events per key over the last `window` seconds. Thread-safe; idle keys are dropped.

    In-memory, so limits reset when the server restarts; fine for a single process.
    """
    _all = []

    def __init__(self, window_seconds):
        self.window = window_seconds
        self._hits = {}  # key -> monotonic times, oldest first
        self._lock = threading.Lock()
        SlidingWindow._all.append(self)

    def _recent(self, key, now):
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def count(self, key):
        with self._lock:
            return len(self._recent(key, time.monotonic()))

    def hit(self, key):
        with self._lock:
            now = time.monotonic()
            self._hits[key] = self._recent(key, now) + [now]

    def hit_if_under(self, key, limit):
        """Records a hit and returns True if `key` has fewer than `limit`; otherwise records nothing."""
        with self._lock:
            now = time.monotonic()
            hits = self._recent(key, now)
            if len(hits) >= limit:
                return False
            self._hits[key] = hits + [now]
            return True

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)

    def tracked_keys(self):
        with self._lock:
            return set(self._hits)

    @classmethod
    def reset_all(cls):
        """Forget every count (tests)."""
        for window in cls._all:
            with window._lock:
                window._hits.clear()
