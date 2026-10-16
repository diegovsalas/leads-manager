"""Recomendaciones desde campo.

Técnico como perfil comercial sin usuario de CRM, el origen de lead
"Referido operaciones" y quién recomendó cada lead (nombre, celular y, si
está registrado, su perfil).

Hay que aplicarla ANTES de desplegar el código: Lead lee las
columnas nuevas en cada consulta, así que sin ellas se cae el pipe entero.
"""
from alembic import op

revision = 'f0a3c19d7b52'
down_revision = 'c8f26d90be47'
branch_labels = None
depends_on = None


def upgrade():
    # ADD VALUE no puede usarse en la misma transacción que lo agrega.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE rol_comercial_enum ADD VALUE IF NOT EXISTS 'Técnico'")
        op.execute("ALTER TYPE origen_lead_enum ADD VALUE IF NOT EXISTS 'Referido operaciones'")
    op.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS referido_nombre VARCHAR(150)")
    op.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS referido_telefono VARCHAR(30)")
    op.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS referido_por_id UUID REFERENCES usuarios(id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_leads_referido_por_id ON leads (referido_por_id)")


def downgrade():
    # Los valores de enum se quedan: Postgres no permite quitarlos.
    op.execute("DROP INDEX IF EXISTS ix_leads_referido_por_id")
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS referido_por_id")
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS referido_telefono")
    op.execute("ALTER TABLE leads DROP COLUMN IF EXISTS referido_nombre")
