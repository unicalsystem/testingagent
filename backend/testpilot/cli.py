"""Operator commands.

  python -m testpilot.cli check-config
  python -m testpilot.cli create-admin --username admin [--tenant SLUG] [--platform-admin]
                                                                 (password read from stdin/prompt)
  python -m testpilot.cli allow-target --host sample-app --note "bundled sample" [--tenant SLUG]
  python -m testpilot.cli seed-sample [--tenant SLUG]            (sample project + test cases)
  python -m testpilot.cli list-tenants
  python -m testpilot.cli platform-admins [--grant USER | --revoke USER]   (Unical Super Admins)
  python -m testpilot.cli auth-recovery [--disable-captcha] [--disable-microsoft]
        emergency recovery: re-enables password sign-in for the Unical organization (and optionally turns
        reCAPTCHA / Unical Microsoft sign-in off). Use with create-admin --reset if a password is lost.

  --tenant defaults to DEFAULT_TENANT_SLUG (the Unical organization).
  python -m testpilot.cli generate-keys
  python -m testpilot.cli check-report-db                        (test MySQL report store from config.local)
  python -m testpilot.cli sync-reports [--all]                   (push pending / all reports to MySQL)
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from cryptography.fernet import Fernet
from sqlalchemy import select


def _cmd_check(_):
    from .config import get_settings

    s = get_settings()
    probs = s.problems()
    print(f"environment: {s.app_env}\npublic url: {s.public_url}\nallowed origins: {', '.join(s.origins)}")
    print(f"claude: {'configured (' + s.claude_model + ')' if s.ai_configured else 'NOT configured — AI features disabled'}")
    from .config import CONFIG_LOCAL
    import os as _os
    print(f"config.local: {CONFIG_LOCAL} ({'found' if _os.path.exists(CONFIG_LOCAL) else 'not present'})")
    print(f"mysql report store: {'enabled -> ' + s.mysql_reports_host + '/' + s.mysql_reports_database if s.mysql_reports_enabled else 'disabled'}")
    print(f"evidence dir: {s.evidence_dir}\nprivate targets: {', '.join(sorted(s.private_targets)) or '(none)'}")
    for p in probs:
        print(f"PROBLEM: {p}")
    if probs:
        sys.exit(1)
    print("configuration OK")


def _tenant(db, slug: str | None):
    from .config import get_settings
    from .models import Tenant

    slug = (slug or get_settings().default_tenant_slug).lower()
    t = db.scalars(select(Tenant).where(Tenant.slug == slug)).first()
    if not t:
        sys.exit(f"organization '{slug}' not found (run migrations first: alembic upgrade head)")
    return t


def _cmd_tenants(_):
    from .db import session_scope
    from .models import Tenant

    with session_scope() as db:
        for t in db.scalars(select(Tenant).order_by(Tenant.id)).all():
            print(f"{t.id:4}  {t.slug:30}  {t.status:10}  {t.name}")


def _cmd_platform_admins(a):
    from .config import get_settings
    from .db import session_scope
    from .models import Session_, User
    from sqlalchemy import delete

    with session_scope() as db:
        t = _tenant(db, get_settings().default_tenant_slug)
        name = a.grant or a.revoke
        if name:
            u = db.scalars(select(User).where(User.tenant_id == t.id, User.username == name.lower())).first()
            if not u:
                sys.exit(f"user {name} not found in {t.slug}")
            u.is_platform_admin = bool(a.grant)
            db.execute(delete(Session_).where(Session_.user_id == u.id))
            print(f"{'granted' if a.grant else 'revoked'} Unical Super Admin for {u.username}")
        for u in db.scalars(select(User).where(User.tenant_id == t.id, User.is_platform_admin.is_(True))
                            .order_by(User.username)).all():
            print(f"super admin: {u.username}{'' if u.is_active else ' (disabled)'}")


def _cmd_recovery(a):
    from . import settings_store
    from .db import session_scope
    from .models import AuditLog, Tenant
    from .config import get_settings

    with session_scope() as db:
        t = db.scalars(select(Tenant).where(Tenant.slug == get_settings().default_tenant_slug)).one()
        changes = {"platform_password_enabled": True}
        if a.disable_microsoft:
            changes["platform_microsoft_enabled"] = False
        settings_store.put(db, "auth", changes, None)
        if a.disable_captcha:
            settings_store.put(db, "recaptcha", {"enabled": False}, None)
        db.add(AuditLog(tenant_id=t.id, scope="platform", action="platform_auth_recovery", target="cli",
                        detail={"disable_captcha": a.disable_captcha, "disable_microsoft": a.disable_microsoft}))
    print("password sign-in re-enabled for the Unical organization"
          + ("; reCAPTCHA disabled" if a.disable_captcha else "")
          + ("; Unical Microsoft sign-in disabled" if a.disable_microsoft else ""))


def _cmd_admin(a):
    from .db import session_scope
    from .models import User
    from .security import hash_password, password_problems

    pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass("New admin password: ")
    probs = password_problems(pw)
    if probs:
        sys.exit("Password needs " + " and ".join(probs))
    with session_scope() as db:
        t = _tenant(db, a.tenant)
        from .config import get_settings
        platform = bool(a.platform_admin)
        if platform and t.slug != get_settings().default_tenant_slug:
            sys.exit("--platform-admin is only allowed in the default (Unical) organization")
        u = db.scalars(select(User).where(User.tenant_id == t.id, User.username == a.username.lower())).first()
        if u:
            if not a.reset:
                sys.exit(f"User {a.username} exists in {t.slug}. Use --reset to reset its password.")
            u.password_hash, u.is_active, u.failed_logins, u.locked_until = hash_password(pw), True, 0, None
            if u.role not in ("tenant_owner", "tenant_admin"):
                u.role = "tenant_admin"
            u.is_platform_admin = u.is_platform_admin or platform
            print(f"reset password for {u.username} in {t.slug}")
        else:
            db.add(User(tenant_id=t.id, username=a.username.lower(), display_name=a.display_name or a.username,
                        role="tenant_admin", is_platform_admin=platform, password_hash=hash_password(pw)))
            print(f"created tenant admin {a.username} in {t.slug}{' (Unical Super Admin)' if platform else ''}")


def _cmd_allow(a):
    from .db import session_scope
    from .models import AllowedTarget
    from .urlguard import normalize_host, valid_hostname

    host = normalize_host(a.host)
    if not valid_hostname(host):
        sys.exit("invalid host")
    with session_scope() as db:
        t = _tenant(db, a.tenant)
        if db.scalars(select(AllowedTarget).where(AllowedTarget.tenant_id == t.id, AllowedTarget.host == host)).first():
            print(f"{host} already allowed for {t.slug}")
            return
        db.add(AllowedTarget(tenant_id=t.id, host=host, note=a.note))
    print(f"allowed {host} for {t.slug}")


SAMPLE_CASES = [
    {
        "name": "Sign in with valid credentials",
        "description": "A registered user can sign in and lands on the task board.",
        "tags": ["smoke", "auth"],
        "steps": [
            {"action": "goto", "url": "/", "description": "Open the sample app"},
            {"action": "expect_title", "contains": "Acme Tasks"},
            {"action": "fill", "selector": "#username", "value": "demo", "description": "Enter username"},
            {"action": "fill", "selector": "#password", "value": "{{secret:SAMPLE_PASSWORD}}", "description": "Enter password from project secret"},
            {"action": "click", "selector": "role=button[name=\"Sign in\"]"},
            {"action": "expect_url", "contains": "board.html"},
            {"action": "expect_text", "selector": "[data-testid=welcome]", "text": "Welcome, demo"},
            {"action": "screenshot", "name": "board"},
        ],
    },
    {
        "name": "Reject invalid password",
        "description": "Wrong credentials show an error and stay on the sign-in page.",
        "tags": ["auth", "negative"],
        "steps": [
            {"action": "goto", "url": "/"},
            {"action": "fill", "selector": "#username", "value": "demo"},
            {"action": "fill", "selector": "#password", "value": "wrong-password"},
            {"action": "click", "selector": "role=button[name=\"Sign in\"]"},
            {"action": "expect_visible", "selector": "[data-testid=login-error]"},
            {"action": "expect_text", "selector": "[data-testid=login-error]", "text": "Invalid username or password"},
            {"action": "expect_visible", "selector": "#login-form"},
        ],
    },
    {
        "name": "Add and complete a task",
        "description": "A signed-in user can add a task, complete it and see the counter update.",
        "tags": ["tasks"],
        "steps": [
            {"action": "goto", "url": "/"},
            {"action": "fill", "selector": "#username", "value": "demo"},
            {"action": "fill", "selector": "#password", "value": "{{secret:SAMPLE_PASSWORD}}"},
            {"action": "press", "selector": "#password", "key": "Enter"},
            {"action": "wait_for", "selector": "[data-testid=new-task]"},
            {"action": "fill", "selector": "[data-testid=new-task]", "value": "Prepare IV&V report"},
            {"action": "click", "selector": "[data-testid=add-task]"},
            {"action": "expect_text", "selector": "[data-testid=task-list]", "text": "Prepare IV&V report"},
            {"action": "check", "selector": "[data-testid=task-list] li:last-child input[type=checkbox]"},
            {"action": "expect_text", "selector": "[data-testid=remaining]", "text": "2 remaining"},
            {"action": "select", "selector": "#filter", "value": "done"},
            {"action": "expect_text", "selector": "[data-testid=task-list]", "text": "Prepare IV&V report"},
        ],
    },
]


def _cmd_seed(a):
    from .db import session_scope
    from .models import AllowedTarget, Project, ProjectSecret, TestCase
    from .security import encrypt_secret

    with session_scope() as db:
        t = _tenant(db, a.tenant)
        host = a.url.split("://", 1)[1].split("/")[0].split(":")[0]
        if not db.scalars(select(AllowedTarget).where(AllowedTarget.tenant_id == t.id, AllowedTarget.host == host)).first():
            db.add(AllowedTarget(tenant_id=t.id, host=host, note="Bundled sample application"))
        p = db.scalars(select(Project).where(Project.tenant_id == t.id, Project.name == "Sample App (Acme Tasks)")).first()
        if p:
            print(f"sample project already exists (id {p.id})")
            return
        p = Project(tenant_id=t.id, name="Sample App (Acme Tasks)", description="Bundled demo application used to verify TestAgent.",
                    base_url=a.url, extra_hosts=[])
        db.add(p)
        db.flush()
        db.add(ProjectSecret(tenant_id=t.id, project_id=p.id, name="SAMPLE_PASSWORD", value_encrypted=encrypt_secret("demo-pass-123")))
        for c in SAMPLE_CASES:
            db.add(TestCase(tenant_id=t.id, project_id=p.id, **c))
        print(f"created sample project id {p.id} with {len(SAMPLE_CASES)} test cases")


def _cmd_keys(_):
    print(f"SESSION_SECRET={secrets.token_urlsafe(48)}")
    print(f"CREDENTIAL_ENCRYPTION_KEY={Fernet.generate_key().decode()}")
    print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(32)}")


def _cmd_check_report_db(_):
    from . import report_store

    r = report_store.check()
    if not r.get("enabled"):
        sys.exit("MySQL report store is disabled (set MYSQL_REPORTS_ENABLED=true in config.local)")
    if not r.get("ok"):
        sys.exit(f"MySQL report store UNAVAILABLE at {r['host']}/{r['database']}: {r['error']}")
    print(f"MySQL report store OK: {r['server']} at {r['host']}/{r['database']} (tables ready)")


def _cmd_sync_reports(a):
    from sqlalchemy import update

    from . import report_store
    from .db import session_scope
    from .models import Run

    if not report_store.enabled():
        sys.exit("MySQL report store is disabled")
    if a.all:
        with session_scope() as db:
            db.execute(update(Run).values(report_synced_at=None))
    total_ok = 0
    while True:
        ok, bad = report_store.sync_pending(limit=100)
        total_ok += ok
        if bad:
            sys.exit(f"pushed {total_ok}; stopped on an error (see worker logs / report_sync_error)")
        if ok == 0:
            break
    print(f"pushed {total_ok} report(s) to MySQL")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="testpilot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config").set_defaults(fn=_cmd_check)
    p = sub.add_parser("create-admin")
    p.add_argument("--username", required=True)
    p.add_argument("--display-name", default="")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--tenant", default=None)
    p.add_argument("--platform-admin", action="store_true", help="also make this user a Unical Super Admin")
    p.set_defaults(fn=_cmd_admin)
    sub.add_parser("list-tenants").set_defaults(fn=_cmd_tenants)
    p = sub.add_parser("auth-recovery")
    p.add_argument("--disable-captcha", action="store_true")
    p.add_argument("--disable-microsoft", action="store_true")
    p.set_defaults(fn=_cmd_recovery)
    p = sub.add_parser("platform-admins")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--grant")
    g.add_argument("--revoke")
    p.set_defaults(fn=_cmd_platform_admins)
    p = sub.add_parser("allow-target")
    p.add_argument("--host", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--tenant", default=None)
    p.set_defaults(fn=_cmd_allow)
    p = sub.add_parser("seed-sample")
    p.add_argument("--url", default="http://sample-app:8080")
    p.add_argument("--tenant", default=None)
    p.set_defaults(fn=_cmd_seed)
    sub.add_parser("generate-keys").set_defaults(fn=_cmd_keys)
    sub.add_parser("check-report-db").set_defaults(fn=_cmd_check_report_db)
    p = sub.add_parser("sync-reports")
    p.add_argument("--all", action="store_true", help="re-push every finished run")
    p.set_defaults(fn=_cmd_sync_reports)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
