# tareas.py
"""
El registro único de las tareas periódicas del CRM.  FEAT-2026-08-31

Aquí vive la lista de las 13 tareas, y vive una sola vez. Cada entrada dice
qué hace, cada cuánto, qué necesita del entorno y si está activa. De ahí
salen tanto el APScheduler interno (avantex_crm._start_scheduler) como los
endpoints HTTP de este módulo, así que cambiar SCHEDULER_EN_PROCESO cambia
quién dispara las tareas, no cuáles.

Esa última frase es el motivo de que el registro sea uno solo. Antes había
dos listas, una por modo, y ya habían divergido:

  · `cadencia` estaba pausada a propósito en el scheduler interno pero
    expuesta en el externo. Apagar el scheduler la reactivaba sin que nadie
    lo pidiera, y cadencia no sincroniza nada: avanza etapas y cierra leads
    como perdidos.
  · Las tareas de Gmail pedían GOOGLE_CLIENT_ID en el modo externo. Esa es
    la variable del login con Google; lo que Gmail necesita es
    GMAIL_SERVICE_ACCOUNT_JSON, que es lo que el modo interno ya miraba.
  · `zoho-citas` pedía una de sus ocho variables en un modo y las ocho en
    el otro; `backup` no tenía puerta en el interno; el SDR recorría tres
    unidades escritas a mano en vez de las que hay en la base.

Por qué existe el modo externo: en el plan gratuito de Render el servicio
se apaga tras 15 minutos sin tráfico. Un proceso dormido no consulta Meta,
no sincroniza Savio y no respalda nada, y nadie se entera: no hay error,
simplemente no ocurre. Con SCHEDULER_EN_PROCESO=0 las tareas las dispara un
programador externo por HTTP, y de paso esas llamadas mantienen despierto
al servicio.

Nunca los dos modos a la vez: correrían cada tarea dos veces.
"""
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from flask import Blueprint, current_app, jsonify, request

tareas_bp = Blueprint("tareas", __name__)


# ── La forma de una tarea ────────────────────────────────────────

@dataclass
class Tarea:
    """Una tarea periódica, con todo lo que los dos modos necesitan saber.

    `trigger` es el disparo para APScheduler: (tipo, kwargs), en UTC. None
    significa que el scheduler interno la arma por su cuenta, porque su
    horario no cabe en una constante. Hoy solo el SDR, que saca la hora de
    la base y arma un cron por unidad.

    `requiere` son las variables de entorno sin las cuales la integración no
    está configurada. Si falta alguna, la tarea se omite; no es un error,
    es que esa integración no existe en esta instalación.

    `activa=False` es una pausa deliberada, y es distinta de `requiere`: no
    depende del entorno. La tarea funciona, pero no queremos que corra
    todavía. Al vivir en el registro, la pausa vale para los dos modos —que
    es justo lo que antes se perdía al cambiar de uno a otro.
    """
    fn: Callable
    hace: str
    cadencia: str
    trigger: Optional[tuple]
    requiere: Any = None
    activa: bool = True
    motivo_pausa: str = ""

    def __post_init__(self):
        if self.requiere is None:
            self.requiere = ()
        elif isinstance(self.requiere, str):
            self.requiere = (self.requiere,)
        else:
            self.requiere = tuple(self.requiere)

    def faltantes(self):
        """Las variables que la tarea necesita y no están puestas."""
        return [v for v in self.requiere if not os.getenv(v)]

    def disponible(self):
        """¿Debe correr? Activa y con su entorno completo."""
        return self.activa and not self.faltantes()


# ── Qué corre cada tarea ─────────────────────────────────────────

def _cadencia():
    from cadencia import check_cadencia
    return check_cadencia()


def _lead_notices():
    from lead_notifications import dispatch_pending
    return dispatch_pending()


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


def unidades_sdr():
    """Las unidades de negocio con config del engine en la base.

    Estaban escritas a mano aquí, así que una unidad nueva quedaba fuera del
    modo externo aunque tuviera su fila. El scheduler interno siempre las
    leyó de la base; ahora los dos miran al mismo lugar. El engine ya se
    salta por su cuenta las que tienen enabled=False.
    """
    from models import SdrDirEngineConfig
    return [c.unit for c in SdrDirEngineConfig.query.all()]


def _sdr_engine():
    import sdr_directivo_engine as engine
    hechas = []
    for unidad in unidades_sdr():
        try:
            engine.engine_run_daily_batch(unit=unidad)
            hechas.append(unidad)
        except Exception as e:
            current_app.logger.warning(f"sdr engine ({unidad}): {e}")
    return {"unidades": hechas}


# ── El registro ──────────────────────────────────────────────────
#
# Las horas de los `cron` son UTC, que es lo que entiende APScheduler y lo
# que piden casi todos los programadores externos. La columna `cadencia`
# las repite en hora de México (UTC−6) para quien lea esto, y es la que
# aparece en TAREAS.md.

_ZOHO_VARS = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN",
              "ZOHO_USER_EMAIL", "ZOHO_WORKSPACE", "ZOHO_TABLE",
              "SUPABASE_URL", "SUPABASE_SERVICE_KEY")

