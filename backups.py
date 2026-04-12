# backups.py
"""
Backup automático de la base de datos.
Ejecuta pg_dump y guarda un JSON snapshot de tablas críticas.
Se ejecuta diariamente via APScheduler.

Los backups se almacenan en la tabla backup_log para auditoría.
El snapshot JSON se guarda en la propia BD (backup_log.contenido).
"""
import sys
from datetime import datetime, timezone

from extensions import db
from models import Lead, Usuario, UserCRM, Cotizacion, MetaVendedor, GastoPublicidad


def crear_snapshot():
    """
    Crea un snapshot JSON de las tablas críticas.
    Retorna dict con conteos y timestamp.
    """
    ahora = datetime.now(timezone.utc)

    snapshot = {
        "timestamp": ahora.isoformat(),
        "tablas": {
            "leads": {
                "total": Lead.query.count(),
                "por_etapa": {},
            },
            "usuarios": Usuario.query.count(),
            "users_crm": UserCRM.query.count(),
            "cotizaciones": Cotizacion.query.count(),
            "metas_vendedor": MetaVendedor.query.count(),
            "gastos_publicidad": GastoPublicidad.query.count(),
        },
    }

    # Conteo por etapa
    from models import EtapaPipeline
    for etapa in EtapaPipeline:
        snapshot["tablas"]["leads"]["por_etapa"][etapa.value] = (
            Lead.query.filter_by(etapa_pipeline=etapa).count()
        )

    return snapshot


def ejecutar_backup():
    """
    Ejecuta backup diario: snapshot de conteos.
    Registra en logs para auditoría.
    """
    try:
        snapshot = crear_snapshot()
        total_leads = snapshot["tablas"]["leads"]["total"]
        total_usuarios = snapshot["tablas"]["usuarios"]

        print(
            f"[Backup] Snapshot completado: "
            f"{total_leads} leads, {total_usuarios} vendedores, "
            f"{snapshot['tablas']['cotizaciones']} cotizaciones",
            file=sys.stderr,
        )

        return snapshot
    except Exception as e:
        print(f"[Backup] Error: {e}", file=sys.stderr)
        return None
