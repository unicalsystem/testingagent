"""Commercial plans and billing: plans, organization licences, coupons, payments, usage counters

Additive. Existing organizations get a "legacy" licence (unlimited, no expiry) so nothing changes for them.
Four editable starter plans are seeded (prices are placeholders for the platform administrator to set).

Revision ID: 0011
Revises: 0010
"""
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def _ts(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _prices(inr_month, usd_month):
    disc = {1: 0, 3: 5, 6: 10, 12: 20}
    out = {}
    for cur, base in (("INR", inr_month), ("USD", usd_month)):
        out[cur] = {str(m): int(round(base * m * (100 - d) / 100)) * 100 for m, d in disc.items()}
    return out


ALL = ["browser_automation", "ai", "api_testing", "monitoring", "mobile", "connectors", "sso", "custom_storage"]


def upgrade() -> None:
    op.create_table('billing_plans',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(40), nullable=False, unique=True),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('tagline', sa.String(200), nullable=False, server_default=''),
        sa.Column('badge', sa.String(40), nullable=False, server_default=''),
        sa.Column('is_trial', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('trial_days', sa.Integer(), nullable=False, server_default='7'),
        sa.Column('is_public', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('prices', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('features', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('limits', sa.JSON(), nullable=False, server_default='{}'),
        sa.Column('highlights', sa.JSON(), nullable=False, server_default='[]'),
        _ts('created_at'), _ts('updated_at'),
    )
    op.create_table('tenant_subscriptions',
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('billing_plans.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(12), nullable=False, server_default='none'),
        _ts('period_start', True), _ts('current_period_end', True),
        sa.Column('trial_used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('billing_name', sa.String(200), nullable=False, server_default=''),
        sa.Column('billing_tax_id', sa.String(40), nullable=False, server_default=''),
        sa.Column('billing_address', sa.Text(), nullable=False, server_default=''),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('reminders', sa.JSON(), nullable=False, server_default='{}'),
        _ts('updated_at'),
    )
    op.create_table('billing_coupons',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(40), nullable=False, unique=True),
        sa.Column('description', sa.String(300), nullable=False, server_default=''),
        sa.Column('kind', sa.String(10), nullable=False, server_default='extend'),
        sa.Column('extend_days', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('extend_plan_id', sa.Integer(), sa.ForeignKey('billing_plans.id', ondelete='SET NULL'), nullable=True),
        sa.Column('percent_off', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('amount_off', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('currency', sa.String(3), nullable=False, server_default=''),
        sa.Column('plan_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('tenant_slugs', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('max_redemptions', sa.Integer(), nullable=True),
        sa.Column('redemptions', sa.Integer(), nullable=False, server_default='0'),
        _ts('valid_until', True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        _ts('created_at'),
    )
    op.create_table('billing_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('plan_id', sa.Integer(), sa.ForeignKey('billing_plans.id', ondelete='SET NULL'), nullable=True),
        sa.Column('plan_name', sa.String(80), nullable=False, server_default=''),
        sa.Column('months', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('subtotal', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('discount', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tax', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('tax_label', sa.String(40), nullable=False, server_default=''),
        sa.Column('amount', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('coupon_id', sa.Integer(), sa.ForeignKey('billing_coupons.id', ondelete='SET NULL'), nullable=True),
        sa.Column('coupon_code', sa.String(40), nullable=False, server_default=''),
        sa.Column('gateway', sa.String(12), nullable=False),
        sa.Column('gateway_order_id', sa.String(120), nullable=False, server_default=''),
        sa.Column('gateway_payment_id', sa.String(120), nullable=False, server_default=''),
        sa.Column('status', sa.String(12), nullable=False, server_default='created'),
        sa.Column('error', sa.Text(), nullable=False, server_default=''),
        sa.Column('invoice_no', sa.String(40), nullable=False, server_default=''),
        sa.Column('billing_name', sa.String(200), nullable=False, server_default=''),
        sa.Column('billing_tax_id', sa.String(40), nullable=False, server_default=''),
        _ts('period_start', True), _ts('period_end', True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        _ts('created_at'), _ts('paid_at', True),
    )
    op.create_index('ix_billing_payments_tenant_id', 'billing_payments', ['tenant_id'])
    op.create_index('ix_billing_payments_gateway_order', 'billing_payments', ['gateway', 'gateway_order_id'])
    op.create_table('billing_coupon_redemptions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('coupon_id', sa.Integer(), sa.ForeignKey('billing_coupons.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), nullable=False),
        sa.Column('payment_id', sa.Integer(), sa.ForeignKey('billing_payments.id', ondelete='SET NULL'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        _ts('created_at'),
        sa.UniqueConstraint('coupon_id', 'tenant_id', name='uq_coupon_redemption_tenant'),
    )
    op.create_index('ix_billing_coupon_redemptions_tenant_id', 'billing_coupon_redemptions', ['tenant_id'])
    op.create_table('billing_usage',
        sa.Column('tenant_id', sa.Integer(), sa.ForeignKey('tenants.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('month', sa.String(7), primary_key=True),
        sa.Column('metric', sa.String(40), primary_key=True),
        sa.Column('count', sa.Integer(), nullable=False, server_default='0'),
    )

    now = datetime.now(timezone.utc)
    J, S, B, I, T = sa.JSON(), sa.String(), sa.Boolean(), sa.Integer(), sa.DateTime(timezone=True)
    plans = sa.table('billing_plans', sa.column('code', S), sa.column('name', S), sa.column('tagline', S), sa.column('badge', S),
                     sa.column('is_trial', B), sa.column('trial_days', I), sa.column('is_public', B), sa.column('is_active', B),
                     sa.column('sort_order', I), sa.column('prices', J), sa.column('features', J), sa.column('limits', J),
                     sa.column('highlights', J), sa.column('created_at', T), sa.column('updated_at', T))

    def feats(*on):
        return {k: k in on for k in ALL}

    op.bulk_insert(plans, [
        dict(code='free-trial', name='Free trial', tagline='Try every feature for 7 days, no payment needed', badge='7-day free trial',
             is_trial=True, trial_days=7, is_public=True, is_active=True, sort_order=0, prices={}, features=feats(*ALL),
             limits={"users": 3, "projects": 2, "runs_per_month": 100, "api_runs_per_month": 50, "monitors": 2,
                     "mobile_runs_per_month": 20, "ai_actions_per_month": 50},
             highlights=['Community support'], created_at=now, updated_at=now),
        dict(code='starter', name='Starter', tagline='For small teams automating web and API regression', badge='',
             is_trial=False, trial_days=0, is_public=True, is_active=True, sort_order=10, prices=_prices(2999, 39),
             features=feats('browser_automation', 'ai', 'api_testing'),
             limits={"users": 3, "projects": 3, "runs_per_month": 300, "api_runs_per_month": 200, "monitors": 0,
                     "mobile_runs_per_month": 0, "ai_actions_per_month": 200},
             highlights=['E-mail support'], created_at=now, updated_at=now),
        dict(code='professional', name='Professional', tagline='Web, API and mobile testing with monitoring and integrations',
             badge='Recommended', is_trial=False, trial_days=0, is_public=True, is_active=True, sort_order=20,
             prices=_prices(7999, 99), features=feats('browser_automation', 'ai', 'api_testing', 'monitoring', 'mobile', 'connectors'),
             limits={"users": 10, "projects": 15, "runs_per_month": 1500, "api_runs_per_month": 1000, "monitors": 10,
                     "mobile_runs_per_month": 300, "ai_actions_per_month": 1000},
             highlights=['Priority e-mail support'], created_at=now, updated_at=now),
        dict(code='business', name='Business', tagline='For larger QA organizations with SSO and data-residency needs', badge='',
             is_trial=False, trial_days=0, is_public=True, is_active=True, sort_order=30, prices=_prices(19999, 249),
             features=feats(*ALL),
             limits={"users": 50, "projects": None, "runs_per_month": 10000, "api_runs_per_month": 10000, "monitors": 50,
                     "mobile_runs_per_month": 2000, "ai_actions_per_month": 5000},
             highlights=['Onboarding call', 'Priority support'], created_at=now, updated_at=now),
    ])
    # Existing organizations keep working exactly as before.
    op.execute("INSERT INTO tenant_subscriptions (tenant_id, status, updated_at) "
               "SELECT id, 'legacy', now() FROM tenants ON CONFLICT DO NOTHING")


def downgrade() -> None:
    for t in ('billing_usage', 'billing_coupon_redemptions', 'billing_payments', 'billing_coupons',
              'tenant_subscriptions', 'billing_plans'):
        op.drop_table(t)
