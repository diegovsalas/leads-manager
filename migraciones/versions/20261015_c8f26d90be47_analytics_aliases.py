"""Vinculaciones de clientes Analytics confirmadas por cuenta."""
from alembic import op

revision = 'c8f26d90be47'
down_revision = 'b729d304af61'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('ALTER TABLE cs_accounts ADD COLUMN IF NOT EXISTS analytics_aliases JSON')


def downgrade():
    op.drop_column('cs_accounts', 'analytics_aliases')
