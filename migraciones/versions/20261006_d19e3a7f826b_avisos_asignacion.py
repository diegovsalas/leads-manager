"""Cola transaccional de avisos de asignación de leads.

Compatible con la creación idempotente del arranque durante la transición.
No genera avisos para asignaciones históricas.
"""
from alembic import op

revision = 'd19e3a7f826b'
down_revision = 'c41f8a2e6b17'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE IF NOT EXISTS lead_assignment_notices (
        id UUID PRIMARY KEY,
        lead_id UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
        vendedor_id UUID NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL,
        snapshot JSONB NOT NULL,
        payload JSONB,
        status VARCHAR(20) NOT NULL,
        attempts INTEGER NOT NULL,
        next_attempt_at TIMESTAMPTZ NOT NULL,
        first_attempt_at TIMESTAMPTZ,
        sent_at TIMESTAMPTZ,
        provider_id VARCHAR(120),
        last_error VARCHAR(80)
    )""")
    for column in ("lead_id", "status", "next_attempt_at"):
        op.execute(f"CREATE INDEX IF NOT EXISTS ix_lead_assignment_notices_{column} ON lead_assignment_notices ({column})")


def downgrade():
    op.execute("DROP TABLE IF EXISTS lead_assignment_notices")
