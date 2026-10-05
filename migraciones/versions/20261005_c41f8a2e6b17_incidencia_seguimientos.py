"""bitácora de seguimiento de incidencias

Revision ID: c41f8a2e6b17
Revises: b7e2c41d9a03
Create Date: 2026-10-05 12:00:00

IF NOT EXISTS porque _run_pending_migrations crea la misma tabla en el
arranque; mientras convivan, la que llegue segunda no debe fallar.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c41f8a2e6b17'
down_revision: Union[str, Sequence[str], None] = 'b7e2c41d9a03'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS cs_incidencia_seguimientos (
            id UUID PRIMARY KEY,
            incidencia_id UUID NOT NULL REFERENCES cs_incidencias(id) ON DELETE CASCADE,
            texto TEXT NOT NULL,
            autor VARCHAR(200) DEFAULT '',
            created_at TIMESTAMPTZ
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cs_incidencia_seguimientos_incidencia_id "
               "ON cs_incidencia_seguimientos (incidencia_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS cs_incidencia_seguimientos")
