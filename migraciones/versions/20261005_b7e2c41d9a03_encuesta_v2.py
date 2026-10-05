"""encuesta v2: cuestionario de cierre de año

Revision ID: b7e2c41d9a03
Revises: f3f4bed7765b
Create Date: 2026-10-05 10:00:00

IF NOT EXISTS porque _run_pending_migrations agrega las mismas columnas en el
arranque; mientras convivan, la que llegue segunda no debe fallar.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'b7e2c41d9a03'
down_revision: Union[str, Sequence[str], None] = 'f3f4bed7765b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNAS = (
    ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("csat_cumplimiento", "INTEGER"),
    ("csat_gestion_kam", "INTEGER"),
    ("csat_confianza_kam", "INTEGER"),
)


def upgrade() -> None:
    for col, ddl in COLUMNAS:
        op.execute(f"ALTER TABLE cs_encuestas ADD COLUMN IF NOT EXISTS {col} {ddl}")


def downgrade() -> None:
    for col, _ in reversed(COLUMNAS):
        op.execute(f"ALTER TABLE cs_encuestas DROP COLUMN IF EXISTS {col}")
