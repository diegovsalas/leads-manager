"""
SDR Directivo — biblioteca de integraciones (Apollo + Lusha + Lemlist) y
templates de mensajes para outreach a directivos.

Port de vendedores.cloud/sdr-directivo.js. NO contiene routes — los routes
viven en blueprints/sdr_directivo.py. NO contiene engine logic — eso vive
en sdr_directivo_engine.py (Round 2d).

Env vars:
  APOLLO_API_KEY
  LUSHA_API_KEY
  LEMLIST_API_KEY
  DOMAIN (para registrar webhooks Lemlist con la URL pública)
"""

import base64
import logging
import os
import re
from typing import Any, Optional

import requests

log = logging.getLogger("sdr_directivo")

APOLLO_KEY = os.getenv("APOLLO_API_KEY", "")
LUSHA_KEY = os.getenv("LUSHA_API_KEY", "")
LEMLIST_KEY = os.getenv("LEMLIST_API_KEY", "")
DOMAIN = os.getenv("DOMAIN", "https://leads-manager-avantex.onrender.com")

LEMLIST_AUTH = (
    "Basic " + base64.b64encode(f":{LEMLIST_KEY}".encode()).decode()
    if LEMLIST_KEY else ""
)


# ── Senders por unidad ─────────────────────────────────────────────


SENDERS = {
    "aromatex": {"email": "alejandrogil@aromatex.mx", "name": "Alejandro Gil", "director_name": "Alejandro Gil"},
    "pestex":   {"email": "alanaziz@pestex.mx",       "name": "Alan Aziz",     "director_name": "Alan Aziz"},
    "weldex":   {"email": None,                       "name": "Andres Zambrano", "director_name": "Andres Zambrano"},
}

# Industry tags Apollo por unidad
INDUSTRY_FILTERS = {
    "aromatex": ["retail", "hospitality", "healthcare", "automotive", "real estate", "entertainment",
                 "food & beverage", "beauty & wellness", "food and beverages", "leisure, travel & tourism",
                 "consumer goods"],
    "pestex":   ["food production", "food manufacturing", "hospitality", "logistics", "warehousing",
                 "healthcare", "education", "food & beverage", "real estate", "food and beverages",
                 "hospital & health care", "logistics and supply chain"],
    "weldex":   ["financial services", "banking", "insurance", "information technology", "real estate",
                 "education", "hospital & health care", "manufacturing", "oil & energy", "telecommunications"],
}

# Departamentos Apollo (combinados con seniorities)
DEPARTMENTS = {
    "aromatex": ["marketing", "operations", "business_development", "sales"],
    "pestex":   ["operations", "engineering", "finance", "human_resources", "support"],
    "weldex":   ["operations", "finance", "human_resources", "support"],
}

# Cargos prioritarios por unidad (informativo / UI)
TITLES = {
    "aromatex": ["Director de Marketing", "Gerente de Marketing", "Brand Manager", "CMO",
                 "Director Comercial", "Marketing Manager", "Trade Marketing"],
    "pestex":   ["Director EHS", "Gerente de Seguridad", "HSE Manager", "Director de Mantenimiento",
                 "Facilities Manager", "Director de Compras", "Procurement Manager", "Plant Manager"],
    "weldex":   ["Director de Administracion", "Director de Facilities", "Facilities Manager",
                 "Office Manager", "HR Manager", "COO", "Building Manager", "Property Manager"],
}

# Step day offsets (8 pasos: 0,2,4,7,10,14,18,21)
STEP_DAYS = [0, 2, 4, 7, 10, 14, 18, 21]


# ── Step definitions ───────────────────────────────────────────────


_STEP_DEFS_EMAIL_FIRST = (
    {"channel": "email",    "type": "manual", "label": "Email personalizado"},
    {"channel": "email",    "type": "auto",   "label": "Follow-up email"},
    {"channel": "linkedin", "type": "semi",   "label": "Conexion LinkedIn"},
    {"channel": "email",    "type": "auto",   "label": "Email caso de exito"},
    {"channel": "linkedin", "type": "semi",   "label": "Mensaje LinkedIn"},
    {"channel": "email",    "type": "auto",   "label": "Email propuesta diferente"},
    {"channel": "whatsapp", "type": "semi",   "label": "WhatsApp"},
    {"channel": "email",    "type": "auto",   "label": "Breakup email"},
)

