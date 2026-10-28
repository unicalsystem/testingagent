"""storage & data residency, external artifacts, privacy requests, legal acceptances

- tenant_storage: per-organization storage choice (Unical-managed or its own SharePoint / Google Drive),
  explicit agreement record, encrypted provider credentials, health, retention, AI switch
- evidence: storage location columns (local | spool | sharepoint | gdrive) + external reference
- runs: storage snapshot and upload state
- case_versions: versioned test-case definitions written to the organization's own storage
- privacy_requests, legal_acceptances; tenant_registrations.country_code
- Compatibility: every existing organization keeps Unical-managed storage (recorded as a legacy
  agreement that its administrators are asked to confirm); existing evidence stays where it is.

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def _ts(name, nullable=True, default=False):
    kw = {"server_default": sa.func.now()} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def _str(name, n, default=""):
    return sa.Column(name, sa.String(n), nullable=False, server_default=default)


def upgrade() -> None:
    op.create_table(
        "tenant_storage",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("policy_override", sa.String(16), nullable=True),
        _str("mode", 16), _str("provider", 16),
        _ts("unical_consent_at"),
        sa.Column("unical_consent_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _str("unical_consent_source", 16),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("credentials_enc", sa.Text(), nullable=False, server_default=""),
        _str("credentials_hint", 120),
        _str("status", 16, "unconfigured"),
        sa.Column("needs_reselection", sa.Boolean(), nullable=False, server_default=sa.false()),
        _str("reselection_reason", 500),
        _ts("last_check_at"), _ts("last_ok_at"),
        _str("last_error", 1000),
        sa.Column("allow_delete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("retention_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ai_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("updated_at", False, True),
    )
    # Existing organizations keep using Unical-managed storage exactly as before.
    op.execute("INSERT INTO tenant_storage (tenant_id, mode, provider, unical_consent_at, unical_consent_source, status) "
               "SELECT id, 'unical', 'local', now(), 'legacy', 'ok' FROM tenants")

    with op.batch_alter_table("evidence") as b:
        b.add_column(_str("storage", 16, "local"))
        b.add_column(sa.Column("external_ref", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(_str("external_path", 1024))
        b.add_column(_str("upload_status", 16))
        b.add_column(_str("upload_error", 1000))
        b.add_column(sa.Column("upload_attempts", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(_ts("deleted_at"))
    op.create_index("ix_evidence_upload_pending", "evidence", ["upload_status"],
                    postgresql_where=sa.text("upload_status = 'pending'"))

    with op.batch_alter_table("runs") as b:
        b.add_column(_str("storage_mode", 16, "unical"))
        b.add_column(_str("storage_provider", 16, "local"))
        b.add_column(_str("upload_state", 16))
        b.add_column(sa.Column("details_external", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "case_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("project_id", sa.Integer(), nullable=False, index=True),
        sa.Column("test_case_id", sa.Integer(), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        _str("sha256", 64), _str("storage", 16, "spool"), _str("spool_path", 1024),
        sa.Column("external_ref", sa.JSON(), nullable=False, server_default="{}"),
        _str("external_path", 1024), _str("upload_status", 16, "pending"), _str("upload_error", 1000),
        sa.Column("upload_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("created_at", False, True),
        sa.UniqueConstraint("test_case_id", "version", name="uq_case_versions_case_version"),
        sa.ForeignKeyConstraint(["test_case_id", "tenant_id"], ["test_cases.id", "test_cases.tenant_id"],
                                ondelete="CASCADE", name="fk_case_versions_case_tenant"),
    )

    op.create_table(
        "privacy_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True),
        _str("requester_name", 160), _str("requester_email", 254), _str("kind", 24),
        sa.Column("details", sa.Text(), nullable=False, server_default=""),
        _str("status", 16, "open"),
        _ts("due_at", False),
        sa.Column("response", sa.Text(), nullable=False, server_default=""),
        sa.Column("handled_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("handled_at"), _str("ip", 64), _ts("created_at", False, True),
    )
    op.create_index("ix_privacy_requests_status", "privacy_requests", ["status"])
    op.create_index("ix_privacy_requests_created_at", "privacy_requests", ["created_at"])

    op.create_table(
        "legal_acceptances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("registration_id", sa.Integer(), sa.ForeignKey("tenant_registrations.id", ondelete="SET NULL"), nullable=True),
        _str("document", 16), _str("version", 32), _ts("accepted_at", False, True),
        _str("ip", 64), _str("user_agent", 300),
    )

    with op.batch_alter_table("tenant_registrations") as b:
        b.add_column(_str("country_code", 2))


def downgrade() -> None:
    with op.batch_alter_table("tenant_registrations") as b:
        b.drop_column("country_code")
    op.drop_table("legal_acceptances")
    op.drop_index("ix_privacy_requests_created_at", table_name="privacy_requests")
    op.drop_index("ix_privacy_requests_status", table_name="privacy_requests")
    op.drop_table("privacy_requests")
    op.drop_table("case_versions")
    with op.batch_alter_table("runs") as b:
        for c in ("details_external", "upload_state", "storage_provider", "storage_mode"):
            b.drop_column(c)
    op.drop_index("ix_evidence_upload_pending", table_name="evidence")
    with op.batch_alter_table("evidence") as b:
        for c in ("deleted_at", "upload_attempts", "upload_error", "upload_status", "external_path",
                  "external_ref", "storage"):
            b.drop_column(c)
    op.drop_table("tenant_storage")
