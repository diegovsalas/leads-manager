"""Seguimiento semanal de cuentas AAA, compatible con el arranque idempotente."""
from alembic import op

revision = 'a6c7309ed421'
down_revision = 'd19e3a7f826b'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE cs_accounts ADD COLUMN IF NOT EXISTS es_aaa BOOLEAN NOT NULL DEFAULT false')
    op.execute('''CREATE TABLE IF NOT EXISTS cs_weekly_reports (
        id UUID PRIMARY KEY,
        account_id UUID NOT NULL REFERENCES cs_accounts(id) ON DELETE CASCADE,
        cut_date DATE NOT NULL,
        payload JSON NOT NULL,
        updated_by VARCHAR(200),
        updated_at TIMESTAMPTZ,
        CONSTRAINT uq_cs_weekly_account_cut UNIQUE (account_id, cut_date)
    )''')


def downgrade():
    op.drop_table('cs_weekly_reports')
    op.drop_column('cs_accounts', 'es_aaa')