_STEP_DEFS_WHATSAPP_FIRST = (
    {"channel": "whatsapp", "type": "manual", "label": "WhatsApp personalizado"},
    {"channel": "email",    "type": "auto",   "label": "Email de presentacion"},
    {"channel": "linkedin", "type": "semi",   "label": "Conexion LinkedIn"},
    {"channel": "email",    "type": "auto",   "label": "Email caso de exito"},
    {"channel": "linkedin", "type": "semi",   "label": "Mensaje LinkedIn"},
    {"channel": "email",    "type": "auto",   "label": "Email propuesta diferente"},
    {"channel": "whatsapp", "type": "semi",   "label": "Follow-up WhatsApp"},
    {"channel": "email",    "type": "auto",   "label": "Breakup email"},
)


def get_step_def(first_channel: str, step_idx: int) -> dict:
    defs = _STEP_DEFS_WHATSAPP_FIRST if first_channel == "whatsapp" else _STEP_DEFS_EMAIL_FIRST
    if 0 <= step_idx < len(defs):
        return dict(defs[step_idx])
    return dict(defs[0])


# ── Templates de mensajes (por unidad) ─────────────────────────────


def _t_aromatex(key: str, n: str, c: str) -> str:
    return {
        "email_pres":    f"Hola {n}, soy Alejandro Gil, Director de Aromatex en Grupo Avantex. Ayudamos a empresas como {c} a crear experiencias memorables con marketing olfativo. Marcas como Cinepolis, Coppel y TEC de Monterrey ya confian en nosotros. Me gustaria platicarte como podriamos diferenciar a {c}. Tienes 10 minutos esta semana?",
        "followup":      f"Hola {n}, te escribi hace unos dias sobre marketing olfativo para {c}. Te comparto un dato: los clientes permanecen hasta 20% mas tiempo en espacios aromatizados. Tienes un momento para platicarlo?",
        "linkedin_note": f"Hola {n}, te envie un email sobre una propuesta de marketing olfativo para {c}. Me gustaria conectar.",
        "case_study":    f"Hola {n}, te comparto como ayudamos a una cadena de retail similar a {c} a incrementar permanencia de clientes 20% con una identidad olfativa personalizada. Adjunto el caso. Tienes 10 min para una llamada?",
        "linkedin_msg":  f"Hola {n}, pudiste revisar el caso de exito que te envie? Me encantaria platicarte como aplicaria en {c}.",
        "diff_prop":     f"Hola {n}, tal vez el branding olfativo no era lo que buscabas para {c}. Tambien ofrecemos aromatizacion para sanitizacion de espacios y control de olores en areas operativas. Una llamada rapida de 10 min?",
        "whatsapp":      f"Hola {n}, soy Alejandro Gil de Aromatex (Grupo Avantex). Te escribi por email sobre marketing olfativo para {c}. Tienes 5 min esta semana para platicarlo?",
        "breakup":       f"Hola {n}, entiendo que no es el momento. Te dejo mi contacto para cuando {c} necesite servicios de marketing olfativo. Fue un gusto. Alejandro Gil, Aromatex.",
    }.get(key, "")


def _t_pestex(key: str, n: str, c: str) -> str:
    return {
        "email_pres":    f"Hola {n}, soy Alan Aziz, Director de Pestex en Grupo Avantex. Somos empresa certificada en control de plagas y fumigacion profesional. Atendemos a mas de 200 empresas incluyendo Autozone, Elektra y hospitales en todo Mexico. Me gustaria presentarte nuestros servicios para {c}. Tienes 10 minutos?",
        "followup":      f"Hola {n}, te escribi sobre control de plagas profesional para {c}. Cumplimos con todas las normas sanitarias (NOM-256, NOM-017). Tienes un momento para platicarlo?",
        "linkedin_note": f"Hola {n}, te envie un email sobre fumigacion profesional certificada para {c}. Me gustaria conectar.",
        "case_study":    f"Hola {n}, te comparto como ayudamos a una empresa del mismo sector que {c} a mantener certificaciones sanitarias al dia y evitar multas. Tienes 10 min?",
        "linkedin_msg":  f"Hola {n}, pudiste revisar la informacion sobre control de plagas para {c}?",
        "diff_prop":     f"Hola {n}, ademas de fumigacion, ofrecemos monitoreo continuo con reportes digitales y certificados automaticos para auditorias. Esto podria interesarle a {c}?",
        "whatsapp":      f"Hola {n}, soy Alan Aziz de Pestex (Grupo Avantex). Te escribi sobre control de plagas para {c}. Tienes 5 min?",
        "breakup":       f"Hola {n}, entiendo que no es el momento. Quedo a tus ordenes para cuando {c} necesite control de plagas certificado. Alan Aziz, Pestex.",
    }.get(key, "")


