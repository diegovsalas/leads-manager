"""
LinkedIn Lead Gen Forms Polling — consulta leads vía LinkedIn Marketing API.

Configuración via env vars:
  LINKEDIN_ACCESS_TOKEN    — OAuth2 access token con scope r_marketing_leadgen_automation
  LINKEDIN_AD_ACCOUNTS     — Ad Account IDs separados por coma (ej. "540980140,540810322,508602205")
"""
import logging
import os
from urllib.parse import quote

import requests

log = logging.getLogger("linkedin_lead_polling")

ACCESS_TOKEN = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
AD_ACCOUNTS = [a.strip() for a in os.getenv("LINKEDIN_AD_ACCOUNTS", "").split(",") if a.strip()]

BASE_URL = "https://api.linkedin.com/rest"

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
    """Obtiene los Lead Gen Forms de una Ad Account via sponsoredAccount owner."""
    owner_urn = quote(f"urn:li:sponsoredAccount:{account_id}")
    url = f"{BASE_URL}/leadForms?q=owner&owner=(sponsoredAccount:{owner_urn})&count=50"
    resp = requests.get(url, headers=_headers(), timeout=15)
    if not resp.ok:
        log.error(f"LinkedIn forms error ({account_id}): {resp.status_code} {resp.text[:300]}")
        return []
    data = resp.json()
    return data.get("elements", [])


def _get_leads(account_id):
    """Obtiene TODOS los lead form responses de una Ad Account."""
    owner_urn = quote(f"urn:li:sponsoredAccount:{account_id}")
    url = (
        f"{BASE_URL}/leadFormResponses"
        f"?q=owner"
        f"&owner=(sponsoredAccount:{owner_urn})"
        f"&leadType=(leadType:SPONSORED)"
        f"&limitedToTestLeads=false"
        f"&count=100"
    )
    resp = requests.get(url, headers=_headers(), timeout=15)
    if not resp.ok:
        log.error(f"LinkedIn leads error ({account_id}): {resp.status_code} {resp.text[:300]}")
        return []
    data = resp.json()
    return data.get("elements", [])


def _get_form_questions(forms):
    """Crea un mapa de questionId → predefinedField/name para mapear respuestas."""
    question_map = {}
    for form in forms:
        questions = form.get("content", {}).get("questions", [])
        for q in questions:
            qid = str(q.get("questionId", ""))
            predefined = q.get("predefinedField", "")
            name = q.get("name", "")
            question_map[qid] = predefined or name
    return question_map


def _extract_fields(lead_data, question_map):
    """Extrae campos del lead response de LinkedIn usando el question_map."""
    campos = {}
    answers = lead_data.get("formResponse", {}).get("answers", [])

    for answer in answers:
        question_id = str(answer.get("questionId", ""))
        details = answer.get("answerDetails", {})

        value = ""
        if "textQuestionAnswer" in details:
            value = details["textQuestionAnswer"].get("answer", "")
        elif "multipleChoiceAnswer" in details:
            options = details["multipleChoiceAnswer"].get("options", [])
            value = ", ".join(str(o) for o in options)
        elif "singleChoiceAnswer" in details:
            value = str(details["singleChoiceAnswer"].get("selectedChoice", ""))

        if value:
            field_name = question_map.get(question_id, question_id)
            campos[field_name] = value

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
        stats["forms"] += len(forms)
        question_map = _get_form_questions(forms)

        leads = _get_leads(account_id)
        for lead_data in leads:
            stats["leads_found"] += 1
            linkedin_lead_id = lead_data.get("id", "")

            if Lead.query.filter_by(meta_lead_id=f"li-{linkedin_lead_id}").first():
                stats["duplicates"] += 1
                continue

            try:
                _create_lead(lead_data, linkedin_lead_id, marca, question_map)
                stats["leads_created"] += 1
            except Exception as e:
                db.session.rollback()
                log.exception(f"Error creando lead LinkedIn {linkedin_lead_id}: {e}")
                stats["errors"] += 1
                stats.setdefault("error_details", []).append({"lead_id": linkedin_lead_id, "error": str(e)})

    return stats


def _create_lead(lead_data, linkedin_lead_id, marca, question_map):
    """Crea un lead en el CRM desde datos de LinkedIn."""
    from extensions import db, socketio
    from models import Lead, EtapaPipeline, OrigenLead
    from asignacion import asignar_lead_comercial

    campos = _extract_fields(lead_data, question_map)

    first_name = campos.get("FIRST_NAME", "") or campos.get("firstName", "")
    last_name = campos.get("LAST_NAME", "") or campos.get("lastName", "")
    nombre = f"{first_name} {last_name}".strip() or "Sin nombre"
    telefono = campos.get("PHONE_NUMBER", "") or campos.get("phone", "") or campos.get("phoneNumber", "")
    empresa = campos.get("COMPANY_NAME", "") or campos.get("company", "") or campos.get("companyName", "")
    estado = campos.get("STATE", "") or campos.get("state", "")
    job_title = campos.get("JOB_TITLE", "") or campos.get("jobTitle", "")

    if not telefono:
        telefono = f"li-{linkedin_lead_id[-10:]}"
        log.warning(f"Lead LinkedIn {linkedin_lead_id} sin teléfono, placeholder: {telefono}")
    telefono = str(telefono).strip()[:30]

    # Campos extra como notas
    campos_mapeados = {"FIRST_NAME", "LAST_NAME", "firstName", "lastName",
                       "PHONE_NUMBER", "phone", "phoneNumber",
                       "EMAIL", "email", "COMPANY_NAME", "company", "companyName",
                       "STATE", "state", "CITY", "city",
                       "JOB_TITLE", "jobTitle"}
    extras = {k: v for k, v in campos.items() if k not in campos_mapeados and v}
    notas_parts = []
    if job_title:
        notas_parts.append(f"Puesto: {job_title}")
    if extras:
        notas_parts.extend(f"{k}: {v}" for k, v in extras.items())
    notas = " | ".join(notas_parts) if notas_parts else None

    campaign_name = ""
    meta_info = lead_data.get("leadMetadataInfo", {}).get("sponsoredLeadMetadataInfo", {})
    if meta_info:
        campaign_name = meta_info.get("campaign", {}).get("name", "")

    try:
        nuevo_lead = asignar_lead_comercial({
            "telefono": telefono,
            "nombre": nombre,
            "origen": "Web",
            "marca_interes": marca,
            "empresa_nombre": empresa[:200] if empresa else None,
            "estado_cliente": estado[:100] if estado else None,
            "notas": f"[LinkedIn] {notas}" if notas else None,
            "meta_lead_id": f"li-{linkedin_lead_id}",
            "meta_campaign": f"LinkedIn: {campaign_name}" if campaign_name else "LinkedIn Ads",
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
            notas=f"[LinkedIn] {notas}" if notas else None,
            etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
            meta_lead_id=f"li-{linkedin_lead_id}",
            meta_campaign=f"LinkedIn: {campaign_name}" if campaign_name else "LinkedIn Ads",
        )
        db.session.add(nuevo_lead)
        db.session.commit()
        log.warning(f"Lead LinkedIn creado SIN asignar: {nuevo_lead.id}")

    socketio.emit("nuevo_lead", nuevo_lead.to_dict())
    return nuevo_lead
