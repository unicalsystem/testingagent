"""Server-side target validation: allowlist, scheme, DNS/IP restrictions (SSRF guard).

Every URL the browser worker opens -- the initial navigation, every redirect hop and every
sub-resource request -- is checked against the rules below. The worker additionally pins
DNS for allowlisted hosts inside Chromium so a host cannot be re-resolved to a different
(internal) address between the check and the request.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
# Pseudo-schemes that never touch the network and are safe to let through.
LOCAL_SCHEMES = {"data", "blob", "about"}

# Always blocked, even for operator-approved private hosts.
METADATA_NETS = [
    ipaddress.ip_network("169.254.0.0/16"),  # link-local incl. 169.254.169.254 (AWS/GCP/Azure/Hostinger)
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba metadata
    ipaddress.ip_network("fd00:ec2::254/128"),  # AWS IPv6 metadata
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("255.255.255.255/32"),
]

HOST_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")


class TargetRejected(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    allowed_hosts: frozenset[str]  # lower-case hostnames (exact match)
    private_ok: frozenset[str]  # subset that may resolve to private addresses


def normalize_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    return h


def valid_hostname(host: str) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return bool(HOST_RE.match(h))


def _is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not ip.is_reserved


def check_ip(ip_str: str, *, private_ok: bool) -> None:
    ip = ipaddress.ip_address(ip_str)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    for net in METADATA_NETS:
        if ip.version == net.version and ip in net:
            raise TargetRejected(f"address {ip} is in a blocked range")
    if not private_ok and not _is_public(ip):
        raise TargetRejected(f"address {ip} is private/internal and the host is not approved for private access")


def resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise TargetRejected(f"cannot resolve {host}: {e}") from e
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise TargetRejected(f"no addresses for {host}")
    return ips


def parse_target(url: str) -> tuple[str, str, int]:
    """Return (scheme, host, port) or raise TargetRejected."""
    if not isinstance(url, str) or len(url) > 2048:
        raise TargetRejected("URL missing or too long")
    if any(c in url for c in "\r\n\t\x00"):
        raise TargetRejected("URL contains control characters")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise TargetRejected(f"scheme '{scheme or '(none)'}' is not allowed")
    if parts.username or parts.password:
        raise TargetRejected("credentials in URLs are not allowed; use project secrets")
    host = normalize_host(parts.hostname or "")
    if not valid_hostname(host):
        raise TargetRejected("invalid host")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as e:
        raise TargetRejected("invalid port") from e
    return scheme, host, port


def check_url(url: str, policy: Policy, *, resolve_dns: bool = True) -> list[str]:
    """Validate a URL against the policy. Returns the vetted IP addresses."""
    _, host, port = parse_target(url)
    if host not in policy.allowed_hosts:
        raise TargetRejected(f"host '{host}' is not on the authorised target allowlist")
    private_ok = host in policy.private_ok
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = resolve(host, port) if resolve_dns else []
    for ip in ips:
        check_ip(ip, private_ok=private_ok)
    return ips


def check_request_url(url: str, policy: Policy) -> None:
    """Check used for every browser request (sub-resources, redirects)."""
    scheme = urlsplit(url).scheme.lower()
    if scheme in LOCAL_SCHEMES:
        return
    check_url(url, policy)