def _t_weldex(key: str, n: str, c: str) -> str:
    return {
        "email_pres":    f"Hola {n}, soy Andres Zambrano, Director de Weldex en Grupo Avantex. Ofrecemos servicios de intendencia y limpieza profesional. Trabajamos con corporativos como CBRE, IOS Offices y Gilsa. Me gustaria presentarte como podemos optimizar costos de limpieza en {c}. Tienes 10 minutos?",
        "followup":      f"Hola {n}, te escribi sobre servicios de intendencia profesional para {c}. Nuestros clientes reducen hasta 30% sus costos vs personal propio. Platicamos?",
        "linkedin_note": f"Hola {n}, te envie una propuesta de servicios de limpieza profesional para {c}.",
        "case_study":    f"Hola {n}, te comparto como un corporativo similar a {c} redujo 30% sus costos de limpieza externalizando con nosotros.",
        "linkedin_msg":  f"Hola {n}, pudiste revisar la propuesta de intendencia para {c}?",
        "diff_prop":     f"Hola {n}, ademas de limpieza general, ofrecemos servicios especializados: limpieza de cristales, jardineria, mantenimiento menor. Podria interesarle a {c}?",
        "whatsapp":      f"Hola {n}, soy Andres Zambrano de Weldex (Grupo Avantex). Te escribi sobre intendencia profesional para {c}. Tienes 5 min?",
        "breakup":       f"Hola {n}, entiendo que no es el momento. Quedo a tus ordenes para servicios de intendencia. Andres Zambrano, Weldex.",
    }.get(key, "")


_TPL_FNS = {"aromatex": _t_aromatex, "pestex": _t_pestex, "weldex": _t_weldex}


def get_step_message(unit: str, step_idx: int, first_channel: str, contact_name: str, company_name: str) -> str:
    fn = _TPL_FNS.get(unit) or _t_aromatex
    n = (contact_name or "").split(" ")[0]
    c = company_name or ""
    d = get_step_def(first_channel, step_idx)
    label = (d.get("label") or "").lower()
    if d["channel"] == "email":
        if "presentacion" in label or "personalizado" in label:
            return fn("email_pres", n, c)
        if "follow-up" in label or "followup" in label:
            return fn("followup", n, c)
        if "caso" in label:
            return fn("case_study", n, c)
        if "diferente" in label or "propuesta" in label:
            return fn("diff_prop", n, c)
        if "breakup" in label:
            return fn("breakup", n, c)
        return fn("followup", n, c)
    if d["channel"] == "linkedin":
        if "conexion" in label:
            return fn("linkedin_note", n, c)
        return fn("linkedin_msg", n, c)
    if d["channel"] == "whatsapp":
        return fn("whatsapp", n, c)
    return fn("followup", n, c)


# ── Apollo ─────────────────────────────────────────────────────────


def search_companies(query: str, unit: str = "aromatex") -> list[dict]:
    if not APOLLO_KEY:
        return []
    body: dict[str, Any] = {"organization_locations": ["Mexico"], "page": 1, "per_page": 10}
    if query:
        body["q_organization_name"] = query
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_companies/search",
            headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
            json=body, timeout=15,
        )
        data = resp.json() if resp.status_code < 400 else {}
    except Exception as e:
        log.error(f"Apollo search_companies error: {e}")
        return []
    return [{
        "name": o.get("name") or "", "domain": o.get("primary_domain") or "",
        "industry": o.get("industry") or "", "size": o.get("estimated_num_employees") or 0,
        "website": o.get("website_url") or "", "linkedin": o.get("linkedin_url") or "",
        "logo": o.get("logo_url") or "", "city": o.get("city") or "",
        "state": o.get("state") or "", "country": o.get("country") or "",
    } for o in data.get("organizations") or []]


def suggest_companies(unit: str = "aromatex") -> list[dict]:
    import random
    if not APOLLO_KEY:
        return []
    industries = INDUSTRY_FILTERS.get(unit) or INDUSTRY_FILTERS["aromatex"]
    body = {
        "q_organization_num_employees_ranges": ["51,200", "201,500", "501,1000", "1001,5000", "5001,10000"],
        "organization_locations": ["Mexico"],
        "q_organization_keyword_tags": industries[:5],
        "page": random.randint(1, 10),
        "per_page": 10,
    }
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/mixed_companies/search",
            headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
            json=body, timeout=15,
        )
        data = resp.json() if resp.status_code < 400 else {}
    except Exception as e:
        log.error(f"Apollo suggest_companies error: {e}")
        return []
    return [{
        "name": o.get("name") or "", "domain": o.get("primary_domain") or "",
        "industry": o.get("industry") or "", "size": o.get("estimated_num_employees") or 0,
        "website": o.get("website_url") or "", "logo": o.get("logo_url") or "",
        "country": o.get("country") or "",
    } for o in data.get("organizations") or []]


