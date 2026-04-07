# cadencia.py
"""
Sistema de cadencia automatica de follow-up.

Reglas:
  1er Contacto → 2do Contacto:  24h sin respuesta
  2do Contacto → 3er Contacto:  48h sin respuesta
  3er Contacto → 4to Contacto:  48h sin respuesta
  4to Contacto → Cerrado Perdido: 48h sin respuesta

check_cadencia() se ejecuta cada 15 minutos via APScheduler.
"""
import logging
from datetime import datetime, timezone, timedelta

from extensions import db, socketio
from models import Lead, EtapaPipeline

logger = logging.getLogger(__name__)

# Mapa: etapa_actual → (siguiente_etapa, horas_espera)
CADENCIA = {
    EtapaPipeline.CONTACTO_1: (EtapaPipeline.CONTACTO_2, 24),
    EtapaPipeline.CONTACTO_2: (EtapaPipeline.CONTACTO_3, 48),
    EtapaPipeline.CONTACTO_3: (EtapaPipeline.CONTACTO_4, 48),
    EtapaPipeline.CONTACTO_4: (EtapaPipeline.CIERRE_PERDIDO, 48),
}

# Etapas que participan en la cadencia
ETAPAS_CADENCIA = list(CADENCIA.keys())


def check_cadencia():
    """
    Revisa leads en etapas de contacto que no han recibido respuesta
    y avanza su etapa si el tiempo de espera expiró.
    """
    ahora = datetime.now(timezone.utc)
    avanzados = 0

    leads = Lead.query.filter(
        Lead.etapa_pipeline.in_(ETAPAS_CADENCIA),
        Lead.respondio_ultimo_contacto.is_(False),
    ).all()

    for lead in leads:
        siguiente, horas = CADENCIA[lead.etapa_pipeline]

        # Usar fecha_ultimo_contacto o fecha_actualizacion como referencia
        ref = lead.fecha_ultimo_contacto or lead.fecha_actualizacion
        if not ref:
            continue

        # Asegurar que ref tiene timezone
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)

        limite = ref + timedelta(hours=horas)

        if ahora >= limite:
            etapa_anterior = lead.etapa_pipeline.value
            lead.etapa_pipeline = siguiente
            lead.fecha_ultimo_contacto = ahora
            lead.respondio_ultimo_contacto = False

            # Calcular proximo contacto (si no es cierre)
            if siguiente in CADENCIA:
                _, prox_horas = CADENCIA[siguiente]
                lead.proximo_contacto = ahora + timedelta(hours=prox_horas)
            else:
                lead.proximo_contacto = None

            # Si se cierra como perdido, registrar motivo
            if siguiente == EtapaPipeline.CIERRE_PERDIDO:
                lead.motivo_perdida = "Sin respuesta tras 4 contactos"

            avanzados += 1
            logger.info(
                f"Cadencia: {lead.nombre} ({lead.telefono}) "
                f"{etapa_anterior} → {siguiente.value}"
            )

            # Emitir evento Socket.IO
            try:
                socketio.emit("lead_etapa_cambiada", {
                    "lead_id": str(lead.id),
                    "etapa_anterior": etapa_anterior,
                    "etapa_nueva": siguiente.value,
                    "nombre": lead.nombre,
                    "motivo": "cadencia_automatica",
                })
            except Exception:
                pass  # SocketIO puede no estar disponible en el scheduler

    if avanzados > 0:
        db.session.commit()
        logger.info(f"Cadencia: {avanzados} leads avanzados")

    return avanzados
