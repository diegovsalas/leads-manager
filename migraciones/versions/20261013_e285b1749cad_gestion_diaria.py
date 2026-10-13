"""Gestión diaria, alta KAM y compromisos persistentes."""
from alembic import op
revision = 'e285b1749cad'
down_revision = 'a6c7309ed421'
branch_labels = None
depends_on = None

ACCOUNT_COLUMNS = (('datos_por_validar', 'BOOLEAN NOT NULL DEFAULT false'),)
TASK_COLUMNS = (
    ('estado', "VARCHAR(30) NOT NULL DEFAULT 'Pendiente'"),
    ('fecha_original', 'DATE'), ('completed_at', 'TIMESTAMPTZ'),
    ('updated_at', 'TIMESTAMPTZ'), ('detalle', "JSON NOT NULL DEFAULT '{}'"),
)
REPORT_COLUMNS = (
    ('estado', "VARCHAR(20) NOT NULL DEFAULT 'Borrador'"),
    ('submitted_at', 'TIMESTAMPTZ'), ('reviewed_by', 'VARCHAR(200)'),
    ('reviewed_at', 'TIMESTAMPTZ'), ('review_note', 'TEXT'),
)


def upgrade():
    for table, columns in (('cs_accounts', ACCOUNT_COLUMNS), ('cs_tasks', TASK_COLUMNS), ('cs_weekly_reports', REPORT_COLUMNS)):
        for col, ddl in columns:
            op.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {ddl}')
    op.execute('''CREATE TABLE IF NOT EXISTS cs_gestiones (
        id UUID PRIMARY KEY,
        account_id UUID NOT NULL REFERENCES cs_accounts(id) ON DELETE CASCADE,
        task_id UUID REFERENCES cs_tasks(id) ON DELETE SET NULL,
        accion VARCHAR(30) NOT NULL, resultado TEXT NOT NULL,
        respaldo TEXT, actor VARCHAR(200) NOT NULL, actor_id VARCHAR(40) NOT NULL,
        snapshot JSON NOT NULL, created_at TIMESTAMPTZ NOT NULL
    )''')
    op.execute('CREATE INDEX IF NOT EXISTS ix_cs_gestiones_account_id ON cs_gestiones(account_id)')
    op.execute('CREATE INDEX IF NOT EXISTS ix_cs_gestiones_task_id ON cs_gestiones(task_id)')


def downgrade():
    op.drop_table('cs_gestiones')
    for table, columns in (('cs_weekly_reports', REPORT_COLUMNS), ('cs_tasks', TASK_COLUMNS), ('cs_accounts', ACCOUNT_COLUMNS)):
        for col, _ in reversed(columns):
            op.drop_column(table, col)
