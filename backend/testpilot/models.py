from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Tenant roles (see permissions.py). Legacy role names from 1.0 are accepted as aliases.
ROLES = ("tenant_owner", "tenant_admin", "project_manager", "qa_engineer", "developer", "auditor")
LEGACY_ROLE_MAP = {"admin": "tenant_admin", "tester": "qa_engineer", "viewer": "auditor"}
TENANT_STATUSES = ("active", "suspended")
REGISTRATION_STATUSES = ("pending", "approved", "rejected")
RUN_STATUSES = ("queued", "running", "passed", "failed", "error", "cancelled")
# Result of one test case inside a test run (manual or automated execution).
ITEM_STATUSES = ("untested", "passed", "failed", "blocked", "retest", "skipped")
TERMINAL = ("passed", "failed", "error", "cancelled")


class Tenant(Base):
    """An approved customer organisation. Only created by a Unical Super Admin approving a registration
    (or the migration, for the default Unical tenant)."""

    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), default="active")
    status_reason: Mapped[str] = mapped_column(String(500), default="")
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_registrations.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TenantRegistration(Base):
    """Public sign-up request. Stays pending until a Unical Super Admin approves or rejects it."""

    __tablename__ = "tenant_registrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_name: Mapped[str] = mapped_column(String(160))
    requested_slug: Mapped[str] = mapped_column(String(40), index=True)
    contact_name: Mapped[str] = mapped_column(String(120))
    contact_email: Mapped[str] = mapped_column(String(254), index=True)
    contact_phone: Mapped[str] = mapped_column(String(40), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    country_code: Mapped[str] = mapped_column(String(2), default="")  # ISO 3166-1 alpha-2 (release 1.3)
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decision_note: Mapped[str] = mapped_column(String(1000), default="")
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TenantBranding(Base):
    __tablename__ = "tenant_branding"
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), default="")
    logo_data_url: Mapped[str] = mapped_column(Text, default="")  # data:image/(png|jpeg|webp);base64,...
    primary_color: Mapped[str] = mapped_column(String(7), default="#2a63d4")
    welcome_text: Mapped[str] = mapped_column(String(1000), default="")
    support_email: Mapped[str] = mapped_column(String(254), default="")
    support_url: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Invitation(Base):
    """Time-limited, single-use activation link. Only the SHA-256 of the token is stored."""

    __tablename__ = "invitations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(24))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


