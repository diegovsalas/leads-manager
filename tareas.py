# tareas.py
"""
Las tareas periódicas del CRM, invocables por HTTP.  FEAT-2026-08-31

Hasta ahora las 12 tareas vivían dentro del proceso web, en un
APScheduler que arranca con la app. Eso obliga a correr con un solo
worker —dos procesos duplicarían cada sincronización y cada correo— y,
sobre todo, las ata a que el proceso esté despierto.

En el plan gratuito de Render el servicio se apaga tras 15 minutos sin
tráfico. Un proceso dormido no consulta Meta, no sincroniza Savio y no
respalda nada, y nadie se entera: no hay error, simplemente no ocurre.

Este módulo expone cada tarea como una función suelta para que un
programador externo —cron-job.org, GitHub Actions, el cron de Render, o
cualquier otro— las dispare por HTTP. Como efecto secundario, esas
llamadas mantienen el servicio despierto.

El scheduler interno sigue funcionando igual. Se apaga poniendo
SCHEDULER_EN_PROCESO=0, y entonces manda el programador externo. Nunca
los dos a la vez: correrían la misma tarea dos veces.
"""
import os
import time

from flask import Blueprint, current_app, jsonify, request

tareas_bp = Blueprint("tareas", __name__)


# ── El registro ──────────────────────────────────────────────────
#
# Cada entrada es (función, descripción, cada_cuánto). La cadencia es
# informativa: la decide quien programa, no este archivo. Se documenta
# aquí para que quien configure el cron externo no tenga que adivinarla.

def _cadencia():
    from cadencia import check_cadencia
    return check_cadencia()


def _notificaciones():
    from notificaciones import enviar_notificaciones_diarias
    return enviar_notificaciones_diarias()


def _backup():
    from backups import ejecutar_backup
    return ejecutar_backup()


def _savio_horario():
    import savio_sync
    savio_sync.sync_invoices()
    savio_sync.sync_payments()
    # Puente Savio → CSInvoice para que el dashboard de CS vea pagos y
    # facturas nuevas sin esperar al de 6 horas.
    savio_sync.sync_savio_to_cs_invoices()
    return "invoices, payments y puente a CS"


def _savio_6h():
    import savio_sync
    savio_sync.sync_subscriptions()
    savio_sync.sync_customers()
    savio_sync.bridge_savio_to_cs_mrr()
    return "subscriptions, customers y MRR"


def _savio_reconciliacion():
    """Barrido completo del año, saltándose la marca incremental. Es el
    respaldo del sync horario, no el mecanismo principal."""
    import savio_sync
    savio_sync.sync_invoices(days=savio_sync.DEFAULT_SYNC_WINDOW_DAYS)
    savio_sync.sync_payments(days=savio_sync.DEFAULT_SYNC_WINDOW_DAYS)
    return "reconciliación completa"


def _gmail_poll():
    import gmail_monitor
    return gmail_monitor.poll_all()


def _gmail_purge():
    import gmail_monitor
    return gmail_monitor.purge_old()


def _kam_respuestas():
    import gmail_monitor
    return gmail_monitor.poll_kam_responses()


def _zoho_citas():
    import zoho_appointments_etl as etl
    return etl.run()


def _meta_leads():
    from meta_lead_polling import poll_and_create_leads
    return poll_and_create_leads()


def _linkedin_leads():
    from linkedin_lead_polling import poll_and_create_leads
    return poll_and_create_leads()


def _sdr_engine():
    import sdr_directivo_engine as engine
    hechas = []
    for unidad in ("aromatex", "pestex", "weldex"):
        try:
            engine.engine_run_daily_batch(unit=unidad)
            hechas.append(unidad)
        except Exception as e:
            current_app.logger.warning(f"sdr engine ({unidad}): {e}")
    return {"unidades": hechas}


