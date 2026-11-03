"""Oportunidades multi UN y respaldo de cierre por unidad.

oportunidades.marcas_interes guarda todas las unidades en juego, incluida la
dueña; se rellena con la dueña para que el filtro por UN devuelva lo mismo
que antes. cierre_evidencias.unidad dice qué unidad respalda cada archivo:
una venta cruzada exige uno por unidad.

Hay que aplicarla ANTES de desplegar el código: Oportunidad y
CierreEvidencia leen las columnas nuevas en cada consulta.
"""
from alembic import op

revision = '9d2e6b4c1f70'
down_revision = 'f0a3c19d7b52'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE oportunidades ADD COLUMN IF NOT EXISTS marcas_interes "
               "VARCHAR(80)[] NOT NULL DEFAULT '{}'")
    op.execute("""
        UPDATE oportunidades SET marcas_interes = ARRAY[marca_interes]
        WHERE marca_interes IS NOT NULL AND marca_interes <> ''
          AND marcas_interes = '{}'
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_oportunidades_marcas_interes "
               "ON oportunidades USING GIN (marcas_interes)")
    op.execute("ALTER TABLE cierre_evidencias ADD COLUMN IF NOT EXISTS unidad VARCHAR(30)")


def downgrade():
    op.execute("ALTER TABLE cierre_evidencias DROP COLUMN IF EXISTS unidad")
    op.execute("DROP INDEX IF EXISTS ix_oportunidades_marcas_interes")
    op.execute("ALTER TABLE oportunidades DROP COLUMN IF EXISTS marcas_interes")
