"""Builds the per-project network policy from the global allowlist."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .models import AllowedTarget, Project
from .urlguard import Policy, TargetRejected, normalize_host, parse_target, valid_hostname


@dataclass
class ProjectPolicy:
    policy: Policy
    ports: set[int]
    problems: list[str]


def parse_extra_host(entry: str) -> tuple[str, int | None]:
    e = entry.strip().lower()
    host, port = e, None
    if e.count(":") == 1:
        host, p = e.split(":")
        if not p.isdigit() or not (0 < int(p) < 65536):
            raise TargetRejected(f"invalid port in '{entry}'")
        port = int(p)
    host = normalize_host(host)
    if not valid_hostname(host):
        raise TargetRejected(f"invalid host '{entry}'")
    return host, port


def project_hosts(base_url: str, extra_hosts: list[str]) -> tuple[set[str], set[int]]:
    _, host, port = parse_target(base_url)
    hosts, ports = {host}, {80, 443, port}
    for e in extra_hosts or []:
        h, p = parse_extra_host(e)
        hosts.add(h)
        if p:
            ports.add(p)
    return hosts, ports


def allowlist(db: Session) -> set[str]:
    return {normalize_host(h) for h in db.scalars(select(AllowedTarget.host)).all()}


def build(db: Session, project: Project) -> ProjectPolicy:
    problems: list[str] = []
    try:
        hosts, ports = project_hosts(project.base_url, project.extra_hosts)
    except TargetRejected as e:
        return ProjectPolicy(Policy(frozenset(), frozenset()), set(), [str(e)])
    approved = allowlist(db)
    missing = sorted(hosts - approved)
    if missing:
        problems.append("not on the authorised target allowlist: " + ", ".join(missing))
    ok_hosts = frozenset(hosts & approved)
    private_ok = frozenset(ok_hosts & get_settings().private_targets)
    return ProjectPolicy(Policy(ok_hosts, private_ok), ports, problems)