TAREAS = {
    "avisos-asignacion": Tarea(
        _lead_notices, "Reintenta avisos de asignación de leads pendientes",
        "cada minuto", ("interval", {"minutes": 1}), "RESEND_API_KEY"),
    "meta-leads": Tarea(
        _meta_leads, "Trae los leads nuevos de Meta Lead Ads",
        "cada 5 min", ("interval", {"minutes": 5}), "META_PAGE_TOKEN"),

    "linkedin-leads": Tarea(
        _linkedin_leads, "Trae los leads nuevos de LinkedIn",
        "cada 5 min", ("interval", {"minutes": 5}), "LINKEDIN_ACCESS_TOKEN"),

    "gmail-poll": Tarea(
        _gmail_poll, "Lee los correos nuevos de los vendedores",
        "cada 5 min", ("interval", {"minutes": 5}), "GMAIL_SERVICE_ACCOUNT_JSON"),

    "cadencia": Tarea(
        _cadencia, "Seguimiento automático de leads sin respuesta",
        "cada 15 min", ("interval", {"minutes": 15}), None,
        activa=False,
        motivo_pausa="faltan los mappings campaign→marca/zona"),

    "savio-horario": Tarea(
        _savio_horario, "Facturas y pagos de Savio",
        "cada hora", ("interval", {"hours": 1}), "SAVIO_API_KEY"),

    "kam-respuestas": Tarea(
        _kam_respuestas, "Respuestas de clientes a los KAM",
        "cada hora", ("interval", {"hours": 1}), "GMAIL_SERVICE_ACCOUNT_JSON"),

    "savio-6h": Tarea(
        _savio_6h, "Suscripciones, clientes y MRR de Savio",
        "cada 6 horas", ("interval", {"hours": 6}), "SAVIO_API_KEY"),

    "notificaciones": Tarea(
        _notificaciones, "Correo diario a cada vendedor",
        "diaria 9:00 CST", ("cron", {"hour": 15, "minute": 0}), "RESEND_API_KEY"),

    "backup": Tarea(
        _backup, "Respaldo de la base a Supabase Storage",
        "diaria 3:00 CST", ("cron", {"hour": 9, "minute": 0}),
        ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")),

    "gmail-purge": Tarea(
        _gmail_purge, "Borra correos viejos del CRM",
        "diaria 4:00 CST", ("cron", {"hour": 10, "minute": 0}),
        "GMAIL_SERVICE_ACCOUNT_JSON"),

    "zoho-citas": Tarea(
        _zoho_citas, "Trae las citas de Zoho Analytics",
        "diaria 4:30 CST", ("cron", {"hour": 10, "minute": 30}), _ZOHO_VARS),

    # trigger=None: el scheduler interno arma un cron por unidad, con la
    # hora que cada una tiene en sdr_dir_engine_config. Por HTTP se dispara
    # como una sola tarea que las recorre todas.
    "sdr-engine": Tarea(
        _sdr_engine, "Lote diario del SDR directivo",
        "diaria, según la config de cada unidad", None, None),

    "savio-reconcilia": Tarea(
        _savio_reconciliacion,
        "Barrido completo de Savio, por si el incremental saltó algo",
        "semanal, domingo 3:30 CST",
        ("cron", {"day_of_week": "sun", "hour": 9, "minute": 30}),
        "SAVIO_API_KEY"),
}


def scheduler_en_proceso():
    """¿El APScheduler interno debe arrancar?

    Por omisión sí, para que nada cambie en las instalaciones que ya
    funcionan. En plan gratuito se pone SCHEDULER_EN_PROCESO=0 y las tareas
    pasan a dispararse desde fuera.
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
            {"nombre": n,
             "hace": t.hace,
             "cadencia": t.cadencia,
             "habilitada": t.disponible(),
             "activa": t.activa,
             "pausa": t.motivo_pausa or None,
             "requiere": list(t.requiere),
             "faltantes": t.faltantes()}
            for n, t in TAREAS.items()
        ],
    })


@tareas_bp.route("/<nombre>", methods=["POST", "GET"])
def ejecutar(nombre):
    """Corre una tarea. Acepta GET además de POST porque varios
    programadores gratuitos solo saben hacer GET."""
    ok, motivo = _autorizada()
    if not ok:
        return jsonify({"error": motivo}), 403

    tarea = TAREAS.get(nombre)
    if not tarea:
        return jsonify({"error": f"No existe la tarea «{nombre}»",
                        "disponibles": sorted(TAREAS)}), 404

    # Una pausa se puede saltar a mano —para probar antes de reactivarla—
    # pero nunca por accidente desde un cron: hay que pedirlo explícito.
    forzar = request.args.get("forzar", "").strip().lower() in ("1", "true", "si", "sí")
    if not tarea.activa and not forzar:
        return jsonify({"ok": True, "tarea": nombre, "omitida": True,
                        "motivo": f"Pausada: {tarea.motivo_pausa}",
                        "forzable": "agrega &forzar=1 para correrla igual"}), 200

    # Esto no se fuerza: sin sus variables la tarea no puede funcionar.
    faltan = tarea.faltantes()
    if faltan:
        return jsonify({"ok": True, "tarea": nombre, "omitida": True,
                        "motivo": f"Falta {', '.join(faltan)}: "
                                  f"la integración no está configurada"}), 200

    inicio = time.monotonic()
    try:
        resultado = tarea.fn()
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
