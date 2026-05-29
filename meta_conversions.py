"""
Meta Conversions API — envía eventos server-side a Meta para optimización de campañas.

Eventos mapeados desde el pipeline del CRM:
  Negociación    → Lead        (lead calificado)
  Cotización     → InitiateCheckout (propuesta enviada)
  Demo           → Schedule    (demo agendada)
  Cierre Ganado  → Purchase    (venta cerrada)

Configuración via env vars:
  META_PIXEL_ID          — Pixel / Dataset ID (ej. 1489999282014048)
  META_CONVERSIONS_TOKEN — Access token con permiso ads_management
  META_API_VERSION       — (default v19.0)
"""
import hashlib
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("meta_conversions")

PIXEL_ID = os.getenv("META_PIXEL_ID", "")
CONVERSIONS_TOKEN = os.getenv("META_CONVERSIONS_TOKEN", "") or os.getenv("META_ACCESS_TOKEN", "")
API_VERSION = os.getenv("META_API_VERSION", "v19.0")
BASE_URL = os.getenv("META_BASE_URL", "https://graph.facebook.com")

# Mapeo de etapas del pipeline a eventos de Meta
ETAPA_EVENT_MAP = {
    "Negociación":    "Lead",
    "Cotización":     "InitiateCheckout",
    "Demo":           "Schedule",
    "Cerrado Ganado": "Purchase",
}


def _hash_sha256(value: str) -> Optional[str]:
    """Hash SHA-256 para datos PII (requerido por Meta CAPI)."""
    if not value:
        return None
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def _normalize_phone(phone: str) -> Optional[str]:
    """Normaliza teléfono a formato E.164 y hashea."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        digits = "52" + digits  # México default
    return _hash_sha256(digits)


def send_conversion_event(lead, event_name: str, value: float = 0.0,
                          currency: str = "MXN", test_event_code: str = ""):
    """
    Envía un evento de conversión a Meta vía Conversions API.

    Args:
        lead: objeto Lead del CRM con campos como telefono, correo, nombre, etc.
        event_name: nombre del evento Meta (Lead, Purchase, etc.)
        value: valor monetario del evento
        currency: moneda (default MXN)
        test_event_code: código de prueba de Meta (para debug en Events Manager)
    """
    if not PIXEL_ID or not CONVERSIONS_TOKEN:
        log.warning("Meta CAPI no configurado (falta META_PIXEL_ID o token)")
        return None

    user_data = {}
    if hasattr(lead, "correo") and lead.correo:
        user_data["em"] = [_hash_sha256(lead.correo)]
    if hasattr(lead, "telefono") and lead.telefono:
        user_data["ph"] = [_normalize_phone(lead.telefono)]
    if hasattr(lead, "nombre") and lead.nombre:
        parts = lead.nombre.strip().split()
        if parts:
            user_data["fn"] = [_hash_sha256(parts[0])]
            if len(parts) > 1:
                user_data["ln"] = [_hash_sha256(" ".join(parts[1:]))]
    user_data["country"] = [_hash_sha256("mx")]

    # External ID para deduplicación
    if hasattr(lead, "id"):
        user_data["external_id"] = [_hash_sha256(str(lead.id))]

    # Lead ID de Meta para attribution
    lead_id = None
    if hasattr(lead, "meta_lead_id") and lead.meta_lead_id:
        lead_id = lead.meta_lead_id

    event_data = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": "system_generated",
        "user_data": user_data,
    }

    if value > 0:
        event_data["custom_data"] = {
            "value": value,
            "currency": currency,
        }

    if lead_id:
        event_data["custom_data"] = event_data.get("custom_data", {})
        event_data["custom_data"]["lead_id"] = lead_id

    # Agregar info de campaña si está disponible
    if hasattr(lead, "meta_campaign") and lead.meta_campaign:
        event_data["custom_data"] = event_data.get("custom_data", {})
        event_data["custom_data"]["campaign"] = lead.meta_campaign

    payload = {
        "data": [event_data],
        "access_token": CONVERSIONS_TOKEN,
    }
    if test_event_code:
        payload["test_event_code"] = test_event_code

    url = f"{BASE_URL}/{API_VERSION}/{PIXEL_ID}/events"

    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if resp.ok:
            log.info(f"Meta CAPI ok: {event_name} para lead {getattr(lead, 'id', '?')} → {result}")
        else:
            log.error(f"Meta CAPI error: {resp.status_code} → {result}")
        return result
    except Exception as e:
        log.exception(f"Meta CAPI exception: {e}")
        return None


def send_pipeline_event(lead, nueva_etapa: str, valor_estimado: float = 0.0):
    """
    Wrapper que mapea etapa del pipeline a evento Meta y lo envía.
    Llamar desde mover_lead() cuando cambia la etapa.
    """
    event_name = ETAPA_EVENT_MAP.get(nueva_etapa)
    if not event_name:
        return None

    value = valor_estimado or getattr(lead, "valor_estimado", 0) or 0
    return send_conversion_event(lead, event_name, value=float(value))