# nombre -> (función, qué hace, cadencia sugerida, variable de entorno que la habilita)
TAREAS = {
    "meta-leads":       (_meta_leads, "Trae los leads nuevos de Meta Lead Ads",
                         "cada 5 min", "META_PAGE_TOKEN"),
    "linkedin-leads":   (_linkedin_leads, "Trae los leads nuevos de LinkedIn",
                         "cada 5 min", "LINKEDIN_ACCESS_TOKEN"),
    "gmail-poll":       (_gmail_poll, "Lee los correos nuevos de los vendedores",
                         "cada 5 min", "GOOGLE_CLIENT_ID"),
    "cadencia":         (_cadencia, "Seguimiento automático de leads sin respuesta",
                         "cada 15 min", None),
    "savio-horario":    (_savio_horario, "Facturas y pagos de Savio",
                         "cada hora", "SAVIO_API_KEY"),
    "kam-respuestas":   (_kam_respuestas, "Respuestas de clientes a los KAM",
                         "cada hora", "GOOGLE_CLIENT_ID"),
    "savio-6h":         (_savio_6h, "Suscripciones, clientes y MRR de Savio",
                         "cada 6 horas", "SAVIO_API_KEY"),
    "notificaciones":   (_notificaciones, "Correo diario a cada vendedor",
                         "diaria 9:00 CST", None),
    "backup":           (_backup, "Respaldo de la base a Supabase Storage",
                         "diaria 3:00 CST", "SUPABASE_SERVICE_KEY"),
    "gmail-purge":      (_gmail_purge, "Borra correos viejos del CRM",
                         "diaria 4:00 CST", "GOOGLE_CLIENT_ID"),
    "zoho-citas":       (_zoho_citas, "Trae las citas de Zoho Analytics",
                         "diaria 4:30 CST", "ZOHO_CLIENT_ID"),
    "sdr-engine":       (_sdr_engine, "Lote diario del SDR directivo",
                         "diaria 5:00 CST", None),
    "savio-reconcilia": (_savio_reconciliacion, "Barrido completo de Savio, por si el incremental saltó algo",
                         "semanal", "SAVIO_API_KEY"),
}


def scheduler_en_proceso():
    """¿El APScheduler interno debe arrancar?

    Por omisión sí, para que nada cambie en las instalaciones que ya
    funcionan. En plan gratuito se pone SCHEDULER_EN_PROCESO=0 y las
    tareas pasan a dispararse desde fuera.
    """
    return os.getenv("SCHEDULER_EN_PROCESO", "1").strip().lower() not in ("0", "false", "no")


def _autorizada():
    """El secreto puede venir por cabecera o por parámetro, porque no todos
    los programadores gratuitos permiten mandar cabeceras."""
    esperado = os.getenv("TAREAS_SECRET", "").strip()
    if not esperado:
        return False, "TAREAS_SECRET no está configurado en el servidor"
    recibido = (request.headers.get("X-Tareas-Secret")
                or request.args.get("secret")
                or "").strip()
    if recibido != esperado:
        return False, "Secreto inválido"
    return True, ""


@tareas_bp.route("/", methods=["GET"])
def listar():
    """Qué tareas existen y cómo dispararlas. No ejecuta nada."""
    ok, motivo = _autorizada()
    if not ok:
        return jsonify({"error": motivo}), 403
    return jsonify({
        "scheduler_en_proceso": scheduler_en_proceso(),
        "tareas": [
            {"nombre": n, "hace": desc, "cadencia": cada,
             "habilitada": (env is None or bool(os.getenv(env))),
             "requiere": env}
            for n, (_, desc, cada, env) in TAREAS.items()
        ],
    })


@tareas_bp.route("/<nombre>", methods=["POST", "GET"])
def ejecutar(nombre):
    """Corre una tarea. Acepta GET además de POST porque varios
    programadores gratuitos solo saben hacer GET."""
    ok, motivo = _autorizada()
    if not ok:
        return jsonify({"error": motivo}), 403

    entrada = TAREAS.get(nombre)
    if not entrada:
        return jsonify({"error": f"No existe la tarea «{nombre}»",
                        "disponibles": sorted(TAREAS)}), 404

    funcion, desc, _cada, env = entrada
    if env and not os.getenv(env):
        # No es un error: esa integración simplemente no está configurada.
        return jsonify({"ok": True, "tarea": nombre, "omitida": True,
                        "motivo": f"Falta {env}: la integración no está configurada"}), 200

    inicio = time.monotonic()
    try:
        resultado = funcion()
        segundos = round(time.monotonic() - inicio, 2)
        current_app.logger.info(f"[tarea] {nombre} en {segundos}s → {resultado}")
        return jsonify({"ok": True, "tarea": nombre, "segundos": segundos,
                        "resultado": resultado if isinstance(resultado, (dict, list, str, int, float)) else str(resultado)})
    except Exception as e:
        segundos = round(time.monotonic() - inicio, 2)
        current_app.logger.error(f"[tarea] {nombre} FALLÓ en {segundos}s: {e}", exc_info=True)
        # 500 a propósito: el programador externo debe poder detectar el fallo
        # y avisar. Una tarea que falla en silencio es el problema que este
        # módulo viene a resolver.
        return jsonify({"ok": False, "tarea": nombre, "segundos": segundos,
                        "error": str(e)[:300]}), 500
