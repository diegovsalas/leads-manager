"""
Meta Lead Ads Polling — consulta leads vía Graph API sin depender del webhook.

Alternativa al webhook para Apps en Development mode.
Usa META_PAGE_TOKEN (system user token) para obtener un Page token
y consultar los formularios de lead ads.

Configuración:
  META_PAGE_TOKEN   — System user token con permisos de Page
  META_PAGE_IDS     — Page IDs separados por coma (ej. "153702277822462,123456789")
  META_API_VERSION  — (default v19.0)
"""
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger("meta_lead_polling")

API_VERSION = os.getenv("META_API_VERSION", "v19.0")
BASE_URL = f"https://graph.facebook.com/{API_VERSION}"
SYSTEM_TOKEN = os.getenv("META_PAGE_TOKEN", "")
PAGE_IDS = [p.strip() for p in os.getenv("META_PAGE_IDS", "153702277822462").split(",") if p.strip()]


def _get_page_token(page_id):
    """Intercambia system user token por page access token."""
    resp = requests.get(f"{BASE_URL}/{page_id}", params={
        "fields": "access_token,name",
        "access_token": SYSTEM_TOKEN,
    }, timeout=15)
    data = resp.json()
    if "access_token" not in data:
        log.error(f"No se pudo obtener Page token para {page_id}: {data}")
        return None, None
    return data["access_token"], data.get("name", "")


def _get_forms(page_id, page_token):
    """Obtiene los formularios de lead ads de una Page."""
    resp = requests.get(f"{BASE_URL}/{page_id}/leadgen_forms", params={
        "access_token": page_token,
        "fields": "id,name,status",
        "limit": 50,
    }, timeout=15)
    data = resp.json()
    return data.get("data", [])


def _get_leads(form_id, page_token, since=None):
    """Obtiene leads de un formulario. Filtra por fecha si se proporciona."""
    params = {
        "access_token": page_token,
        "fields": "id,created_time,field_data,ad_id,form_id,campaign_id",
        "limit": 100,
    }
    if since:
        params["filtering"] = f'[{{"field":"time_created","operator":"GREATER_THAN","value":"{since}"}}]'

    resp = requests.get(f"{BASE_URL}/{form_id}/leads", params=params, timeout=15)
    data = resp.json()
    return data.get("data", [])


def poll_and_create_leads():
    """
    Consulta Meta por nuevos leads y los crea en el CRM.
    Retorna dict con estadísticas.
    """
    from extensions import db
    from models import Lead

    if not SYSTEM_TOKEN:
        return {"error": "META_PAGE_TOKEN no configurado"}

    stats = {"pages": 0, "forms": 0, "leads_found": 0, "leads_created": 0, "duplicates": 0, "errors": 0}

    for page_id in PAGE_IDS:
        page_token, page_name = _get_page_token(page_id)
        if not page_token:
            stats["errors"] += 1
            continue

        stats["pages"] += 1
        forms = _get_forms(page_id, page_token)

        for form in forms:
            stats["forms"] += 1
            leads = _get_leads(form["id"], page_token)

            from sqlalchemy import text as _sa_text

            for lead_data in leads:
                stats["leads_found"] += 1
                meta_lead_id = lead_data.get("id")

                if Lead.query.filter_by(meta_lead_id=meta_lead_id).first():
                    stats["duplicates"] += 1
                    continue

                # BUGFIX 24-jun-2026: si el lead fue borrado manualmente antes,
                # NO lo recreamos. Antes el polling cada 5 min resurrecía los
                # leads que el vendedor acababa de borrar.
                dismissed = db.session.execute(
                    _sa_text("SELECT 1 FROM meta_leads_dismissed WHERE meta_lead_id = :mid"),
                    {"mid": meta_lead_id},
                ).fetchone()
                if dismissed:
                    stats.setdefault("dismissed", 0)
                    stats["dismissed"] += 1
                    continue

                try:
                    _create_lead_from_api(lead_data, page_name)
                    stats["leads_created"] += 1
                except Exception as e:
                    db.session.rollback()
                    log.exception(f"Error creando lead {meta_lead_id}: {e}")
                    stats["errors"] += 1
                    stats.setdefault("error_details", []).append({"lead_id": meta_lead_id, "error": str(e)})

    return stats


