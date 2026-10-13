"""Contratos privados y versionados por cuenta."""
from alembic import op
revision='b729d304af61'
down_revision='e285b1749cad'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE IF NOT EXISTS cs_contratos (
        id UUID PRIMARY KEY,
        account_id UUID NOT NULL REFERENCES cs_accounts(id) ON DELETE CASCADE,
        nombre VARCHAR(200) NOT NULL, mimetype VARCHAR(100) NOT NULL,
        contenido BYTEA NOT NULL, sha256 VARCHAR(64) NOT NULL, bytes INTEGER NOT NULL,
        activo BOOLEAN NOT NULL DEFAULT false, inicio DATE, fin DATE,
        resumen JSON NOT NULL, created_by VARCHAR(200) NOT NULL,
        actor_id VARCHAR(40) NOT NULL, created_at TIMESTAMPTZ NOT NULL
    )''')
    op.execute('CREATE INDEX IF NOT EXISTS ix_cs_contratos_account_id ON cs_contratos(account_id)')
    op.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_cs_contrato_activo ON cs_contratos(account_id) WHERE activo = true')


def downgrade():
    op.drop_table('cs_contratos')
