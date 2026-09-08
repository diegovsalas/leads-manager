#!/usr/bin/env python3
"""
Datos mínimos para el entorno LOCAL de pruebas.

No toca producción a propósito: aborta si DATABASE_URL apunta a Supabase.
Es idempotente — se puede correr las veces que haga falta.

Uso:
    set -a; source .env.local; set +a
    python3 seed_local.py

Deja montado el escenario que hay que probar:
  · un admin para entrar
  · Quick Learning con dos oportunidades de Aromatex en sucursales distintas
    (prueba que el guardia de duplicados ya no las rebota)
  · una oportunidad y un lead de Pestex (prueban el gate de respaldo)
"""
import os
import sys

DB = os.getenv("DATABASE_URL", "")
if "supabase" in DB.lower():
    sys.exit("ABORTADO: DATABASE_URL apunta a Supabase (producción).")
if not DB:
    sys.exit("Falta DATABASE_URL. Corre:  set -a; source .env.local; set +a")

from avantex_crm import create_app                                    # noqa: E402
from extensions import db                                             # noqa: E402
from models import (                                                  # noqa: E402
    Usuario, UserCRM, RolCRM, RolComercial,
    Account, Oportunidad, EtapaOportunidad,
    Lead, EtapaPipeline,
)

CORREO = "local@grupoavantex.com"
PASSWORD = "local1234"


def get_or_create(modelo, filtro, **campos):
    obj = modelo.query.filter_by(**filtro).first()
    if obj:
        return obj, False
    obj = modelo(**{**filtro, **campos})
    db.session.add(obj)
    db.session.flush()
    return obj, True


def main():
    app = create_app()
    with app.app_context():
        vendedor, _ = get_or_create(
            Usuario, {"nombre": "Vendedor Local"},
            rol_comercial=RolComercial.ASESOR_COMERCIAL,
            especialidad_marca=["Aromatex", "Pestex"],
        )

        admin = UserCRM.query.filter_by(correo=CORREO).first()
        if not admin:
            admin = UserCRM(nombre="Admin Local", correo=CORREO,
                            rol=RolCRM.SUPER_ADMIN, usuario_id=vendedor.id)
            db.session.add(admin)
        admin.set_password(PASSWORD)   # se reescribe siempre, por si se olvidó

        quick, _ = get_or_create(
            Account, {"nombre": "Quick Learning"},
            industria="Educación", tamano="grande", num_sucursales=40,
        )

        # Dos sucursales distintas, misma empresa, misma UN. Antes del cambio
        # la segunda rebotaba con 409.
        for sucursal in ("Satélite", "Polanco"):
            get_or_create(
                Oportunidad,
                {"nombre": f"Aromatex — Quick Learning {sucursal}"},
                empresa="Quick Learning", account_id=quick.id, sitio=sucursal,
                marca_interes="Aromatex", monthly_amount=4500, valor=4500,
                etapa=EtapaOportunidad.NEGOCIACION, propietario_id=vendedor.id,
            )

        # Pestex: al arrastrarla a Cerrado Ganado debe pedir el respaldo.
        get_or_create(
            Oportunidad, {"nombre": "Pestex — Quick Learning Satélite"},
            empresa="Quick Learning", account_id=quick.id, sitio="Satélite",
            marca_interes="Pestex", monthly_amount=3200, valor=3200,
            etapa=EtapaOportunidad.NEGOCIACION, propietario_id=vendedor.id,
        )

        # Lead de Pestex en etapa desde la que se puede cerrar ganado.
        get_or_create(
            Lead, {"nombre": "Contacto Pestex Local"},
            telefono="8110000001", email="pestex.local@example.com",
            empresa_nombre="Quick Learning", account_id=quick.id,
            marca_interes="Pestex", marcas_interes=["Pestex"],
            valor_estimado=3200, etapa_pipeline=EtapaPipeline.NEGOCIACION,
            usuario_asignado_id=vendedor.id,
        )

        # Lead de Aromatex: control — este debe cerrar SIN pedir respaldo.
        get_or_create(
            Lead, {"nombre": "Contacto Aromatex Local"},
            telefono="8110000002", email="aromatex.local@example.com",
            empresa_nombre="Quick Learning", account_id=quick.id,
            marca_interes="Aromatex", marcas_interes=["Aromatex"],
            valor_estimado=4500, etapa_pipeline=EtapaPipeline.NEGOCIACION,
            usuario_asignado_id=vendedor.id,
        )

        db.session.commit()

        print(f"Listo. Entra en http://127.0.0.1:{os.getenv('PORT', '5001')}")
        print(f"  correo:   {CORREO}")
        print(f"  password: {PASSWORD}")
        print(f"  oportunidades: {Oportunidad.query.count()}   leads: {Lead.query.count()}")


if __name__ == "__main__":
    main()