def _create_lead_from_api(lead_data, page_name=""):
    """Crea un lead en el CRM desde datos de la Graph API."""
    from extensions import db, socketio
    from models import Lead, EtapaPipeline, OrigenLead
    from asignacion import asignar_lead_comercial
    import meta_campaign_registry

    meta_lead_id = lead_data.get("id")
    form_id = lead_data.get("form_id")
    ad_id = lead_data.get("ad_id")
    campaign_id = lead_data.get("campaign_id")

    campos = {}
    for item in lead_data.get("field_data", []):
        if item.get("values"):
            campos[item["name"]] = item["values"][0]

    nombre = campos.get("full_name") or campos.get("nombre_completo") or campos.get("nombre", "Sin nombre")
    whatsapp = campos.get("número_de_whatsapp") or campos.get("numero_de_whatsapp") or campos.get("whatsapp") or ""
    telefono = campos.get("phone_number") or campos.get("telefono") or campos.get("tel") or whatsapp
    email = campos.get("email") or campos.get("correo")
    marca = campos.get("marca_interes") or campos.get("brand", "")

    # Mapear campos conocidos del formulario a campos del Lead
    empresa = campos.get("company_name") or campos.get("empresa") or ""
    estado = campos.get("state") or campos.get("estado") or ""
    ciudad = campos.get("city") or campos.get("ciudad") or ""

    # Limpiar teléfono: quitar dummy data de Meta testing y truncar a 30 chars
    if not telefono or "dummy" in str(telefono).lower() or "test lead" in str(telefono).lower():
        telefono = f"meta-{meta_lead_id[-10:]}"
        log.warning(f"Lead {meta_lead_id} sin teléfono válido, usando placeholder: {telefono}")
    telefono = str(telefono).strip()[:30]

    # Limpiar nombre dummy
    if nombre and "dummy" in nombre.lower():
        nombre = f"Lead Meta {meta_lead_id[-6:]}"

    # Campos extra del formulario → notas (información importante del lead)
    campos_mapeados = {"full_name", "nombre", "nombre_completo", "phone_number", "telefono", "tel",
                       "email", "correo", "marca_interes", "brand", "company_name", "empresa",
                       "state", "estado", "city", "ciudad", "número_de_whatsapp",
                       "numero_de_whatsapp", "whatsapp"}
    extras = {k: v for k, v in campos.items() if k not in campos_mapeados and v}
    notas_formulario = ""
    if extras:
        notas_formulario = " | ".join(f"{k}: {v}" for k, v in extras.items())

    # Payload base
    datos = {
        "telefono": telefono,
        "nombre": nombre,
        "origen": OrigenLead.META_ADS.value,
        "marca_interes": marca,
        "empresa_nombre": empresa[:200] if empresa else None,
        "estado_cliente": estado[:100] if estado else None,
        "notas": notas_formulario or None,
        "meta_lead_id": meta_lead_id,
        "meta_form_id": form_id,
        "meta_ad_id": ad_id,
        "meta_campaign": campaign_id,
    }

    # Enriquecer con el registry de campañas (override marca, default estado,
    # tag de unidad para reporting). No-op si la campaña no está registrada.
    meta_campaign_registry.aplicar_a_lead(datos, campaign_id)
    if datos.get("meta_campaign_nombre"):
        log.info(f"Lead {meta_lead_id} matcheó campaña registrada: {datos['meta_campaign_nombre']}")

    try:
        nuevo_lead = asignar_lead_comercial(datos)
        log.info(f"Lead polling creado: {nuevo_lead.id} ({nombre})")
    except ValueError:
        nuevo_lead = Lead(
            telefono=datos["telefono"],
            nombre=datos["nombre"],
            origen=OrigenLead.META_ADS,
            marca_interes=datos.get("marca_interes") or "",
            empresa_nombre=datos.get("empresa_nombre"),
            estado_cliente=datos.get("estado_cliente"),
            notas=datos.get("notas"),
            etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
            meta_lead_id=datos.get("meta_lead_id"),
            meta_form_id=datos.get("meta_form_id"),
            meta_ad_id=datos.get("meta_ad_id"),
            meta_campaign=datos.get("meta_campaign"),
        )
        db.session.add(nuevo_lead)
        db.session.commit()
        log.warning(f"Lead polling creado SIN asignar: {nuevo_lead.id}")

    socketio.emit("nuevo_lead", nuevo_lead.to_dict())
    return nuevo_lead
