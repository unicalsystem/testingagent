"""test management: case fields, test runs, test run items (tenant-scoped)

Additive only: safe on a populated database. test_runs / test_run_items carry tenant_id and reference
their parents through composite (id, tenant_id) foreign keys, like every other project child since 0003.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

_STR = [('external_id', 100), ('module', 200), ('sub_module', 200), ('priority', 40), ('scenario_type', 80),
        ('assigned_to', 120), ('import_batch', 40)]
_TEXT = ['requirement', 'preconditions', 'test_data', 'procedure', 'expected_result']


def upgrade() -> None:
    for name, n in _STR:
        op.add_column('test_cases', sa.Column(name, sa.String(length=n), nullable=False, server_default=''))
    for name in _TEXT:
        op.add_column('test_cases', sa.Column(name, sa.Text(), nullable=False, server_default=''))
    op.add_column('test_cases', sa.Column('attributes', sa.JSON(), nullable=False, server_default='{}'))
    op.create_index('ix_test_cases_external_id', 'test_cases', ['external_id'])

    op.create_table('test_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('source', sa.String(length=16), nullable=False, server_default='manual'),
        sa.Column('group_label', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('import_batch', sa.String(length=40), nullable=False, server_default=''),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id', 'tenant_id'], ['projects.id', 'projects.tenant_id'],
                                ondelete='CASCADE', name='fk_test_runs_project_tenant'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'tenant_id', name='uq_test_runs_id_tenant'),
    )
    op.create_index('ix_test_runs_tenant_id', 'test_runs', ['tenant_id'])
    op.create_index('ix_test_runs_project_id', 'test_runs', ['project_id'])
    op.create_table('test_run_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tenant_id', sa.Integer(), nullable=False),
        sa.Column('test_run_id', sa.Integer(), nullable=False),
        sa.Column('test_case_id', sa.Integer(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='untested'),
        sa.Column('actual_result', sa.Text(), nullable=False, server_default=''),
        sa.Column('notes', sa.Text(), nullable=False, server_default=''),
        sa.Column('assigned_to', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('executed_by', sa.Integer(), nullable=True),
        sa.Column('executed_by_name', sa.String(length=120), nullable=False, server_default=''),
        sa.Column('executed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('automated_run_id', sa.Integer(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['test_run_id', 'tenant_id'], ['test_runs.id', 'test_runs.tenant_id'],
                                ondelete='CASCADE', name='fk_test_run_items_run_tenant'),
        sa.ForeignKeyConstraint(['test_case_id', 'tenant_id'], ['test_cases.id', 'test_cases.tenant_id'],
                                ondelete='CASCADE', name='fk_test_run_items_case_tenant'),
        sa.ForeignKeyConstraint(['executed_by'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['automated_run_id'], ['runs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('test_run_id', 'test_case_id', name='uq_test_run_item_case'),
    )
    op.create_index('ix_test_run_items_tenant_id', 'test_run_items', ['tenant_id'])
    op.create_index('ix_test_run_items_test_run_id', 'test_run_items', ['test_run_id'])
    op.create_index('ix_test_run_items_test_case_id', 'test_run_items', ['test_case_id'])
    op.create_index('ix_test_run_items_automated_run_id', 'test_run_items', ['automated_run_id'])


def downgrade() -> None:
    op.drop_table('test_run_items')
    op.drop_table('test_runs')
    op.drop_index('ix_test_cases_external_id', table_name='test_cases')
    for name in ['attributes', *_TEXT, *[n for n, _ in _STR]]:
        op.drop_column('test_cases', name)
