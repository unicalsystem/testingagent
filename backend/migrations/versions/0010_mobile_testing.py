"""Mobile app testing: device providers, app builds, mobile cases, cross-device runs, live device sessions

Additive only; every table is tenant-scoped with composite (id, tenant_id) keys like the rest of the schema.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def _tenant():
    return sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False)


def _users(name):
    return sa.Column(name, sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)


def _project_fk(table):
    return sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                   ondelete='CASCADE', name=f'fk_{table}_project_tenant')


def _ts(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table('mobile_providers',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('secret_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('secret_hint', sa.String(120), nullable=False, server_default=''),
        sa.Column('status', sa.String(16), nullable=False, server_default='unverified'),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        _users('created_by'),
        _ts('created_at'), _ts('updated_at'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_providers_id_tenant'),
        _project_fk('mobile_providers'),
    )
    op.create_index('ix_mobile_providers_tenant_id', 'mobile_providers', ['tenant_id'])
    op.create_index('ix_mobile_providers_project_id', 'mobile_providers', ['project_id'])

    op.create_table('mobile_apps',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('platform', sa.String(10), nullable=False),
        sa.Column('version', sa.String(60), nullable=False, server_default=''),
        sa.Column('filename', sa.String(255), nullable=False, server_default=''),
        sa.Column('rel_path', sa.String(1024), nullable=False, server_default=''),
        sa.Column('source_url', sa.String(2048), nullable=False, server_default=''),
        sa.Column('size', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.String(64), nullable=False, server_default=''),
        sa.Column('app_id', sa.String(255), nullable=False, server_default=''),
        sa.Column('app_activity', sa.String(255), nullable=False, server_default=''),
        sa.Column('provider_refs', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        _users('created_by'),
        _ts('created_at'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_apps_id_tenant'),
        _project_fk('mobile_apps'),
    )
    op.create_index('ix_mobile_apps_tenant_id', 'mobile_apps', ['tenant_id'])
    op.create_index('ix_mobile_apps_project_id', 'mobile_apps', ['project_id'])

    op.create_table('mobile_cases',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('kind', sa.String(8), nullable=False, server_default='native'),
        sa.Column('platform', sa.String(10), nullable=False, server_default='android'),
        sa.Column('app_id', sa.Integer(), sa.ForeignKey('mobile_apps.id', ondelete='SET NULL'), nullable=True),
        sa.Column('start_url', sa.String(2048), nullable=False, server_default=''),
        sa.Column('steps', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('source', sa.String(12), nullable=False, server_default='manual'),
        sa.Column('test_case_id', sa.Integer(), sa.ForeignKey('test_cases.id', ondelete='SET NULL'), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        _users('created_by'),
        _ts('created_at'), _ts('updated_at'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_cases_id_tenant'),
        _project_fk('mobile_cases'),
    )
    op.create_index('ix_mobile_cases_tenant_id', 'mobile_cases', ['tenant_id'])
    op.create_index('ix_mobile_cases_project_id', 'mobile_cases', ['project_id'])

    op.create_table('mobile_run_groups',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(200), nullable=False, server_default=''),
        sa.Column('provider_id', sa.Integer(), sa.ForeignKey('mobile_providers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('devices', sa.JSON(), nullable=False, server_default='[]'),
        _users('requested_by'),
        _ts('created_at'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_run_groups_id_tenant'),
        _project_fk('mobile_run_groups'),
    )
    op.create_index('ix_mobile_run_groups_tenant_id', 'mobile_run_groups', ['tenant_id'])
    op.create_index('ix_mobile_run_groups_project_id', 'mobile_run_groups', ['project_id'])

    op.create_table('mobile_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('mobile_cases.id', ondelete='SET NULL'), nullable=True),
        sa.Column('provider_id', sa.Integer(), sa.ForeignKey('mobile_providers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('device', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('case_snapshot', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('status', sa.String(16), nullable=False, server_default='queued'),
        sa.Column('step_results', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('session_id', sa.String(200), nullable=False, server_default=''),
        sa.Column('session_url', sa.String(1000), nullable=False, server_default=''),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('worker_id', sa.String(120), nullable=False, server_default=''),
        _users('requested_by'),
        _ts('created_at'), _ts('started_at', True), _ts('finished_at', True), _ts('heartbeat_at', True),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_runs_id_tenant'),
        sa.ForeignKeyConstraint(['group_id', 'tenant_id'], ['mobile_run_groups.id', 'mobile_run_groups.tenant_id'],
                                ondelete='CASCADE', name='fk_mobile_runs_group_tenant'),
    )
    op.create_index('ix_mobile_runs_tenant_id', 'mobile_runs', ['tenant_id'])
    op.create_index('ix_mobile_runs_project_id', 'mobile_runs', ['project_id'])
    op.create_index('ix_mobile_runs_group_id', 'mobile_runs', ['group_id'])
    op.create_index('ix_mobile_runs_status_created', 'mobile_runs', ['status', 'created_at'])

    op.create_table('mobile_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('purpose', sa.String(8), nullable=False, server_default='record'),
        sa.Column('provider_id', sa.Integer(), sa.ForeignKey('mobile_providers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('device', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('target', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('goal', sa.Text(), nullable=False, server_default=''),
        sa.Column('test_case_id', sa.Integer(), sa.ForeignKey('test_cases.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='starting'),
        sa.Column('phase', sa.String(12), nullable=False, server_default=''),
        sa.Column('session_id', sa.String(200), nullable=False, server_default=''),
        sa.Column('session_url', sa.String(1000), nullable=False, server_default=''),
        sa.Column('steps', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('log', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('worker_id', sa.String(120), nullable=False, server_default=''),
        sa.Column('cmd_seq', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cmd', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('res', sa.JSON(), nullable=False, server_default='{}'),
        _ts('heartbeat_at', True),
        _users('created_by'),
        _ts('created_at'), _ts('last_used_at'), _ts('closed_at', True),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_mobile_sessions_id_tenant'),
        _project_fk('mobile_sessions'),
    )
    op.create_index('ix_mobile_sessions_tenant_id', 'mobile_sessions', ['tenant_id'])
    op.create_index('ix_mobile_sessions_project_id', 'mobile_sessions', ['project_id'])
    op.create_index('ix_mobile_sessions_status', 'mobile_sessions', ['status'])


def downgrade() -> None:
    for t in ('mobile_sessions', 'mobile_runs', 'mobile_run_groups', 'mobile_cases', 'mobile_apps', 'mobile_providers'):
        op.drop_table(t)
