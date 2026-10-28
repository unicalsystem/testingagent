"""Connectors to Azure DevOps / Jira / GitHub / GitLab / webhooks: integrations, push outbox, tracked issues

Additive only. Tenant-scoped; integrations reference their project through the composite (id, tenant_id) key.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def _tenant():
    return sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False)


def upgrade() -> None:
    op.create_table('integrations',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('trigger', sa.String(12), nullable=False, server_default='auto'),
        sa.Column('run_types', sa.JSON(), nullable=False, server_default='["api", "browser", "manual"]'),
        sa.Column('config', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('secret_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('secret_hint', sa.String(120), nullable=False, server_default=''),
        sa.Column('active_since', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='unverified'),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_integrations_id_tenant'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_integrations_project_tenant'),
    )
    op.create_index('ix_integrations_tenant_id', 'integrations', ['tenant_id'])
    op.create_index('ix_integrations_project_id', 'integrations', ['project_id'])

    op.create_table('integration_syncs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('integration_id', sa.Integer(), sa.ForeignKey('integrations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('source_kind', sa.String(12), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(12), nullable=False, server_default='pending'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('result', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('requested_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('integration_id', 'source_kind', 'source_id', name='uq_integration_sync_source'),
    )
    op.create_index('ix_integration_syncs_tenant_id', 'integration_syncs', ['tenant_id'])
    op.create_index('ix_integration_syncs_integration_id', 'integration_syncs', ['integration_id'])
    op.create_index('ix_integration_syncs_project_id', 'integration_syncs', ['project_id'])
    op.create_index('ix_integration_syncs_due', 'integration_syncs', ['status', 'next_attempt_at'])

    op.create_table('integration_issues',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('integration_id', sa.Integer(), sa.ForeignKey('integrations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('case_key', sa.String(80), nullable=False),
        sa.Column('title', sa.String(300), nullable=False, server_default=''),
        sa.Column('external_id', sa.String(100), nullable=False, server_default=''),
        sa.Column('external_url', sa.String(1000), nullable=False, server_default=''),
        sa.Column('state', sa.String(12), nullable=False, server_default='failing'),
        sa.Column('last_source', sa.String(40), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('integration_id', 'case_key', name='uq_integration_issue_case'),
    )
    op.create_index('ix_integration_issues_tenant_id', 'integration_issues', ['tenant_id'])
    op.create_index('ix_integration_issues_integration_id', 'integration_issues', ['integration_id'])


def downgrade() -> None:
    for t in ('integration_issues', 'integration_syncs', 'integrations'):
        op.drop_table(t)
