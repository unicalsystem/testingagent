"""API testing (suites, cases, runs, results) and API uptime monitoring (monitors, checks, incidents)

Additive only; every table is tenant-scoped. Suites, cases, runs and results reference their parents
through composite (id, tenant_id) foreign keys like the rest of the schema.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def _tenant():
    return sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False)


def _users(name):
    return sa.Column(name, sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)


def upgrade() -> None:
    op.create_table('api_suites',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('base_url', sa.String(2048), nullable=False),
        sa.Column('spec_source', sa.String(10), nullable=False, server_default='manual'),
        sa.Column('spec_url', sa.String(2048), nullable=False, server_default=''),
        sa.Column('spec_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('spec_title', sa.String(300), nullable=False, server_default=''),
        sa.Column('endpoints_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('auth_type', sa.String(16), nullable=False, server_default='none'),
        sa.Column('auth_config', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('auth_secret_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('auth_hint', sa.String(120), nullable=False, server_default=''),
        sa.Column('requires_auth', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('allow_writes', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('verify_tls', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('timeout_ms', sa.Integer(), nullable=False, server_default='15000'),
        sa.Column('max_response_ms', sa.Integer(), nullable=False, server_default='2000'),
        sa.Column('default_headers', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('import_state', sa.String(12), nullable=False, server_default=''),
        sa.Column('import_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('run_after_import', sa.Boolean(), nullable=False, server_default=sa.false()),
        _users('import_requested_by'),
        sa.Column('archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        _users('created_by'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_api_suites_id_tenant'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_api_suites_project_tenant'),
    )
    op.create_index('ix_api_suites_tenant_id', 'api_suites', ['tenant_id'])
    op.create_index('ix_api_suites_project_id', 'api_suites', ['project_id'])

    op.create_table('api_cases',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('suite_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('name', sa.String(300), nullable=False),
        sa.Column('category', sa.String(16), nullable=False, server_default='positive'),
        sa.Column('method', sa.String(10), nullable=False),
        sa.Column('path', sa.String(2048), nullable=False),
        sa.Column('definition', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('source', sa.String(12), nullable=False, server_default='spec'),
        sa.Column('is_write', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_api_cases_id_tenant'),
        sa.ForeignKeyConstraint(['suite_id', 'tenant_id'], ['api_suites.id', 'api_suites.tenant_id'],
                                ondelete='CASCADE', name='fk_api_cases_suite_tenant'),
    )
    op.create_index('ix_api_cases_tenant_id', 'api_cases', ['tenant_id'])
    op.create_index('ix_api_cases_suite_id', 'api_cases', ['suite_id'])

    op.create_table('api_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('suite_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='queued'),
        _users('requested_by'),
        sa.Column('options', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('summary', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('suite_snapshot', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('worker_id', sa.String(120), nullable=False, server_default=''),
        sa.Column('report_sha256', sa.String(64), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_api_runs_id_tenant'),
        sa.ForeignKeyConstraint(['suite_id', 'tenant_id'], ['api_suites.id', 'api_suites.tenant_id'],
                                ondelete='CASCADE', name='fk_api_runs_suite_tenant'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_api_runs_project_tenant'),
    )
    op.create_index('ix_api_runs_tenant_id', 'api_runs', ['tenant_id'])
    op.create_index('ix_api_runs_project_id', 'api_runs', ['project_id'])
    op.create_index('ix_api_runs_suite_id', 'api_runs', ['suite_id'])
    op.create_index('ix_api_runs_status_created', 'api_runs', ['status', 'created_at'])

    op.create_table('api_run_results',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('idx', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), sa.ForeignKey('api_cases.id', ondelete='SET NULL'), nullable=True),
        sa.Column('name', sa.String(300), nullable=False, server_default=''),
        sa.Column('category', sa.String(16), nullable=False, server_default=''),
        sa.Column('method', sa.String(10), nullable=False, server_default=''),
        sa.Column('path', sa.String(2048), nullable=False, server_default=''),
        sa.Column('url', sa.String(4096), nullable=False, server_default=''),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('assertions', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('request', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('response', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('run_id', 'idx', name='uq_api_run_results_idx'),
        sa.ForeignKeyConstraint(['run_id', 'tenant_id'], ['api_runs.id', 'api_runs.tenant_id'],
                                ondelete='CASCADE', name='fk_api_run_results_run_tenant'),
    )
    op.create_index('ix_api_run_results_tenant_id', 'api_run_results', ['tenant_id'])
    op.create_index('ix_api_run_results_run_id', 'api_run_results', ['run_id'])

    op.create_table('api_monitors',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('suite_id', sa.Integer(), sa.ForeignKey('api_suites.id', ondelete='SET NULL'), nullable=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('url', sa.String(2048), nullable=False),
        sa.Column('method', sa.String(8), nullable=False, server_default='GET'),
        sa.Column('headers', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('use_suite_auth', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('expected_status', sa.JSON(), nullable=False, server_default='["2xx"]'),
        sa.Column('body_contains', sa.String(500), nullable=False, server_default=''),
        sa.Column('max_response_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('timeout_ms', sa.Integer(), nullable=False, server_default='10000'),
        sa.Column('interval_seconds', sa.Integer(), nullable=False, server_default='300'),
        sa.Column('failure_threshold', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('alert_channels', sa.JSON(), nullable=False, server_default='["inapp"]'),
        sa.Column('alert_emails', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('webhook_url_enc', sa.Text(), nullable=False, server_default=''),
        sa.Column('webhook_hint', sa.String(120), nullable=False, server_default=''),
        sa.Column('webhook_kind', sa.String(10), nullable=False, server_default='slack'),
        sa.Column('status', sa.String(10), nullable=False, server_default='unknown'),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failing_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_check_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status_change_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        _users('created_by'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_api_monitors_id_tenant'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_api_monitors_project_tenant'),
    )
    op.create_index('ix_api_monitors_tenant_id', 'api_monitors', ['tenant_id'])
    op.create_index('ix_api_monitors_project_id', 'api_monitors', ['project_id'])
    op.create_index('ix_api_monitors_due', 'api_monitors', ['enabled', 'next_check_at'])

    op.create_table('monitor_checks',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        _tenant(),
        sa.Column('monitor_id', sa.Integer(), sa.ForeignKey('api_monitors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ok', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('http_status', sa.Integer(), nullable=True),
        sa.Column('duration_ms', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.String(1000), nullable=False, server_default=''),
    )
    op.create_index('ix_monitor_checks_tenant_id', 'monitor_checks', ['tenant_id'])
    op.create_index('ix_monitor_checks_monitor_time', 'monitor_checks', ['monitor_id', 'checked_at'])

    op.create_table('monitor_incidents',
        sa.Column('id', sa.Integer(), primary_key=True),
        _tenant(),
        sa.Column('monitor_id', sa.Integer(), sa.ForeignKey('api_monitors.id', ondelete='CASCADE'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('cause', sa.String(1000), nullable=False, server_default=''),
        sa.Column('failed_checks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('alerts', sa.JSON(), nullable=False, server_default='[]'),
    )
    op.create_index('ix_monitor_incidents_tenant_id', 'monitor_incidents', ['tenant_id'])
    op.create_index('ix_monitor_incidents_monitor_id', 'monitor_incidents', ['monitor_id'])


def downgrade() -> None:
    for t in ('monitor_incidents', 'monitor_checks', 'api_monitors', 'api_run_results', 'api_runs', 'api_cases', 'api_suites'):
        op.drop_table(t)
