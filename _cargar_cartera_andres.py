"""Carga la cartera de Andres Garza (Director General).

Idempotente: correrlo dos veces no duplica nada. Se puede correr contra
local o contra produccion; la base la decide DATABASE_URL.

Dos bloques, porque viven en estructuras distintas a proposito:

  cs_accounts  los 6 clientes -> Andres como ejecutivo comercial.
               El KAM NO se toca: sigue siendo el dueno operativo.
  leads        los 6 prospectos -> al pipe, asignados a su perfil comercial.

Iconn queda fuera: esta Cerrado Ganado por Alan Aziz con venta registrada.
Tocarlo moveria una comision ya pagada.
"""
import os, sys

CORREO_ANDRES = "andresgarza@grupoavantex.com"

# Clientes actuales. Los tres primeros ya existen; los otros tres se crean.
CLIENTES = ["Coppel", "Farmacias del Ahorro", "Innova Sport",
            "Afirme", "La Comer", "Waldos"]

# Prospectos al pipe. Sin marca_interes a proposito: no sabemos de que unidad
# es cada uno, y adivinarla los metaria en las metas y comisiones de una UN
# que no los trabajo. Se llena cuando Andres lo sepa.
PROSPECTOS = ["Banorte", "Santander", "Pastelerias Lety",
              "Farmacias Benavides", "Metrorey", "Heineken"]


def main():
    from avantex_crm import create_app
    from extensions import db
    from models import CSAccount, UserCRM, Lead, OrigenLead, EtapaPipeline

    app = create_app()
    with app.app_context():
        andres = UserCRM.query.filter_by(correo=CORREO_ANDRES).first()
        if not andres:
            sys.exit(f"ABORTA: no existe {CORREO_ANDRES} en users_crm")
        if not andres.usuario_id:
            sys.exit("ABORTA: Andres no tiene perfil comercial (usuarios); "
                     "sin el no se le pueden asignar leads")

        hechos = []

        for nombre in CLIENTES:
            cta = CSAccount.query.filter(CSAccount.nombre.ilike(nombre)).first()
            if cta is None:
                cta = CSAccount(nombre=nombre, ejecutivo_id=andres.id)
                db.session.add(cta)
                db.session.flush()
                hechos.append(f"  cuenta creada    {nombre} ({cta.client_id}) "
                              f"KAM vacio")
            elif str(cta.ejecutivo_id or "") != str(andres.id):
                cta.ejecutivo_id = andres.id
                hechos.append(f"  ejecutivo puesto {cta.nombre} ({cta.client_id}) "
                              f"KAM: {cta.kam.nombre if cta.kam else 'vacio'}")
            else:
                hechos.append(f"  ya estaba        {cta.nombre}")

        for empresa in PROSPECTOS:
            ya = Lead.query.filter(
                Lead.empresa_nombre.ilike(empresa),
                Lead.usuario_asignado_id == andres.usuario_id,
            ).first()
            if ya:
                hechos.append(f"  ya estaba        lead {empresa}")
                continue
            ajeno = Lead.query.filter(Lead.empresa_nombre.ilike(empresa)).first()
            if ajeno:
                hechos.append(f"  OMITIDO          lead {empresa} ya existe con "
                              f"otro dueno — revisar a mano")
                continue
            db.session.add(Lead(
                nombre=empresa,                      # sin contacto todavia
                empresa_nombre=empresa,
                origen=OrigenLead.PROSPECCION,
                etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
                usuario_asignado_id=andres.usuario_id,
                notas="Cartera de Andres Garza (Direccion General). "
                      "Prospecto: falta contacto, telefono y unidad de negocio.",
            ))
            hechos.append(f"  lead creado      {empresa}")

        db.session.commit()
        print("\n".join(hechos))

        n = CSAccount.query.filter_by(ejecutivo_id=andres.id).count()
        mrr = sum(float(c.mrr or 0) for c in
                  CSAccount.query.filter_by(ejecutivo_id=andres.id))
        leads = Lead.query.filter_by(usuario_asignado_id=andres.usuario_id).count()
        print(f"\nCartera de {andres.nombre}: {n} cuentas, "
              f"MRR {mrr:,.2f} | {leads} leads en el pipe")


if __name__ == "__main__":
    main()
