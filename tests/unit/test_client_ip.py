"""Which address a request is counted against for rate limits (see client_ip() in server.py)."""
import ipaddress

import pytest

import server

CF_EDGE_V4 = '172.70.1.2'        # 172.64.0.0/13
CF_EDGE_V6 = '2a06:98c0:3600::103'
RAILWAY_PROXY = '10.250.0.4'     # the socket peer behind Railway: its internal proxy


def ip(headers, peer=RAILWAY_PROXY):
    found, source = server.client_ip(headers, peer)
    return (str(found) if found else None), source


def test_without_proxy_trust_only_the_socket_counts(monkeypatch):
    monkeypatch.setattr(server, 'TRUST_PROXY', False)
    assert ip({'X-Real-IP': '198.51.100.7', 'X-Forwarded-For': '203.0.113.1'}, '127.0.0.1') == ('127.0.0.1', 'socket')


def test_behind_railway_x_real_ip_is_the_client():
    assert ip({'X-Real-IP': '198.51.100.7'}) == ('198.51.100.7', 'proxy')


def test_x_forwarded_for_is_never_used():
    assert ip({'X-Forwarded-For': '203.0.113.1'}) == (RAILWAY_PROXY, 'socket')
    assert ip({'X-Real-IP': '198.51.100.7', 'X-Forwarded-For': '203.0.113.1'}) == ('198.51.100.7', 'proxy')


def test_cf_connecting_ip_is_used_when_the_request_came_from_cloudflare():
    assert ip({'X-Real-IP': CF_EDGE_V4, 'CF-Connecting-IP': '198.51.100.7'}) == ('198.51.100.7', 'cloudflare')
    assert ip({'X-Real-IP': CF_EDGE_V6, 'CF-Connecting-IP': '2001:db8::1'}) == ('2001:db8::1', 'cloudflare')


@pytest.mark.parametrize('real_ip', ['198.51.100.7', '8.8.8.8', '2001:db8::5'])
def test_cf_connecting_ip_from_anyone_else_is_ignored(real_ip):
    assert ip({'X-Real-IP': real_ip, 'CF-Connecting-IP': '203.0.113.9'}) == (real_ip, 'proxy')


def test_garbage_headers_fall_back_safely():
    assert ip({'X-Real-IP': 'not an ip'}) == (RAILWAY_PROXY, 'socket')
    assert ip({'X-Real-IP': CF_EDGE_V4, 'CF-Connecting-IP': 'nope'}) == (CF_EDGE_V4, 'proxy')


def test_ipv4_mapped_ipv6_is_treated_as_ipv4():
    assert ip({'X-Real-IP': '::ffff:198.51.100.7'}) == ('198.51.100.7', 'proxy')


def test_cloudflare_ranges():
    assert server.is_cloudflare(ipaddress.ip_address('104.16.0.1'))
    assert server.is_cloudflare(ipaddress.ip_address('2606:4700::1'))
    assert not server.is_cloudflare(ipaddress.ip_address('198.51.100.7'))
    assert not server.is_cloudflare(None)


def test_rate_limit_keys_group_ipv6_by_64():
    a = server.rate_limit_key(ipaddress.ip_address('2001:db8:1:2::1'))
    b = server.rate_limit_key(ipaddress.ip_address('2001:db8:1:2:ffff::9'))
    c = server.rate_limit_key(ipaddress.ip_address('2001:db8:1:3::1'))
    assert a == b == '2001:db8:1:2::/64' and c != a
    assert server.rate_limit_key(ipaddress.ip_address('198.51.100.7')) == '198.51.100.7'
    assert server.rate_limit_key(None) == 'unknown'