def search_contacts(domain: str, company_name: str, unit: str = "aromatex") -> list[dict]:
    """Apollo people search + reveal. Devuelve hasta 10 contactos enriquecidos."""
    if not APOLLO_KEY:
        return []
    depts = DEPARTMENTS.get(unit) or DEPARTMENTS["aromatex"]
    body: dict[str, Any] = {
        "person_seniorities": ["director", "vp", "manager", "head", "chief", "senior"],
        "person_departments": depts,
        "page": 1, "per_page": 15,
    }
    if domain:
        body["q_organization_domains"] = domain
    elif company_name:
        body["q_organization_name"] = company_name
    else:
        return []

    def _post(b):
        return requests.post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
            json=b, timeout=15,
        )

    try:
        data = _post(body).json()
        people = data.get("people") or []
        if not people:
            fb = {"person_seniorities": ["director", "vp", "manager"], "page": 1, "per_page": 15}
            if domain:
                fb["q_organization_domains"] = domain
            elif company_name:
                fb["q_organization_name"] = company_name
            data = _post(fb).json()
            people = data.get("people") or []

        revealed = []
        for p in people[:10]:
            pid = p.get("id")
            if not pid:
                continue
            try:
                rev = requests.post(
                    "https://api.apollo.io/api/v1/people/match",
                    headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
                    json={"id": pid, "reveal_personal_emails": True},
                    timeout=15,
                ).json()
                rp = rev.get("person")
                if rp:
                    phones = rp.get("phone_numbers") or []
                    revealed.append({
                        "name": f"{rp.get('first_name') or ''} {rp.get('last_name') or ''}".strip(),
                        "first_name": rp.get("first_name") or "",
                        "last_name": rp.get("last_name") or "",
                        "title": rp.get("title") or "",
                        "email": rp.get("email") or "",
                        "phone": (phones[0].get("sanitized_number") if phones else ""),
                        "linkedin": rp.get("linkedin_url") or "",
                        "city": rp.get("city") or "",
                    })
            except Exception:
                continue
        return revealed
    except Exception as e:
        log.error(f"Apollo search_contacts error: {e}")
        return []


# ── Lusha ──────────────────────────────────────────────────────────


def enrich_phone(first_name: str, last_name: str, company: str) -> Optional[str]:
    """Devuelve un teléfono móvil o None. Versión simple."""
    if not LUSHA_KEY or not first_name:
        return None
    try:
        resp = requests.get(
            "https://api.lusha.com/person",
            params={"firstName": first_name, "lastName": last_name, "company": company},
            headers={"api_key": LUSHA_KEY}, timeout=10,
        )
        data = resp.json() if resp.status_code < 400 else {}
    except Exception:
        return None
    phones = data.get("phoneNumbers") or []
    mobile = next((p for p in phones if p.get("type") == "mobile"), phones[0] if phones else None)
    if not mobile:
        return None
    return mobile.get("internationalNumber") or mobile.get("localizedNumber")


def enrich_lusha(person: dict, want_email: bool = True, want_phone: bool = True) -> dict:
    """Lusha granular. Devuelve {ok, email, phone, phones, credits, raw, error}."""
    out: dict[str, Any] = {
        "ok": False, "email": None, "phone": None, "phones": [],
        "credits": 0, "raw": None, "error": None,
    }
    if not LUSHA_KEY:
        out["error"] = "no_lusha_key"
        return out
    if not (want_email or want_phone):
        out["error"] = "nothing_requested"
        return out
    fname = person.get("first_name") or person.get("firstName") or (person.get("name") or "").split(" ")[0]
    lname = person.get("last_name") or person.get("lastName") or " ".join((person.get("name") or "").split(" ")[1:])
    company = person.get("company") or person.get("company_name") or ""
    if not fname:
        out["error"] = "no_first_name"
        return out
    try:
        resp = requests.get(
            "https://api.lusha.com/person",
            params={"firstName": fname, "lastName": lname or "", "company": company},
            headers={"api_key": LUSHA_KEY}, timeout=10,
        )
        if resp.status_code >= 400:
            out["error"] = f"http_{resp.status_code}"
            return out
        data = resp.json()
    except Exception as e:
        out["error"] = str(e)
        return out
    out["raw"] = data
    api_emails = [(e.get("email") if isinstance(e, dict) else e) for e in (data.get("emailAddresses") or [])]
    api_emails = [e for e in api_emails if e]
    api_phones = data.get("phoneNumbers") or []
    mobile = next((p for p in api_phones if p.get("type") == "mobile"), api_phones[0] if api_phones else None)
    phone_str = (mobile.get("internationalNumber") or mobile.get("localizedNumber")) if mobile else None
    if want_email and api_emails:
        out["email"] = api_emails[0]
        out["credits"] += 1
    if want_phone and phone_str:
        out["phone"] = phone_str
        out["phones"] = [p.get("internationalNumber") or p.get("localizedNumber") for p in api_phones if p.get("internationalNumber") or p.get("localizedNumber")]
        out["credits"] += 1
    out["ok"] = bool(out["email"] or out["phone"])
    return out


