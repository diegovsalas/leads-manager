"""
Meta Conversions API — envía eventos server-side a Meta para optimización de campañas.

Eventos mapeados desde el pipeline del CRM:
  Negociación    → Lead        (lead calificado)
  Cotización     → InitiateCheckout (propuesta enviada)
  Demo           → Schedule    (demo agendada)
  Cierre Ganado  → Purchase    (venta cerrada)

Multi-pixel por unidad de negocio:
  META_PIXEL_AROMATEX / META_TOKEN_AROMATEX  — Aromatex B2B/B2C
  META_PIXEL_WELDU    / META_TOKEN_WELDU     — Weldu
"""
import hashlib
import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger("meta_conversions")

API_VERSION = os.getenv("META_API_VERSION", "v19.0")
BASE_URL = os.getenv("META_BASE_URL", "https://graph.facebook.com")

# Configuración por unidad de negocio
PIXELS = {
    "aromatex": {
        "pixel_id": os.getenv("META_PIXEL_AROMATEX", ""),
        "token": os.getenv("META_TOKEN_AROMATEX", ""),
    },
    "weldu": {
        "pixel_id": os.getenv("META_PIXEL_WELDU", ""),
        "token": os.getenv("META_TOKEN_WELDU", ""),
    },
}

# Aliases: distintas formas en que la marca aparece en el lead → key de PIXELS
MARCA_ALIASES = {
    "aromatex": "aromatex",
    "aromatex b2b": "aromatex",
    "aromatex b2c": "aromatex",
    "pestex": "aromatex",       # Pestex usa el mismo pixel de Aromatex (Grupo Avantex)
    "weldu": "weldu",
    "weldex": "weldu",
    "nexo": "weldu",
}

# Mapeo de etapas del pipeline a eventos de Meta
ETAPA_EVENT_MAP = {
    "Negociación":    "Lead",
    "Cotización":     "InitiateCheckout",
    "Demo":           "Schedule",
    "Cerrado Ganado": "Purchase",
}


def _resolve_pixel(marca: str) -> Optional[dict]:
    """Resuelve pixel_id y token según la marca del lead."""
    if not marca:
        return None
    key = MARCA_ALIASES.get(marca.strip().lower())
    if not key:
        return None
    cfg = PIXELS.get(key)
    if cfg and cfg["pixel_id"] and cfg["token"]:
        return cfg
    return None


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


def _build_user_data(lead) -> dict:
    """Construye user_data hasheado desde el lead."""
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
    if hasattr(lead, "id"):
        user_data["external_id"] = [_hash_sha256(str(lead.id))]
    return user_data


def send_conversion_event(lead, event_name: str, pixel_cfg: dict,
                          value: float = 0.0, currency: str = "MXN",
                          test_event_code: str = ""):
    """
    Envía un evento de conversión a Meta vía Conversions API.
    """
    pixel_id = pixel_cfg["pixel_id"]
    token = pixel_cfg["token"]

    user_data = _build_user_data(lead)
    custom_data = {}

    if value > 0:
        custom_data["value"] = value
        custom_data["currency"] = currency

    if hasattr(lead, "meta_lead_id") and lead.meta_lead_id:
        custom_data["lead_id"] = lead.meta_lead_id
    if hasattr(lead, "meta_campaign") and lead.meta_campaign:
        custom_data["campaign"] = lead.meta_campaign
    if hasattr(lead, "marca_interes") and lead.marca_interes:
        custom_data["unidad_negocio"] = lead.marca_interes

    event_data = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "action_source": "system_generated",
        "user_data": user_data,
    }
    if custom_data:
        event_data["custom_data"] = custom_data

    payload = {
        "data": [event_data],
        "access_token": token,
    }
    if test_event_code:
        payload["test_event_code"] = test_event_code

    url = f"{BASE_URL}/{API_VERSION}/{pixel_id}/events"

    try:
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if resp.ok:
            log.info(f"Meta CAPI [{pixel_id}] ok: {event_name} lead={getattr(lead, 'id', '?')} → {result}")
        else:
            log.error(f"Meta CAPI [{pixel_id}] error: {resp.status_code} → {result}")
        return result
    except Exception as e:
        log.exception(f"Meta CAPI [{pixel_id}] exception: {e}")
        return None


def send_pipeline_event(lead, nueva_etapa: str, valor_estimado: float = 0.0):
    """
    Wrapper que mapea etapa del pipeline a evento Meta y lo envía
    al pixel correcto según la marca del lead.
    """
    event_name = ETAPA_EVENT_MAP.get(nueva_etapa)
    if not event_name:
        return None

    marca = getattr(lead, "marca_interes", "") or ""
    pixel_cfg = _resolve_pixel(marca)
    if not pixel_cfg:
        log.info(f"Meta CAPI skip: marca '{marca}' sin pixel configurado (lead {getattr(lead, 'id', '?')})")
        return None

    value = valor_estimado or getattr(lead, "valor_estimado", 0) or 0
    return send_conversion_event(lead, event_name, pixel_cfg, value=float(value))


def get_pixel_ids() -> dict:
    """Retorna pixel IDs configurados (para inyectar en templates)."""
    return {k: v["pixel_id"] for k, v in PIXELS.items() if v["pixel_id"]}