def _tenant_fk() -> Mapped[int]:
    return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
                      UniqueConstraint("id", "tenant_id", name="uq_users_id_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    username: Mapped[str] = mapped_column(String(80), index=True)
    email: Mapped[str] = mapped_column(String(254), default="")
    # Unical Super Admin: platform operations (registrations, tenant lifecycle) only.
    # It grants NO access to other tenants' projects, runs or evidence.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(24), default="auditor")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Session_(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    tenant_id: Mapped[int] = _tenant_fk()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    auth_method: Mapped[str] = mapped_column(String(16), default="password")  # password | microsoft | invite
    user: Mapped[User] = relationship()


class AllowedTarget(Base):
    """Per-tenant allowlist of hosts the browser worker may contact. Managed by tenant admins."""

    __tablename__ = "allowed_targets"
    __table_args__ = (UniqueConstraint("tenant_id", "host", name="uq_allowed_targets_tenant_host"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    host: Mapped[str] = mapped_column(String(253))
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_projects_id_tenant"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    base_url: Mapped[str] = mapped_column(String(2048))
    extra_hosts: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Automation settings: steps that bring the app into a signed-in state (run before any case
    # flagged use_setup), and free-text notes about pages/selectors that help Claude automate cases.
    setup_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    app_notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectSecret(Base):
    """Encrypted values usable in steps as {{secret:NAME}}. Never returned by the API."""

    __tablename__ = "project_secrets"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_project_secret_name"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_project_secrets_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64))
    value_encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TestCase(Base):
    __tablename__ = "test_cases"
    __test__ = False  # stop pytest collecting this class
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_test_cases_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_test_cases_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | ai | import
    # Test-management fields (filled by hand or by a CSV/Excel import). A case without
    # automated steps is a manual case: it is executed by a tester inside a test run.
    external_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    module: Mapped[str] = mapped_column(String(200), default="")
    sub_module: Mapped[str] = mapped_column(String(200), default="")
    priority: Mapped[str] = mapped_column(String(40), default="")
    scenario_type: Mapped[str] = mapped_column(String(80), default="")
    assigned_to: Mapped[str] = mapped_column(String(120), default="")
    requirement: Mapped[str] = mapped_column(Text, default="")
    preconditions: Mapped[str] = mapped_column(Text, default="")
    test_data: Mapped[str] = mapped_column(Text, default="")
    procedure: Mapped[str] = mapped_column(Text, default="")
    expected_result: Mapped[str] = mapped_column(Text, default="")
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # test level, method, version, custom columns
    import_batch: Mapped[str] = mapped_column(String(40), default="")
    use_setup: Mapped[bool] = mapped_column(Boolean, default=False)  # prepend the project's setup steps
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_status_created", "status", "created_at"),
                      UniqueConstraint("id", "tenant_id", name="uq_runs_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_runs_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    test_case_id: Mapped[int | None] = mapped_column(ForeignKey("test_cases.id", ondelete="SET NULL"), index=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    case_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ai_analysis: Mapped[str] = mapped_column(Text, default="")
    ai_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # MySQL report store sync state (null synced_at = needs pushing)
    report_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_sync_error: Mapped[str] = mapped_column(Text, default="")
    # Release 1.3: where this run's artifacts live, decided when the run starts and never changed later.
    storage_mode: Mapped[str] = mapped_column(String(16), default="unical")      # unical | external
    storage_provider: Mapped[str] = mapped_column(String(16), default="local")  # local | sharepoint | gdrive
    upload_state: Mapped[str] = mapped_column(String(16), default="")          # "" | pending | done | failed
    # True once step errors / AI analysis were removed from PostgreSQL (they live in the external report).
    details_external: Mapped[bool] = mapped_column(Boolean, default=False)
    steps: Mapped[list["RunStep"]] = relationship(order_by="RunStep.idx", cascade="all, delete-orphan")


class TestRun(Base):
    """A planned execution of a set of test cases (e.g. one module, one sprint, one release).
    Not to be confused with Run, which is one automated browser execution of one case."""

    __tablename__ = "test_runs"
    __test__ = False
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_test_runs_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_test_runs_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | closed
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | import
    group_label: Mapped[str] = mapped_column(String(300), default="")
    import_batch: Mapped[str] = mapped_column(String(40), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list["TestRunItem"]] = relationship(order_by="TestRunItem.position", cascade="all, delete-orphan")


class TestRunItem(Base):
    __tablename__ = "test_run_items"
    __test__ = False
    __table_args__ = (UniqueConstraint("test_run_id", "test_case_id", name="uq_test_run_item_case"),
                      ForeignKeyConstraint(["test_run_id", "tenant_id"], ["test_runs.id", "test_runs.tenant_id"],
                                           ondelete="CASCADE", name="fk_test_run_items_run_tenant"),
                      ForeignKeyConstraint(["test_case_id", "tenant_id"], ["test_cases.id", "test_cases.tenant_id"],
                                           ondelete="CASCADE", name="fk_test_run_items_case_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    test_run_id: Mapped[int] = mapped_column(Integer, index=True)
    test_case_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="untested")
    actual_result: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    assigned_to: Mapped[str] = mapped_column(String(120), default="")
    executed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    executed_by_name: Mapped[str] = mapped_column(String(120), default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    automated_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    case: Mapped[TestCase] = relationship(viewonly=True)


# AutomationItem statuses
AUTO_PENDING = ("queued", "repair")           # waiting for Claude (first draft / repair after a failed trial)
AUTO_STATUSES = ("queued", "drafting", "repair", "trial", "drafted", "verified", "failed", "manual_only",
                 "applied", "rejected", "error", "cancelled")


class AutomationJob(Base):
    """Converts manual cases to automated steps: the worker explores the app, Claude drafts steps
    per case, a trial run verifies them (with one repair attempt), then they are applied."""

    __tablename__ = "automation_jobs"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_automation_jobs_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_automation_jobs_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    test_run_id: Mapped[int | None] = mapped_column(ForeignKey("test_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="exploring")  # exploring|running|done|cancelled
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    app_map: Mapped[str] = mapped_column(Text, default="")
    explore_log: Mapped[str] = mapped_column(Text, default="")
    claude_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AutomationItem(Base):
    __tablename__ = "automation_items"
    __table_args__ = (Index("ix_automation_items_status", "status"),
                      ForeignKeyConstraint(["job_id", "tenant_id"], ["automation_jobs.id", "automation_jobs.tenant_id"],
                                           ondelete="CASCADE", name="fk_automation_items_job_tenant"),
                      ForeignKeyConstraint(["test_case_id", "tenant_id"], ["test_cases.id", "test_cases.tenant_id"],
                                           ondelete="CASCADE", name="fk_automation_items_case_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    job_id: Mapped[int] = mapped_column(Integer, index=True)
    test_case_id: Mapped[int] = mapped_column(Integer, index=True)
    test_run_item_id: Mapped[int | None] = mapped_column(ForeignKey("test_run_items.id", ondelete="SET NULL"), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    use_setup: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")       # Claude's notes / why not automatable
    error: Mapped[str] = mapped_column(Text, default="")        # last trial failure or processing error
    trial_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    case: Mapped[TestCase] = relationship(viewonly=True)


class RunStep(Base):
    __tablename__ = "run_steps"
    __table_args__ = (UniqueConstraint("run_id", "idx", name="uq_run_step_idx"),
                      ForeignKeyConstraint(["run_id", "tenant_id"], ["runs.id", "runs.tenant_id"],
                                           ondelete="CASCADE", name="fk_run_steps_run_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    idx: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (ForeignKeyConstraint(["run_id", "tenant_id"], ["runs.id", "runs.tenant_id"],
                                           ondelete="CASCADE", name="fk_evidence_run_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_evidence_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    test_case_id: Mapped[int | None] = mapped_column(ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    step_id: Mapped[int | None] = mapped_column(ForeignKey("run_steps.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))  # screenshot|trace|video|log|report_html|report_json
    rel_path: Mapped[str] = mapped_column(String(1024))
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Release 1.3. storage: local (Unical evidence volume) | spool (temporary, awaiting upload) |
    # sharepoint | gdrive. rel_path is empty once a spooled file has been uploaded and removed.
    storage: Mapped[str] = mapped_column(String(16), default="local")
    external_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    external_path: Mapped[str] = mapped_column(String(1024), default="")
    upload_status: Mapped[str] = mapped_column(String(16), default="")  # "" | pending | uploaded | lost
    upload_error: Mapped[str] = mapped_column(String(1000), default="")
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The tenant the event concerns (null only for platform events with no tenant, e.g. a rejected registration).
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)
    # "tenant": visible to that tenant's admins/auditors; "platform": visible to Unical Super Admins only.
    scope: Mapped[str] = mapped_column(String(12), default="tenant")
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    current_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# =============================================================================== release 1.2
PROJECT_ROLES = ("project_manager", "qa_engineer", "developer", "auditor")
ASSIGNMENT_STATUSES = ("active", "revoked", "expired")


class PlatformSetting(Base):
    """Platform-wide configuration documents (auth, recaptcha, trusted_ips, smtp).
    Secret fields inside `value` are Fernet-encrypted (keys ending in `_enc`) and never returned by the API."""

    __tablename__ = "platform_settings"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TenantAuthSettings(Base):
    __tablename__ = "tenant_auth_settings"
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    password_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    microsoft_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # CIDRs whose sign-in requests to this organization skip the CAPTCHA (in addition to the platform list)
    trusted_ips: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IdentityProvider(Base):
    """A tenant's own Microsoft Entra ID (OpenID Connect) configuration. Never shared across tenants."""

    __tablename__ = "identity_providers"
    __table_args__ = (UniqueConstraint("tenant_id", "provider", name="uq_identity_providers_tenant_provider"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="microsoft")
    entra_tenant_id: Mapped[str] = mapped_column(String(64), default="")
    client_id: Mapped[str] = mapped_column(String(64), default="")
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    secret_hint: Mapped[str] = mapped_column(String(16), default="")      # last 4 characters, for the masked display
    secret_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cloud_host: Mapped[str] = mapped_column(String(100), default="login.microsoftonline.com")
    allowed_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_group_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    provisioning: Mapped[str] = mapped_column(String(20), default="invite_only")  # invite_only | auto_domain
    auto_role: Mapped[str] = mapped_column(String(24), default="auditor")
    status: Mapped[str] = mapped_column(String(16), default="unverified")  # unverified | active | error
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(1000), default="")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ExternalIdentity(Base):
    """Link between a TestAgent user and a stable identity-provider subject (Entra tid + oid)."""

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "issuer", "subject", name="uq_external_identity_subject"),
        UniqueConstraint("user_id", "provider", name="uq_external_identity_user_provider"),
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                             name="fk_external_identities_user_tenant"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    issuer: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str] = mapped_column(String(128))
    email_at_link: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OidcTransaction(Base):
    """Server-side OAuth state: binds the callback to one tenant, provider, purpose and browser flow."""

    __tablename__ = "oidc_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    provider_id: Mapped[int] = mapped_column(ForeignKey("identity_providers.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(10))  # login | link | invite
    nonce: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    invitation_id: Mapped[int | None] = mapped_column(ForeignKey("invitations.id", ondelete="CASCADE"), nullable=True)
    browser_binding: Mapped[str] = mapped_column(String(64))  # sha256 of a cookie set on start
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")


class CaptchaTokenSeen(Base):
    """Replay protection: a reCAPTCHA token is accepted once."""

    __tablename__ = "captcha_tokens_seen"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class UserGroup(Base):
    __tablename__ = "user_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_user_groups_tenant_name"),
                      UniqueConstraint("id", "tenant_id", name="uq_user_groups_id_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(1000), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        ForeignKeyConstraint(["group_id", "tenant_id"], ["user_groups.id", "user_groups.tenant_id"], ondelete="CASCADE",
                             name="fk_group_members_group_tenant"),
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                             name="fk_group_members_user_tenant"),
    )
    group_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    added_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectAssignment(Base):
    """Grants a user or a group a project role, for one project or all projects of the tenant,
    optionally within a [starts_at, ends_at) UTC window. Checked on every request (see access.py)."""

    __tablename__ = "project_assignments"
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                             name="fk_assignments_user_tenant"),
        ForeignKeyConstraint(["group_id", "tenant_id"], ["user_groups.id", "user_groups.tenant_id"], ondelete="CASCADE",
                             name="fk_assignments_group_tenant"),
        ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"], ondelete="CASCADE",
                             name="fk_assignments_project_tenant"),
        Index("ix_assignments_user", "tenant_id", "user_id"),
        Index("ix_assignments_group", "tenant_id", "group_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(10))  # all | project
    project_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(24))
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    status: Mapped[str] = mapped_column(String(10), default="active")
    reason: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


# =============================================================================== release 1.3
STORAGE_POLICIES = ("choice", "unical_only", "external_only")
STORAGE_MODES = ("unical", "external")
EXTERNAL_PROVIDERS = ("sharepoint", "gdrive")


class TenantStorage(Base):
    """Where an organization's test artifacts are stored, and its data-residency controls.

    mode "" means the organization has not chosen yet: runs are refused until an administrator
    chooses Unical-managed storage (with explicit agreement) or configures its own SharePoint/Drive."""

    __tablename__ = "tenant_storage"
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    policy_override: Mapped[str | None] = mapped_column(String(16), nullable=True)  # set by Unical Super Admin
    mode: Mapped[str] = mapped_column(String(16), default="")
    provider: Mapped[str] = mapped_column(String(16), default="")
    unical_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unical_consent_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    unical_consent_source: Mapped[str] = mapped_column(String(16), default="")  # admin | legacy
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)          # destination + folder names
    credentials_enc: Mapped[str] = mapped_column(Text, default="")
    credentials_hint: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default="unconfigured")    # ok | error | unconfigured
    needs_reselection: Mapped[bool] = mapped_column(Boolean, default=False)
    reselection_reason: Mapped[str] = mapped_column(String(500), default="")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(1000), default="")
    allow_delete: Mapped[bool] = mapped_column(Boolean, default=False)  # TestAgent may delete files it created
    retention_days: Mapped[int] = mapped_column(Integer, default=0)      # 0 = keep until deleted by hand
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)      # AI features send data to Anthropic
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CaseVersion(Base):
    """A versioned JSON copy of a test-case definition, written to the organization's own storage."""

    __tablename__ = "case_versions"
    __table_args__ = (UniqueConstraint("test_case_id", "version", name="uq_case_versions_case_version"),
                      ForeignKeyConstraint(["test_case_id", "tenant_id"], ["test_cases.id", "test_cases.tenant_id"],
                                           ondelete="CASCADE", name="fk_case_versions_case_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    test_case_id: Mapped[int] = mapped_column(Integer, index=True)
    version: Mapped[int] = mapped_column(Integer)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage: Mapped[str] = mapped_column(String(16), default="spool")
    spool_path: Mapped[str] = mapped_column(String(1024), default="")
    external_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    external_path: Mapped[str] = mapped_column(String(1024), default="")
    upload_status: Mapped[str] = mapped_column(String(16), default="pending")
    upload_error: Mapped[str] = mapped_column(String(1000), default="")
    upload_attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


PRIVACY_REQUEST_KINDS = ("access", "export", "correction", "erasure", "withdraw_consent", "restrict",
                         "opt_out", "nominate", "grievance")
PRIVACY_REQUEST_STATUSES = ("open", "in_progress", "completed", "rejected")


class PrivacyRequest(Base):
    """A data-subject / data-principal request (DPDP Act 2023, UK GDPR, US state privacy laws)."""

    __tablename__ = "privacy_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    requester_name: Mapped[str] = mapped_column(String(160), default="")
    requester_email: Mapped[str] = mapped_column(String(254), default="")
    kind: Mapped[str] = mapped_column(String(24))
    details: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    response: Mapped[str] = mapped_column(Text, default="")
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class LegalAcceptance(Base):
    """Record of who accepted which version of the Terms or Privacy Policy, when and from where."""

    __tablename__ = "legal_acceptances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    registration_id: Mapped[int | None] = mapped_column(ForeignKey("tenant_registrations.id", ondelete="SET NULL"), nullable=True)
    document: Mapped[str] = mapped_column(String(16))  # terms | privacy
    version: Mapped[str] = mapped_column(String(32))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")


# =============================================================================== release 1.6
# API testing (suites, generated cases, runs) and API uptime monitoring.
API_AUTH_TYPES = ("none", "api_key", "bearer", "basic", "oauth2")
API_SPEC_SOURCES = ("url", "upload", "manual")
API_CASE_SOURCES = ("spec", "auto", "ai", "manual")
MONITOR_STATUSES = ("unknown", "up", "down", "paused")
ALERT_CHANNELS = ("email", "webhook", "inapp")


class ApiSuite(Base):
    """An API under test inside a project: where it lives, how to authenticate, and where its
    test cases come from (an OpenAPI/Swagger document by URL or upload, or a list of endpoints)."""

    __tablename__ = "api_suites"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_api_suites_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_suites_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(2048))
    spec_source: Mapped[str] = mapped_column(String(10), default="manual")  # url | upload | manual
    spec_url: Mapped[str] = mapped_column(String(2048), default="")
    spec_text: Mapped[str] = mapped_column(Text, default="")
    spec_title: Mapped[str] = mapped_column(String(300), default="")
    endpoints_text: Mapped[str] = mapped_column(Text, default="")
    auth_type: Mapped[str] = mapped_column(String(16), default="none")
    auth_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # non-secret settings only
    auth_secret_enc: Mapped[str] = mapped_column(Text, default="")  # Fernet-encrypted JSON of secret values
    auth_hint: Mapped[str] = mapped_column(String(120), default="")
    requires_auth: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_writes: Mapped[bool] = mapped_column(Boolean, default=False)
    verify_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=15000)
    max_response_ms: Mapped[int] = mapped_column(Integer, default=2000)
    default_headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    import_state: Mapped[str] = mapped_column(String(12), default="")  # "" | pending | running | done | error
    import_error: Mapped[str] = mapped_column(Text, default="")
    run_after_import: Mapped[bool] = mapped_column(Boolean, default=False)
    import_requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ApiCase(Base):
    __tablename__ = "api_cases"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_api_cases_id_tenant"),
                      ForeignKeyConstraint(["suite_id", "tenant_id"], ["api_suites.id", "api_suites.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_cases_suite_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    suite_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(16), default="positive")  # positive | auth | negative | performance
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(2048))
    definition: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(12), default="spec")  # spec | auto | ai | manual
    is_write: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ApiRun(Base):
    __tablename__ = "api_runs"
    __table_args__ = (Index("ix_api_runs_status_created", "status", "created_at"),
                      UniqueConstraint("id", "tenant_id", name="uq_api_runs_id_tenant"),
                      ForeignKeyConstraint(["suite_id", "tenant_id"], ["api_suites.id", "api_suites.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_runs_suite_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_runs_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    suite_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    suite_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # name, base URL, auth type, spec title
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    report_sha256: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    results: Mapped[list["ApiRunResult"]] = relationship(order_by="ApiRunResult.idx", cascade="all, delete-orphan")


class ApiRunResult(Base):
    __tablename__ = "api_run_results"
    __table_args__ = (UniqueConstraint("run_id", "idx", name="uq_api_run_results_idx"),
                      ForeignKeyConstraint(["run_id", "tenant_id"], ["api_runs.id", "api_runs.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_run_results_run_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    idx: Mapped[int] = mapped_column(Integer)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("api_cases.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(300), default="")
    category: Mapped[str] = mapped_column(String(16), default="")
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(2048), default="")
    url: Mapped[str] = mapped_column(String(4096), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    assertions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    request: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiMonitor(Base):
    """Opt-in uptime monitor for one API endpoint. Checked by the worker every interval_seconds."""

    __tablename__ = "api_monitors"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_api_monitors_id_tenant"),
                      Index("ix_api_monitors_due", "enabled", "next_check_at"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_api_monitors_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    suite_id: Mapped[int | None] = mapped_column(ForeignKey("api_suites.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(2048))
    method: Mapped[str] = mapped_column(String(8), default="GET")
    headers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    use_suite_auth: Mapped[bool] = mapped_column(Boolean, default=False)
    expected_status: Mapped[list[Any]] = mapped_column(JSON, default=lambda: ["2xx"])
    body_contains: Mapped[str] = mapped_column(String(500), default="")
    max_response_ms: Mapped[int] = mapped_column(Integer, default=0)  # 0 = no latency limit
    timeout_ms: Mapped[int] = mapped_column(Integer, default=10000)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    failure_threshold: Mapped[int] = mapped_column(Integer, default=2)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_channels: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["inapp"])
    alert_emails: Mapped[list[str]] = mapped_column(JSON, default=list)
    webhook_url_enc: Mapped[str] = mapped_column(Text, default="")
    webhook_hint: Mapped[str] = mapped_column(String(120), default="")
    webhook_kind: Mapped[str] = mapped_column(String(10), default="slack")  # slack | teams | generic
    status: Mapped[str] = mapped_column(String(10), default="unknown")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    failing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MonitorCheck(Base):
    __tablename__ = "monitor_checks"
    __table_args__ = (Index("ix_monitor_checks_monitor_time", "monitor_id", "checked_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    monitor_id: Mapped[int] = mapped_column(ForeignKey("api_monitors.id", ondelete="CASCADE"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(1000), default="")


class MonitorIncident(Base):
    __tablename__ = "monitor_incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    monitor_id: Mapped[int] = mapped_column(ForeignKey("api_monitors.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cause: Mapped[str] = mapped_column(String(1000), default="")
    failed_checks: Mapped[int] = mapped_column(Integer, default=0)
    alerts: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # delivery log (channel, ok, at, error)


# =============================================================================== release 1.7
# Connectors to test-management / bug-tracking tools (Azure DevOps, Jira, GitHub, GitLab, webhook).
INTEGRATION_KINDS = ("azure_devops", "jira", "github", "gitlab", "webhook")
INTEGRATION_TRIGGERS = ("auto", "failures", "manual")
INTEGRATION_RUN_TYPES = ("api", "browser", "manual")


class Integration(Base):
    """A project's connection to an external tool. Secrets are Fernet-encrypted and never returned."""

    __tablename__ = "integrations"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_integrations_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_integrations_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    trigger: Mapped[str] = mapped_column(String(12), default="auto")  # auto | failures | manual
    run_types: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["api", "browser", "manual"])
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    secret_hint: Mapped[str] = mapped_column(String(120), default="")
    active_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="unverified")  # unverified | ok | error
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class IntegrationSync(Base):
    """Outbox: one push of one finished run (API / browser / manual test run) to one integration."""

    __tablename__ = "integration_syncs"
    __table_args__ = (UniqueConstraint("integration_id", "source_kind", "source_id", name="uq_integration_sync_source"),
                      Index("ix_integration_syncs_due", "status", "next_attempt_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    source_kind: Mapped[str] = mapped_column(String(12))  # api_run | run | test_run
    source_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | running | done | error
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegrationIssue(Base):
    """The bug / issue opened in the external tool for one test case, so repeat failures update it."""

    __tablename__ = "integration_issues"
    __table_args__ = (UniqueConstraint("integration_id", "case_key", name="uq_integration_issue_case"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    integration_id: Mapped[int] = mapped_column(ForeignKey("integrations.id", ondelete="CASCADE"), index=True)
    case_key: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(300), default="")
    external_id: Mapped[str] = mapped_column(String(100), default="")
    external_url: Mapped[str] = mapped_column(String(1000), default="")
    state: Mapped[str] = mapped_column(String(12), default="failing")  # failing | passing
    last_source: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# =============================================================================== release 1.8
# Mobile app testing: device providers (BrowserStack, LambdaTest, Sauce Labs, Appium, Unical lab, built-in
# phone emulation), app builds, mobile test cases, cross-device runs, live device sessions (recorder / AI agent).
MOBILE_PROVIDER_KINDS = ("browserstack", "lambdatest", "saucelabs", "appium", "unical_lab", "emulator")


class MobileProvider(Base):
    __tablename__ = "mobile_providers"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_mobile_providers_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_providers_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # username, region, hub URL ... (non-secret)
    secret_enc: Mapped[str] = mapped_column(Text, default="")
    secret_hint: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(16), default="unverified")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MobileApp(Base):
    """An uploaded build (APK / AAB / IPA / zipped .app) or a download URL. Uploaded to each device cloud on
    first use; the cloud's app reference is cached in provider_refs."""

    __tablename__ = "mobile_apps"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_mobile_apps_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_apps_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(160))
    platform: Mapped[str] = mapped_column(String(10))  # android | ios
    version: Mapped[str] = mapped_column(String(60), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    rel_path: Mapped[str] = mapped_column(String(1024), default="")  # under the evidence volume
    source_url: Mapped[str] = mapped_column(String(2048), default="")
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    app_id: Mapped[str] = mapped_column(String(255), default="")  # package name / bundle id (optional)
    app_activity: Mapped[str] = mapped_column(String(255), default="")
    provider_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {provider_id: {"ref": "bs://..", "at": iso}}
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MobileCase(Base):
    __tablename__ = "mobile_cases"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_mobile_cases_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_cases_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(8), default="native")  # native | web
    platform: Mapped[str] = mapped_column(String(10), default="android")  # android | ios | any (web)
    app_id: Mapped[int | None] = mapped_column(ForeignKey("mobile_apps.id", ondelete="SET NULL"), nullable=True)
    start_url: Mapped[str] = mapped_column(String(2048), default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(12), default="manual")  # manual | ai | recorded
    test_case_id: Mapped[int | None] = mapped_column(ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MobileRunGroup(Base):
    """One request to run N cases on M devices (the cross-device matrix)."""

    __tablename__ = "mobile_run_groups"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_mobile_run_groups_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_run_groups_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("mobile_providers.id", ondelete="SET NULL"), nullable=True)
    devices: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MobileRun(Base):
    """One case on one device."""

    __tablename__ = "mobile_runs"
    __table_args__ = (Index("ix_mobile_runs_status_created", "status", "created_at"),
                      UniqueConstraint("id", "tenant_id", name="uq_mobile_runs_id_tenant"),
                      ForeignKeyConstraint(["group_id", "tenant_id"], ["mobile_run_groups.id", "mobile_run_groups.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_runs_group_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    group_id: Mapped[int] = mapped_column(Integer, index=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("mobile_cases.id", ondelete="SET NULL"), nullable=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("mobile_providers.id", ondelete="SET NULL"), nullable=True)
    device: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    case_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    step_results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    session_id: Mapped[str] = mapped_column(String(200), default="")
    session_url: Mapped[str] = mapped_column(String(1000), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MobileSession(Base):
    """A live device session driven from the UI (recorder) or by Claude (AI agent)."""

    __tablename__ = "mobile_sessions"
    __table_args__ = (UniqueConstraint("id", "tenant_id", name="uq_mobile_sessions_id_tenant"),
                      ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"],
                                           ondelete="CASCADE", name="fk_mobile_sessions_project_tenant"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = _tenant_fk()
    project_id: Mapped[int] = mapped_column(Integer, index=True)
    purpose: Mapped[str] = mapped_column(String(8), default="record")  # record | ai
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("mobile_providers.id", ondelete="SET NULL"), nullable=True)
    device: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # kind, platform, app_id, start_url
    goal: Mapped[str] = mapped_column(Text, default="")
    test_case_id: Mapped[int | None] = mapped_column(ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="starting")  # device: starting | ready | closed | error
    phase: Mapped[str] = mapped_column(String(12), default="")  # AI agent: running | done | failed | stopped
    session_id: Mapped[str] = mapped_column(String(200), default="")
    session_url: Mapped[str] = mapped_column(String(1000), default="")
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    # The device is held by a worker thread; the API sends one command at a time (cmd_seq) and the
    # worker answers with the same sequence number in res (result + fresh screen observation).
    worker_id: Mapped[str] = mapped_column(String(120), default="")
    cmd_seq: Mapped[int] = mapped_column(Integer, default=0)
    cmd: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    res: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
