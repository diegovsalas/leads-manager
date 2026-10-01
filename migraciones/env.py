"""Configuracion de Alembic para el CRM.

Deliberadamente NO llama a create_app(). La fabrica de la app corre
_run_pending_migrations() —690 lineas de DDL a mano— en cada arranque, y eso
es precisamente lo que Alembic viene a reemplazar: dejarlo en el camino haria
que cada comando `alembic` disparara las migraciones viejas antes de calcular
las nuevas. Para leer el esquema basta con importar los modelos.
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Cargar .env antes de leer DATABASE_URL: los comandos de alembic se corren a
# mano desde la terminal, sin el entorno que Render inyecta.
try:
    from dotenv import load_dotenv
    load_dotenv(".env.local" if os.getenv("ENTORNO") == "local" else ".env")
except ImportError:
    pass

# El import de models puebla db.metadata con las 68 tablas. extensions.db no
# necesita una app para eso; la necesitaria solo para ejecutar consultas.
from extensions import db  # noqa: E402
import models  # noqa: F401,E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = db.metadata


def _url():
    """La misma normalizacion que create_app, por la misma razon.

    FIX-2026-09-25: el driver va explicito en la URL. SQLAlchemy 2.1 cambio el
    DBAPI por omision de postgresql:// de psycopg2 a psycopg (v3), que no esta
    en requirements, y tumbo un deploy sin que nadie tocara nada. Si Alembic
    normalizara distinto que la app, se conectarian con drivers distintos a la
    misma base.
    """
    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "Falta DATABASE_URL. Para local:\n"
            "  DATABASE_URL=$(grep '^DATABASE_URL=' .env.local | cut -d= -f2-) "
            "python3 -m alembic ..."
        )
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    seccion = config.get_section(config.config_ini_section, {})
    seccion["sqlalchemy.url"] = _url()
    conectable = engine_from_config(
        seccion, prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with conectable.connect() as conexion:
        context.configure(
            connection=conexion,
            target_metadata=target_metadata,
            # compare_type detecta cambios de tipo, no solo columnas nuevas.
            # Vale el ruido: la mitad de la deriva de este esquema son tipos.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
