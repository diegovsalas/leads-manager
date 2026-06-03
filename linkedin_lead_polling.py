"""
LinkedIn Lead Gen Forms Polling — consulta leads vía LinkedIn Marketing API.

Configuración via env vars:
  LINKEDIN_CLIENT_ID       — Client ID de la App
  LINKEDIN_CLIENT_SECRET   — Client Secret de la App
  LINKEDIN_ACCESS_TOKEN    — OAuth2 access token (long-lived)
  LINKEDIN_AD_ACCOUNTS     — Ad Account IDs separados por coma (ej. "540980140,540810322,508602205")
"""
import logging
import os
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("linkedin_lead_polling")

CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "")
ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
AD_ACCOUNTS = [a.strip() for a in os.getenv("LINKEDIN_AD_ACCOUNTS", "").split(",") if a.strip()]

BASE_URL = "https://api.linkedin.com/rest"

# Mapeo de Ad Account ID → marca
ACCOUNT_MARCA = {
    "540980140": "Weldex",
    "540810322": "Aromatex",
    "508602205": "Pestex",
}


def _headers():
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "LinkedIn-Version": "202405",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _get_lead_forms(account_id):
    """Obtiene los Lead Gen Forms de una Ad Account."""
    resp = requests.get(
        f"{BASE_URL}/leadForms",
        headers=_headers(),
        params={"q": "account", "account": f"urn:li:sponsoredAccount:{account_id}"},
        timeout=15,
    )
    if not resp.ok:
        log.error(f"LinkedIn forms error ({account_id}): {resp.status_code} {resp.text[:200]}")
        return []
    data = resp.json()
    return data.get("elements", [])


def _get_leads(form_id):
    """Obtiene leads de un formulario."""
    resp = requests.get(
        f"{BASE_URL}/leadFormResponses",
        headers=_headers(),
        params={"q": "form", "form": form_id, "count": 100},
        timeout=15,
    )
    if not resp.ok:
        log.error(f"LinkedIn leads error ({form_id}): {resp.status_code} {resp.text[:200]}")
        return []
    data = resp.json()
    return data.get("elements", [])


def _extract_fields(lead_data):
    """Extrae campos del lead response de LinkedIn."""
    campos = {}
    for answer in lead_data.get("answers", []):
        question_id = answer.get("questionId", "")
        value = answer.get("answerDetails", {}).get("textQuestionAnswer", {}).get("answer", "")
        if not value:
            value = answer.get("answerDetails", {}).get("singleChoiceAnswer", {}).get("selectedChoice", "")

        q_lower = question_id.lower()
        if "firstname" in q_lower or "first_name" in q_lower:
            campos["first_name"] = value
        elif "lastname" in q_lower or "last_name" in q_lower:
            campos["last_name"] = value
        elif "email" in q_lower:
            campos["email"] = value
        elif "phone" in q_lower or "tel" in q_lower:
            campos["phone"] = value
        elif "company" in q_lower or "empresa" in q_lower:
            campos["company"] = value
        elif "title" in q_lower or "puesto" in q_lower or "job" in q_lower:
            campos["job_title"] = value
        elif "city" in q_lower or "ciudad" in q_lower:
            campos["city"] = value
        elif "state" in q_lower or "estado" in q_lower:
            campos["state"] = value
        else:
            campos[question_id] = value

    return campos


def poll_and_create_leads():
    """Consulta LinkedIn por nuevos leads y los crea en el CRM."""
    from extensions import db
    from models import Lead

    if not ACCESS_TOKEN or not AD_ACCOUNTS:
        return {"error": "LINKEDIN_ACCESS_TOKEN o LINKEDIN_AD_ACCOUNTS no configurados"}

    stats = {"accounts": 0, "forms": 0, "leads_found": 0, "leads_created": 0, "duplicates": 0, "errors": 0}

    for account_id in AD_ACCOUNTS:
        stats["accounts"] += 1
        marca = ACCOUNT_MARCA.get(account_id, "")

        forms = _get_lead_forms(account_id)
        for form in forms:
            stats["forms"] += 1
            form_urn = form.get("id", "")

            leads = _get_leads(form_urn)
            for lead_data in leads:
                stats["leads_found"] += 1
                linkedin_lead_id = lead_data.get("id", "")

                if Lead.query.filter_by(meta_lead_id=f"li-{linkedin_lead_id}").first():
                    stats["duplicates"] += 1
                    continue

                try:
                    _create_lead(lead_data, linkedin_lead_id, form_urn, marca)
                    stats["leads_created"] += 1
                except Exception as e:
                    db.session.rollback()
                    log.exception(f"Error creando lead LinkedIn {linkedin_lead_id}: {e}")
                    stats["errors"] += 1
                    stats.setdefault("error_details", []).append({"lead_id": linkedin_lead_id, "error": str(e)})

    return stats


def _create_lead(lead_data, linkedin_lead_id, form_urn, marca):
    """Crea un lead en el CRM desde datos de LinkedIn."""
    from extensions import db, socketio
    from models import Lead, EtapaPipeline, OrigenLead
    from asignacion import asignar_lead_comercial

    campos = _extract_fields(lead_data)

    first_name = campos.get("first_name", "")
    last_name = campos.get("last_name", "")
    nombre = f"{first_name} {last_name}".strip() or "Sin nombre"
    telefono = campos.get("phone", "")
    empresa = campos.get("company", "")
    estado = campos.get("state", "")

    if not telefono:
        telefono = f"li-{linkedin_lead_id[-10:]}"
        log.warning(f"Lead LinkedIn {linkedin_lead_id} sin teléfono, placeholder: {telefono}")
    telefono = str(telefono).strip()[:30]

    # Campos extra como notas
    campos_mapeados = {"first_name", "last_name", "phone", "email", "company", "state", "city"}
    extras = {k: v for k, v in campos.items() if k not in campos_mapeados and v}
    notas = ""
    if extras:
        notas = " | ".join(f"{k}: {v}" for k, v in extras.items())

    try:
        nuevo_lead = asignar_lead_comercial({
            "telefono": telefono,
            "nombre": nombre,
            "origen": "Web",
            "marca_interes": marca,
            "empresa_nombre": empresa[:200] if empresa else None,
            "estado_cliente": estado[:100] if estado else None,
            "motivo_perdida": f"[LinkedIn] {notas}" if notas else None,
            "meta_lead_id": f"li-{linkedin_lead_id}",
            "meta_form_id": form_urn,
            "meta_campaign": "LinkedIn Ads",
        })
        log.info(f"Lead LinkedIn creado: {nuevo_lead.id} ({nombre})")
    except ValueError:
        nuevo_lead = Lead(
            telefono=telefono,
            nombre=nombre,
            origen=OrigenLead.WEB,
            marca_interes=marca,
            empresa_nombre=empresa[:200] if empresa else None,
            estado_cliente=estado[:100] if estado else None,
            motivo_perdida=f"[LinkedIn] {notas}" if notas else None,
            etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
            meta_lead_id=f"li-{linkedin_lead_id}",
            meta_form_id=form_urn,
            meta_campaign="LinkedIn Ads",
        )
        db.session.add(nuevo_lead)
        db.session.commit()
        log.warning(f"Lead LinkedIn creado SIN asignar: {nuevo_lead.id}")

    socketio.emit("nuevo_lead", nuevo_lead.to_dict())
    return nuevo_lead
