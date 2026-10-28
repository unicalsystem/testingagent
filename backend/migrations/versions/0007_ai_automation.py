"""AI automation of manual cases: project setup steps, case use_setup, automation jobs/items (tenant-scoped)

Additive only. automation_jobs / automation_items carry tenant_id and reference their project, job and
test case through composite (id, tenant_id) foreign keys. Nullable references (test run, test-run item,
trial run) use plain SET NULL foreign keys; the application only links rows of the same organization.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('setup_steps', sa.JSON(), nullable=False, server_default='[]'))
    op.add_column('projects', sa.Column('app_notes', sa.Text(), nullable=False, server_default=''))
    op.add_column('test_cases', sa.Column('use_setup', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('automation_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('test_run_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='exploring'),
        sa.Column('options', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('app_map', sa.Text(), nullable=False, server_default=''),
        sa.Column('explore_log', sa.Text(), nullable=False, server_default=''),
        sa.Column('claude_calls', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_automation_jobs_project_tenant'),
        sa.ForeignKeyConstraint(['test_run_id'], ['test_runs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_automation_jobs_id_tenant'),
    )
    op.create_index('ix_automation_jobs_tenant_id', 'automation_jobs', ['tenant_id'])
    op.create_index('ix_automation_jobs_project_id', 'automation_jobs', ['project_id'])
    op.create_table('automation_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('test_case_id', sa.Integer(), nullable=False),
        sa.Column('test_run_item_id', sa.Integer(), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='queued'),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('steps', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('use_setup', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('trial_run_id', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['job_id', 'tenant_id'], ['automation_jobs.id', 'automation_jobs.tenant_id'],
                                ondelete='CASCADE', name='fk_automation_items_job_tenant'),
        sa.ForeignKeyConstraint(['test_case_id', 'tenant_id'], ['test_cases.id', 'test_cases.tenant_id'],
                                ondelete='CASCADE', name='fk_automation_items_case_tenant'),
        sa.ForeignKeyConstraint(['test_run_item_id'], ['test_run_items.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['trial_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_automation_items_tenant_id', 'automation_items', ['tenant_id'])
    op.create_index('ix_automation_items_job_id', 'automation_items', ['job_id'])
    op.create_index('ix_automation_items_test_case_id', 'automation_items', ['test_case_id'])
    op.create_index('ix_automation_items_trial_run_id', 'automation_items', ['trial_run_id'])
    op.create_index('ix_automation_items_status', 'automation_items', ['status'])


def downgrade() -> None:
    op.drop_table('automation_items')
    op.drop_table('automation_jobs')
    op.drop_column('test_cases', 'use_setup')
    op.drop_column('projects', 'app_notes')
    op.drop_column('projects', 'setup_steps')
