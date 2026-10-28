"""authentication settings, Microsoft identity, groups and project assignments

- platform_settings, tenant_auth_settings, identity_providers, external_identities, oidc_transactions,
  password_resets, captcha_tokens_seen
- user_groups, group_members, project_assignments (tenant-bound composite foreign keys)
- sessions.auth_method; unique e-mail per tenant (non-empty, case-insensitive)
- Compatibility: every existing non-administrator gets an "all projects" assignment with its current
  role, so release-1.1 access is unchanged. Owners and administrators keep implicit access.

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _ts(name, nullable=True, default=False):
    kw = {"server_default": sa.func.now()} if default else {}
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def upgrade() -> None:
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(40), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("updated_at", False, True),
    )
    op.create_table(
        "tenant_auth_settings",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("microsoft_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("trusted_ips", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("updated_at", False, True),
    )
    op.create_table(
        "identity_providers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False, server_default="microsoft"),
        sa.Column("entra_tenant_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("client_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("client_secret_enc", sa.Text(), nullable=False, server_default=""),
        sa.Column("secret_hint", sa.String(16), nullable=False, server_default=""),
        _ts("secret_updated_at"),
        sa.Column("cloud_host", sa.String(100), nullable=False, server_default="login.microsoftonline.com"),
        sa.Column("allowed_domains", sa.JSON(), nullable=False),
        sa.Column("allowed_group_ids", sa.JSON(), nullable=False),
        sa.Column("provisioning", sa.String(20), nullable=False, server_default="invite_only"),
        sa.Column("auto_role", sa.String(24), nullable=False, server_default="auditor"),
        sa.Column("status", sa.String(16), nullable=False, server_default="unverified"),
        _ts("last_validated_at"),
        sa.Column("last_error", sa.String(1000), nullable=False, server_default=""),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("updated_at", False, True),
        sa.UniqueConstraint("tenant_id", "provider", name="uq_identity_providers_tenant_provider"),
    )
    op.create_index("ix_identity_providers_tenant_id", "identity_providers", ["tenant_id"])
    op.create_table(
        "external_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("issuer", sa.String(300), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("email_at_link", sa.String(254), nullable=False, server_default=""),
        _ts("created_at", False, True),
        sa.UniqueConstraint("tenant_id", "provider", "issuer", "subject", name="uq_external_identity_subject"),
        sa.UniqueConstraint("user_id", "provider", name="uq_external_identity_user_provider"),
        sa.ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                                name="fk_external_identities_user_tenant"),
    )
    op.create_index("ix_external_identities_tenant_id", "external_identities", ["tenant_id"])
    op.create_index("ix_external_identities_user_id", "external_identities", ["user_id"])
    op.create_table(
        "oidc_transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("identity_providers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(10), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("invitation_id", sa.Integer(), sa.ForeignKey("invitations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("browser_binding", sa.String(64), nullable=False),
        _ts("created_at", False, True),
        _ts("expires_at", False),
        _ts("used_at"),
    )
    op.create_table(
        "password_resets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        _ts("created_at", False, True),
        _ts("expires_at", False),
        _ts("used_at"),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_password_resets_tenant_id", "password_resets", ["tenant_id"])
    op.create_table(
        "captcha_tokens_seen",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        _ts("seen_at", False, True),
    )
    op.create_index("ix_captcha_tokens_seen_seen_at", "captcha_tokens_seen", ["seen_at"])

    op.create_table(
        "user_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(1000), nullable=False, server_default=""),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("created_at", False, True),
        _ts("updated_at", False, True),
        sa.UniqueConstraint("tenant_id", "name", name="uq_user_groups_tenant_name"),
        sa.UniqueConstraint("id", "tenant_id", name="uq_user_groups_id_tenant"),
    )
    op.create_index("ix_user_groups_tenant_id", "user_groups", ["tenant_id"])
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("added_at", False, True),
        sa.ForeignKeyConstraint(["group_id", "tenant_id"], ["user_groups.id", "user_groups.tenant_id"], ondelete="CASCADE",
                                name="fk_group_members_group_tenant"),
        sa.ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                                name="fk_group_members_user_tenant"),
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])
    op.create_index("ix_group_members_tenant_id", "group_members", ["tenant_id"])
    op.create_table(
        "project_assignments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(10), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("role", sa.String(24), nullable=False),
        _ts("starts_at"),
        _ts("ends_at"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column("reason", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        _ts("created_at", False, True),
        _ts("updated_at", False, True),
        _ts("revoked_at"),
        sa.Column("revoked_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.ForeignKeyConstraint(["user_id", "tenant_id"], ["users.id", "users.tenant_id"], ondelete="CASCADE",
                                name="fk_assignments_user_tenant"),
        sa.ForeignKeyConstraint(["group_id", "tenant_id"], ["user_groups.id", "user_groups.tenant_id"], ondelete="CASCADE",
                                name="fk_assignments_group_tenant"),
        sa.ForeignKeyConstraint(["project_id", "tenant_id"], ["projects.id", "projects.tenant_id"], ondelete="CASCADE",
                                name="fk_assignments_project_tenant"),
        sa.CheckConstraint("(user_id IS NULL) <> (group_id IS NULL)", name="ck_assignments_one_principal"),
        sa.CheckConstraint("(scope = 'all' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
                           name="ck_assignments_scope"),
        sa.CheckConstraint("role IN ('project_manager','qa_engineer','developer','auditor')", name="ck_assignments_role"),
        sa.CheckConstraint("status IN ('active','revoked','expired')", name="ck_assignments_status"),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_assignments_window"),
    )
    op.create_index("ix_project_assignments_tenant_id", "project_assignments", ["tenant_id"])
    op.create_index("ix_project_assignments_project_id", "project_assignments", ["project_id"])
    op.create_index("ix_assignments_user", "project_assignments", ["tenant_id", "user_id"])
    op.create_index("ix_assignments_group", "project_assignments", ["tenant_id", "group_id"])

    op.add_column("sessions", sa.Column("auth_method", sa.String(16), nullable=False, server_default="password"))
    # login by e-mail needs e-mails to be unique per organization (blank allowed)
    op.execute("UPDATE users SET email = lower(trim(email))")
    op.execute("""
        UPDATE users u SET email = '' WHERE email <> '' AND EXISTS (
          SELECT 1 FROM users o WHERE o.tenant_id = u.tenant_id AND o.email = u.email AND o.id < u.id)""")
    op.execute("CREATE UNIQUE INDEX uq_users_tenant_email ON users (tenant_id, email) WHERE email <> ''")

    # Compatibility: keep release-1.1 access (non-administrators saw every project of their tenant).
    op.execute("""
        INSERT INTO project_assignments (tenant_id, user_id, scope, role, status, reason)
        SELECT tenant_id, id, 'all', role, 'active', 'Migrated from release 1.1 (tenant-wide access)'
        FROM users WHERE role IN ('project_manager','qa_engineer','developer','auditor')""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_users_tenant_email")
    op.drop_column("sessions", "auth_method")
    for t in ("project_assignments", "group_members", "user_groups", "captcha_tokens_seen", "password_resets",
              "oidc_transactions", "external_identities", "identity_providers", "tenant_auth_settings",
              "platform_settings"):
        op.drop_table(t)
