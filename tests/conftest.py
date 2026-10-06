"""Infraestructura de pruebas.

Corre contra una base Postgres DEDICADA (avantex_crm_test), no contra
SQLite: los modelos usan UUID, ARRAY y JSONB en 118 lugares y SQLite no
los soporta. Probar contra un motor distinto al de produccion daria una
confianza falsa justo en las partes que mas importan.

Cada prueba arranca con las tablas vacias. No se usa rollback por prueba
porque el codigo bajo prueba hace commit por su cuenta —cerrar_lead_core,
asignar_lead_comercial— y un rollback externo no lo deshace.
"""
import os
import subprocess

import pytest

BASE_PRUEBAS = "avantex_crm_test"


def _url_pruebas():
    """URL de la base de pruebas, derivada de la local para no pedir config."""
    local = next(l for l in open(".env.local") if l.startswith("DATABASE_URL="))
    url = local.split("=", 1)[1].strip()
    return url.rsplit("/", 1)[0] + "/" + BASE_PRUEBAS


@pytest.fixture(scope="session", autouse=True)
def _crea_base():
    url = _url_pruebas()
    admin = url.rsplit("/", 1)[0] + "/postgres"
    # Se recrea de cero: una prueba nunca debe depender de lo que dejo otra.
    subprocess.run(["psql", admin, "-tAc",
                    f'DROP DATABASE IF EXISTS {BASE_PRUEBAS}'],
                   check=True, capture_output=True)
    subprocess.run(["psql", admin, "-tAc", f'CREATE DATABASE {BASE_PRUEBAS}'],
                   check=True, capture_output=True)
    yield url


@pytest.fixture(scope="session")
def app(_crea_base):
    os.environ["DATABASE_URL"] = _crea_base
    os.environ.setdefault("SECRET_KEY", "pruebas")
    os.environ["SCHEDULER_EN_PROCESO"] = "0"   # sin tareas de fondo
    from avantex_crm import create_app
    aplicacion = create_app()
    aplicacion.config["LEAD_ASSIGNMENT_EMAIL_DISPATCH"] = False
    with aplicacion.app_context():
        from extensions import db
        db.create_all()
    return aplicacion


@pytest.fixture(autouse=True)
def limpia(app):
    """Deja las tablas vacias antes de cada prueba."""
    with app.app_context():
        from extensions import db
        tablas = db.session.execute(db.text("""
            SELECT tablename FROM pg_tables WHERE schemaname='public'
        """)).scalars().all()
        if tablas:
            lista = ", ".join(f'"{t}"' for t in tablas)
            db.session.execute(db.text(f"TRUNCATE {lista} RESTART IDENTITY CASCADE"))
            db.session.commit()
        yield
