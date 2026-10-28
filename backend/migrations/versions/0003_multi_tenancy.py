"""multi-tenancy foundation

Adds tenants, public registrations, branding and invitations, and gives every tenant-owned row a
mandatory tenant_id. Children of projects/runs reference their parent through a composite
(id, tenant_id) foreign key, so the database itself refuses a row whose tenant differs from its
parent's.

All existing data is assigned to the default tenant (slug DEFAULT_TENANT_SLUG, default "unical"),
so release 1.0 workflows keep working. Legacy roles are mapped: admin -> tenant_admin (and Unical
Super Admin), tester -> qa_engineer, viewer -> auditor.

Revision ID: 0003
Revises: 0002
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

DEFAULT_SLUG = os.environ.get("DEFAULT_TENANT_SLUG", "unical").strip().lower() or "unical"
DEFAULT_NAME = os.environ.get("DEFAULT_TENANT_NAME", "Unical Systems")

# table -> parent for the composite (parent_id, tenant_id) foreign key
CHILDREN = [
    # table, local column, parent table, old FK name, new FK name
    ("project_secrets", "project_id", "projects", "project_secrets_project_id_fkey", "fk_project_secrets_project_tenant"),
    ("test_cases", "project_id", "projects", "test_cases_project_id_fkey", "fk_test_cases_project_tenant"),
    ("runs", "project_id", "projects", "runs_project_id_fkey", "fk_runs_project_tenant"),
    ("run_steps", "run_id", "runs", "run_steps_run_id_fkey", "fk_run_steps_run_tenant"),
    ("evidence", "run_id", "runs", "evidence_run_id_fkey", "fk_evidence_run_tenant"),
    ("evidence", "project_id", "projects", "evidence_project_id_fkey", "fk_evidence_project_tenant"),
]
TENANT_TABLES = ["users", "sessions", "allowed_targets", "projects", "project_secrets", "test_cases", "runs",
                 "run_steps", "evidence"]


def upgrade() -> None:
    op.create_table(
        "tenant_registrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_name", sa.String(160), nullable=False),
        sa.Column("requested_slug", sa.String(40), nullable=False),
        sa.Column("contact_name", sa.String(120), nullable=False),
        sa.Column("contact_email", sa.String(254), nullable=False),
        sa.Column("contact_phone", sa.String(40), nullable=False, server_default=""),
        sa.Column("country", sa.String(80), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decision_note", sa.String(1000), nullable=False, server_default=""),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_registrations_requested_slug", "tenant_registrations", ["requested_slug"])
    op.create_index("ix_tenant_registrations_contact_email", "tenant_registrations", ["contact_email"])
    op.create_index("ix_tenant_registrations_status", "tenant_registrations", ["status"])
    op.create_index("ix_tenant_registrations_created_at", "tenant_registrations", ["created_at"])

    op.create_table(
        "tenants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(40), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("status_reason", sa.String(500), nullable=False, server_default=""),
        sa.Column("registration_id", sa.Integer(),
                  sa.ForeignKey("tenant_registrations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_tenants_status"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "tenant_branding",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("display_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("logo_data_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("primary_color", sa.String(7), nullable=False, server_default="#2a63d4"),
        sa.Column("welcome_text", sa.String(1000), nullable=False, server_default=""),
        sa.Column("support_email", sa.String(254), nullable=False, server_default=""),
        sa.Column("support_url", sa.String(500), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_invitations_tenant_id", "invitations", ["tenant_id"])

    # --- default tenant owns all existing data ---------------------------------------------
    conn = op.get_bind()
    tid = conn.execute(sa.text("INSERT INTO tenants (slug, name, status) VALUES (:s, :n, 'active') RETURNING id"),
                       {"s": DEFAULT_SLUG, "n": DEFAULT_NAME}).scalar_one()
    conn.execute(sa.text("INSERT INTO tenant_branding (tenant_id, display_name) VALUES (:t, :n)"),
                 {"t": tid, "n": DEFAULT_NAME})

    for t in TENANT_TABLES:
        op.add_column(t, sa.Column("tenant_id", sa.Integer(), nullable=True))
        conn.execute(sa.text(f"UPDATE {t} SET tenant_id = :t"), {"t": tid})
        op.alter_column(t, "tenant_id", nullable=False)
        op.create_foreign_key(f"fk_{t}_tenant", t, "tenants", ["tenant_id"], ["id"], ondelete="CASCADE")
        op.create_index(f"ix_{t}_tenant_id", t, ["tenant_id"])

    # audit: nullable tenant + scope
    op.add_column("audit_log", sa.Column("tenant_id", sa.Integer(), nullable=True))
    op.add_column("audit_log", sa.Column("scope", sa.String(12), nullable=False, server_default="tenant"))
    conn.execute(sa.text("UPDATE audit_log SET tenant_id = :t"), {"t": tid})
    op.create_foreign_key("fk_audit_log_tenant", "audit_log", "tenants", ["tenant_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_audit_log_tenant_id", "audit_log", ["tenant_id"])

    # users: username unique per tenant; roles; platform admin flag
    op.drop_index("ix_users_username", table_name="users")
    op.create_index("ix_users_username", "users", ["username"])
    op.create_unique_constraint("uq_users_tenant_username", "users", ["tenant_id", "username"])
    op.create_unique_constraint("uq_users_id_tenant", "users", ["id", "tenant_id"])
    op.add_column("users", sa.Column("email", sa.String(254), nullable=False, server_default=""))
    op.add_column("users", sa.Column("is_platform_admin", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.alter_column("users", "role", type_=sa.String(24))
    conn.execute(sa.text("UPDATE users SET is_platform_admin = true WHERE role = 'admin'"))
    conn.execute(sa.text("UPDATE users SET role = CASE role WHEN 'admin' THEN 'tenant_admin' "
                         "WHEN 'tester' THEN 'qa_engineer' WHEN 'viewer' THEN 'auditor' ELSE role END"))

    # allowlist is per tenant
    op.drop_constraint("uq_allowed_targets_host", "allowed_targets", type_="unique")
    op.create_unique_constraint("uq_allowed_targets_tenant_host", "allowed_targets", ["tenant_id", "host"])

    # parents expose (id, tenant_id) so children can reference both
    op.create_unique_constraint("uq_projects_id_tenant", "projects", ["id", "tenant_id"])
    op.create_unique_constraint("uq_test_cases_id_tenant", "test_cases", ["id", "tenant_id"])
    op.create_unique_constraint("uq_runs_id_tenant", "runs", ["id", "tenant_id"])

    for table, col, parent, old_fk, new_fk in CHILDREN:
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{old_fk}"')
        op.create_foreign_key(new_fk, table, parent, [col, "tenant_id"], ["id", "tenant_id"], ondelete="CASCADE")


def downgrade() -> None:
    # Only safe while a single tenant exists (the default one): tenant data cannot be merged back.
    conn = op.get_bind()
    n = conn.execute(sa.text("SELECT count(*) FROM tenants")).scalar_one()
    if n > 1:
        raise RuntimeError("Cannot downgrade: more than one tenant exists. Restore the pre-upgrade backup instead.")
    for table, col, parent, old_fk, new_fk in CHILDREN:
        op.drop_constraint(new_fk, table, type_="foreignkey")
        op.create_foreign_key(old_fk, table, parent, [col], ["id"], ondelete="CASCADE")
    op.drop_constraint("uq_runs_id_tenant", "runs", type_="unique")
    op.drop_constraint("uq_test_cases_id_tenant", "test_cases", type_="unique")
    op.drop_constraint("uq_projects_id_tenant", "projects", type_="unique")
    op.drop_constraint("uq_allowed_targets_tenant_host", "allowed_targets", type_="unique")
    op.create_unique_constraint("uq_allowed_targets_host", "allowed_targets", ["host"])
    conn.execute(sa.text("UPDATE users SET role = CASE role WHEN 'tenant_owner' THEN 'admin' WHEN 'tenant_admin' THEN 'admin' "
                         "WHEN 'project_manager' THEN 'tester' WHEN 'qa_engineer' THEN 'tester' "
                         "WHEN 'developer' THEN 'tester' ELSE 'viewer' END"))
    op.alter_column("users", "role", type_=sa.String(16))
    op.drop_column("users", "is_platform_admin")
    op.drop_column("users", "email")
    op.drop_constraint("uq_users_id_tenant", "users", type_="unique")
    op.drop_constraint("uq_users_tenant_username", "users", type_="unique")
    op.drop_index("ix_users_username", table_name="users")
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.drop_index("ix_audit_log_tenant_id", table_name="audit_log")
    op.drop_constraint("fk_audit_log_tenant", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "scope")
    op.drop_column("audit_log", "tenant_id")
    for t in reversed(TENANT_TABLES):
        op.drop_index(f"ix_{t}_tenant_id", table_name=t)
        op.drop_constraint(f"fk_{t}_tenant", t, type_="foreignkey")
        op.drop_column(t, "tenant_id")
    op.drop_table("invitations")
    op.drop_table("tenant_branding")
    op.drop_table("tenants")
    op.drop_table("tenant_registrations")