def get_lusha_balance() -> dict:
    """Best-effort: pide balance de créditos a Lusha."""
    if not LUSHA_KEY:
        return {"balance": None, "error": "no_lusha_key"}
    try:
        resp = requests.get(
            "https://api.lusha.com/credits", headers={"api_key": LUSHA_KEY}, timeout=8
        )
        if resp.status_code >= 400:
            return {"balance": None, "error": f"http_{resp.status_code}"}
        data = resp.json() or {}
        return {"balance": data.get("credits") or data.get("balance"), "raw": data}
    except Exception as e:
        return {"balance": None, "error": str(e)}


def verify_whatsapp(phone: Optional[str]) -> bool:
    """Heurística para móvil mexicano (10 dígitos locales)."""
    if not phone:
        return False
    clean = re.sub(r"\D", "", phone)
    if clean.startswith("521") and len(clean) == 13:
        local = clean[3:]
    elif clean.startswith("52") and len(clean) == 12:
        local = clean[2:]
    else:
        local = clean
    return len(local) == 10


# ── Lemlist ────────────────────────────────────────────────────────


def lemlist_create_campaign(name: str) -> Optional[str]:
    if not LEMLIST_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.lemlist.com/api/campaigns",
            headers={"Authorization": LEMLIST_AUTH, "Content-Type": "application/json"},
            json={"name": name}, timeout=10,
        )
        return (resp.json() or {}).get("_id")
    except Exception as e:
        log.error(f"Lemlist create campaign error: {e}")
        return None


def lemlist_add_lead(campaign_id: str, contact: dict) -> Optional[str]:
    if not (LEMLIST_KEY and campaign_id and contact.get("email")):
        return None
    email = contact["email"]
    try:
        import urllib.parse
        url = f"https://api.lemlist.com/api/campaigns/{campaign_id}/leads/{urllib.parse.quote(email)}"
        body = {
            "firstName": contact.get("first_name") or (contact.get("name") or "").split(" ")[0],
            "lastName": contact.get("last_name") or " ".join((contact.get("name") or "").split(" ")[1:]),
            "companyName": contact.get("company_name") or "",
            "phone": contact.get("phone") or "",
            "linkedinUrl": contact.get("linkedin") or "",
        }
        resp = requests.post(
            url, headers={"Authorization": LEMLIST_AUTH, "Content-Type": "application/json"},
            json=body, timeout=10,
        )
        return (resp.json() or {}).get("_id")
    except Exception as e:
        log.error(f"Lemlist add lead error: {e}")
        return None


def lemlist_pause_campaign(campaign_id: str) -> None:
    if not (LEMLIST_KEY and campaign_id):
        return
    try:
        requests.post(
            f"https://api.lemlist.com/api/campaigns/{campaign_id}/pause",
            headers={"Authorization": LEMLIST_AUTH}, timeout=10,
        )
    except Exception as e:
        log.error(f"Lemlist pause error: {e}")


def setup_lemlist_webhooks() -> None:
    """Idempotente: registra webhooks para los 4 eventos relevantes."""
    if not LEMLIST_KEY:
        return
    target = f"{DOMAIN.rstrip('/')}/api/webhooks/lemlist"
    types = ("emailsReplied", "emailsOpened", "emailsClicked", "emailsBounced")
    try:
        existing = requests.get(
            "https://api.lemlist.com/api/hooks", headers={"Authorization": LEMLIST_AUTH}, timeout=10
        ).json() or []
    except Exception:
        existing = []
    for t in types:
        if any((h.get("targetUrl") == target and h.get("type") == t) for h in existing):
            continue
        try:
            requests.post(
                "https://api.lemlist.com/api/hooks",
                headers={"Authorization": LEMLIST_AUTH, "Content-Type": "application/json"},
                json={"targetUrl": target, "type": t}, timeout=10,
            )
            log.info(f"Lemlist webhook registered: {t}")
        except Exception as e:
            log.error(f"Lemlist webhook setup error ({t}): {e}")
