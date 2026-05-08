"""
SDR Directivo Engine — motor diario para procesar la lista maestra.

Port directo de vendedores.cloud/sdr-directivo-engine.js (~1030 líneas).
Convive con sdr_directivo.py (lib): reutiliza Apollo/Lusha/Lemlist clients,
agrega scoring Aromatex, control de créditos mensuales con HARD CAP,
processCompany (1 empresa) y engineRunDailyBatch (batch diario).

Tablas usadas:
  sdr_dir_master_companies (input + status updates)
  sdr_dir_engine_config    (config por unidad)
  sdr_dir_engine_runs      (1 row por corrida)
  sdr_dir_credits_monthly  (budget Lusha/Apollo del mes)
  sdr_dir_sequences        (output: secuencias creadas)

Idempotente por día: 1 run completed por unit por día (override con force=True).
"""

import json
import logging
import os
import re
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests

from extensions import db
from models import (
    SdrDirEngineConfig, SdrDirEngineRun, SdrDirCreditsMonthly,
    SdrDirMasterCompany, SdrDirSequence,
)
import sdr_directivo as sdrdir

log = logging.getLogger("sdr_engine")

APOLLO_KEY = os.getenv("APOLLO_API_KEY", "")
LUSHA_KEY = os.getenv("LUSHA_API_KEY", "")


# ── WA templates por unidad ────────────────────────────────────────


WA_TEMPLATES = {
    "aromatex": (
        "Hola {first_name}, soy Alejandro Gil de Aromatex. Te envie un correo "
        "y te agregue en LinkedIn. Cuando tengas un momento, me encantaria "
        "platicarte una idea para {company_name}. Saludos."
    ),
    "pestex": (
        "Hola {first_name}, soy Alan Aziz de Pestex. Te envie un correo y te "
        "agregue en LinkedIn. Cuando tengas un momento, me encantaria platicarte "
        "una idea para {company_name}. Saludos."
    ),
    "weldex": (
        "Hola {first_name}, soy Andres Zambrano de Weldex. Te envie un correo "
        "y te agregue en LinkedIn. Cuando tengas un momento, me encantaria "
        "platicarte una idea para {company_name}. Saludos."
    ),
}


def build_whatsapp_link(phone: Optional[str], first_name: str, company_name: str, unit: str) -> Optional[str]:
    if not phone:
        return None
    clean = re.sub(r"\D", "", str(phone))
    if not clean or len(clean) < 10:
        return None
    if len(clean) == 10:
        intl = "52" + clean
    elif clean.startswith("521") and len(clean) == 13:
        intl = "52" + clean[3:]
    else:
        intl = clean
    tpl = WA_TEMPLATES.get(unit) or WA_TEMPLATES["aromatex"]
    text = urllib.parse.quote(tpl.format(first_name=first_name or "", company_name=company_name or ""))
    return f"https://wa.me/{intl}?text={text}"


def parse_pipe(s: Optional[str]) -> list[str]:
    if not s:
        return []
    return [x.strip().lower() for x in str(s).split("|") if x.strip()]


# ── Apollo: search con filtros custom ──────────────────────────────


def search_contacts_custom(
    query: str,
    seniorities: list[str],
    departments: list[str],
    country: Optional[str],
    industry: Optional[str],
    per_page: int = 15,
) -> dict[str, Any]:
    """Busca contactos en Apollo con fallbacks por industry → departments."""
    if not APOLLO_KEY:
        return {"people": [], "apollo_calls": 0}
    apollo_calls = 0
    body: dict[str, Any] = {
        "person_seniorities": seniorities or ["director", "vp", "manager", "head", "chief"],
        "page": 1,
        "per_page": per_page,
        "q_organization_name": query,
    }
    if departments:
        body["person_departments"] = departments
    if country:
        body["person_locations"] = [country]
    if industry:
        body["organization_industries"] = [industry]

    def _post(b):
        return requests.post(
            "https://api.apollo.io/api/v1/mixed_people/api_search",
            headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
            json=b, timeout=15,
        )

    try:
        resp = _post(body)
        apollo_calls += 1
        data = resp.json() if resp.status_code < 400 else {}
        people = data.get("people") or []
        # Fallback 1: relajar industria
        if not people and "organization_industries" in body:
            fb = {k: v for k, v in body.items() if k != "organization_industries"}
            resp = _post(fb)
            apollo_calls += 1
            data = resp.json() if resp.status_code < 400 else {}
            people = data.get("people") or []
        # Fallback 2: relajar departamentos
        if not people and "person_departments" in body:
            fb = {k: v for k, v in body.items() if k not in ("person_departments", "organization_industries")}
            resp = _post(fb)
            apollo_calls += 1
            data = resp.json() if resp.status_code < 400 else {}
            people = data.get("people") or []
        return {"people": people, "apollo_calls": apollo_calls}
    except Exception as e:
        log.error(f"Apollo search error: {e}")
        return {"people": [], "apollo_calls": apollo_calls}


# ── Aromatex scoring rules ─────────────────────────────────────────


SENIORITY_RANK = {
    "c_suite": 100, "owner": 95, "founder": 95, "partner": 90,
    "vp": 80, "head": 70, "director": 60,
    "manager": 40, "senior": 30, "entry": 10,
}

IRRELEVANT_DEPARTMENTS_AROMATEX = {
    "master_human_resources", "human_resources", "rrhh", "recursos_humanos", "people", "people_operations",
    "master_finance", "finance", "finanzas", "tesoreria", "treasury",
    "master_information_technology", "master_engineering_technical", "information_technology", "it", "tecnologia", "engineering",
    "master_legal", "legal", "juridico",
    "master_accounting", "accounting", "contabilidad",
    "compliance", "auditoria", "audit", "internal_audit",
}

IRRELEVANT_TITLE_KEYWORDS_AROMATEX = (
    "cto", "chief technology", "director de ti", "director de tecnolog", "head of technology",
    "vp technology", "vp of technology", "cio", "chief information", "director de sistemas",
    "director de informatica", "head of it",
    "cfo", "chief financial", "director de finanzas", "tesorero", "treasurer",
    "vp finance", "vp of finance", "head of finance", "finance director", "deputy finance",
    "chro", "director de rh", "director de recursos humanos", "talento humano",
    "head of hr", "vp hr", "vp human resources", "head of people", "people director",
    "general counsel", "director legal", "director jur", "head of legal", "chief legal",
    "director de auditor", "head of audit", "head of compliance", "chief compliance",
    "controller", "contralor", "director de contabilidad", "head of accounting", "accounting director",
)

RELEVANT_DEPARTMENTS_AROMATEX = {
    "master_marketing", "marketing",
    "master_operations", "operations", "operaciones",
    "customer_service", "customer_experience", "servicio_al_cliente",
    "brand_management", "brand", "branding",
    "procurement", "purchasing", "compras",
    "master_design", "design", "visual_merchandising",
}

TITLE_BOOSTS_AROMATEX = (
    (200, ("marketing", "mercadotecnia", "marca", "branding")),
    (200, ("customer experience", "experiencia del cliente", "experiencia del pasajero",
           "patient experience", "guest experience", "experiencia del huesped")),
    (180, ("visual merchandising", "trade marketing")),
    (120, ("tiendas", "sucursales", "plazas", "showroom", "retail")),
    (100, ("compras", "sourcing", "procurement", "abastecimiento")),
    (100, ("operaciones", "operations")),
)

CEO_GM_KEYWORDS_AROMATEX = (
    "ceo", "chief executive", "director general", "gerente general", "general manager",
    "country manager", "presidente", "president", "founder", "dueño", "owner", "managing director",
)

STRONG_MATCH_THRESHOLD_AROMATEX = 250


def normalize_dept(d: Optional[str]) -> str:
    return re.sub(r"[\s\-]+", "_", (d or "").lower()).strip()


def dept_in_list(dept_norm: str, dept_set: set[str]) -> bool:
    if not dept_norm:
        return False
    if dept_norm in dept_set or f"master_{dept_norm}" in dept_set:
        return True
    return any(d in dept_norm for d in dept_set)


def has_word_keyword(haystack: str, kw: str) -> bool:
    """Word boundary match: evita 'cto' matchee 'director'."""
    if not haystack or not kw:
        return False
    escaped = re.escape(kw)
    pattern = r"(^|[^a-záéíóúñ0-9])" + escaped + r"([^a-záéíóúñ0-9]|$)"
    return bool(re.search(pattern, haystack))


def score_candidate(c: dict, priority_titles: list[str], exclude_keywords: list[str]) -> dict:
    title = (c.get("title") or "").lower()
    headline = (c.get("headline") or "").lower()
    seniority = (c.get("seniority") or "").lower()
    dept_norm = normalize_dept(c.get("department"))

    breakdown = {
        "seniority_bonus": SENIORITY_RANK.get(seniority, 0),
        "priority_title_match": 0,
        "title_match": 0,
        "department_match": 0,
        "penalty_excluded_dept": 0,
        "penalty_irrelevant_function": 0,
        "penalty_generic": 0,
        "email_bonus": 0,
        "ceo_relegation": 0,
    }

    # Priority titles (exact > includes)
    best_pt = 0
    for pt in priority_titles:
        if not pt:
            continue
        if title == pt:
            best_pt = max(best_pt, 200)
        elif pt in title:
            best_pt = max(best_pt, 80)
    breakdown["priority_title_match"] = best_pt

    # Generic / junior penalty
    for g in ("asistente", "assistant", "aux", "auxiliar", "secretari", "intern", "becario", "practicante", "trainee"):
        if has_word_keyword(title, g) or has_word_keyword(headline, g):
            breakdown["penalty_generic"] = -50
            break

    if dept_in_list(dept_norm, IRRELEVANT_DEPARTMENTS_AROMATEX):
        breakdown["penalty_excluded_dept"] = -500

    for kw in IRRELEVANT_TITLE_KEYWORDS_AROMATEX:
        if has_word_keyword(title, kw) or has_word_keyword(headline, kw):
            breakdown["penalty_irrelevant_function"] = -500
            break

    if dept_in_list(dept_norm, RELEVANT_DEPARTMENTS_AROMATEX):
        breakdown["department_match"] = 150

    title_boost = 0
    for boost, kws in TITLE_BOOSTS_AROMATEX:
        for kw in kws:
            if has_word_keyword(title, kw) or has_word_keyword(headline, kw):
                title_boost = max(title_boost, boost)
                break
    breakdown["title_match"] = title_boost

    if c.get("email") and "@" in c["email"]:
        breakdown["email_bonus"] = 5

    score = sum(breakdown.values())

    is_ceo_or_gm = any(has_word_keyword(title, kw) or has_word_keyword(headline, kw) for kw in CEO_GM_KEYWORDS_AROMATEX)
    has_relevant_signal = breakdown["title_match"] > 0 or breakdown["department_match"] > 0

    return {"score": score, "breakdown": breakdown, "is_ceo_or_gm": is_ceo_or_gm and not has_relevant_signal}


def reveal_person(person_id: str) -> Optional[dict]:
    if not (APOLLO_KEY and person_id):
        return None
    try:
        resp = requests.post(
            "https://api.apollo.io/api/v1/people/match",
            headers={"Content-Type": "application/json", "X-Api-Key": APOLLO_KEY},
            json={"id": person_id, "reveal_personal_emails": True},
            timeout=15,
        )
        data = resp.json() if resp.status_code < 400 else {}
        rp = data.get("person")
        if not rp:
            return None
        phones = rp.get("phone_numbers") or []
        depts = rp.get("departments") or []
        return {
            "first_name": rp.get("first_name") or "",
            "last_name": rp.get("last_name") or "",
            "name": f"{rp.get('first_name') or ''} {rp.get('last_name') or ''}".strip(),
            "title": rp.get("title") or "",
            "headline": rp.get("headline") or "",
            "email": rp.get("email") or "",
            "phone": phones[0].get("sanitized_number") if phones else "",
            "linkedin": rp.get("linkedin_url") or "",
            "city": rp.get("city") or "",
            "department": depts[0] if depts else "",
            "seniority": rp.get("seniority") or "",
        }
    except Exception:
        return None


# ── Credits monthly tracking ───────────────────────────────────────


def current_year_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def ensure_monthly_row(unit: str, service: str) -> SdrDirCreditsMonthly:
    ym = current_year_month()
    row = SdrDirCreditsMonthly.query.filter_by(unit=unit, service=service, year_month=ym).first()
    if row:
        return row
    # Heredar de mes anterior
    prev = (
        SdrDirCreditsMonthly.query.filter_by(unit=unit, service=service)
        .filter(SdrDirCreditsMonthly.year_month < ym)
        .order_by(SdrDirCreditsMonthly.year_month.desc()).first()
    )
    if prev:
        defaults = {"credits_limit": prev.credits_limit, "hard_cap": prev.hard_cap, "alert_threshold": prev.alert_threshold}
    elif service == "lusha":
        defaults = {"credits_limit": 600, "hard_cap": True, "alert_threshold": 0.8}
    else:
        defaults = {"credits_limit": 75, "hard_cap": False, "alert_threshold": 0.8}
    row = SdrDirCreditsMonthly(unit=unit, service=service, year_month=ym, **defaults)
    db.session.add(row)
    db.session.commit()
    return row


def check_credits_budget(unit: str, service: str, credits_to_consume: int) -> dict:
    row = ensure_monthly_row(unit, service)
    if not row.hard_cap:
        return {"allow": True, "reason": "soft_cap", "current": row.credits_used, "limit": row.credits_limit}
    will_total = (row.credits_used or 0) + credits_to_consume
    if will_total > row.credits_limit:
        return {
            "allow": False, "reason": "monthly_limit_exceeded",
            "current": row.credits_used, "limit": row.credits_limit,
            "requested": credits_to_consume,
        }
    return {"allow": True, "current": row.credits_used, "limit": row.credits_limit}


def record_credits_used(unit: str, service: str, credits_consumed: int) -> None:
    if not credits_consumed or credits_consumed <= 0:
        return
    row = ensure_monthly_row(unit, service)
    row.credits_used = (row.credits_used or 0) + credits_consumed
    row.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    check_and_fire_alerts(unit, service)


def check_and_fire_alerts(unit: str, service: str) -> None:
    row = ensure_monthly_row(unit, service)
    if not row.credits_limit:
        return
    pct = (row.credits_used or 0) / row.credits_limit
    threshold = row.alert_threshold or 0.8

    if pct >= 1.0 and not row.alerted_100:
        log.error(f"[CREDITS] CRITICAL {unit}/{service}: {row.credits_used}/{row.credits_limit} (100%) — LIMIT REACHED")
        row.alerted_100 = True
        if service == "lusha" and row.hard_cap:
            cfg = SdrDirEngineConfig.query.filter_by(unit=unit).first()
            if cfg:
                cfg.enabled = False
                cfg.updated_at = datetime.now(timezone.utc)
                log.error(f"[CRITICAL] Lusha cap reached, motor paused for {unit}")
    elif pct >= 0.95 and not row.alerted_95:
        log.warning(f"[CREDITS] {unit}/{service}: {row.credits_used}/{row.credits_limit} (95%) — VERY CLOSE TO LIMIT")
        row.alerted_95 = True
    elif pct >= threshold and not row.alerted_80:
        log.warning(f"[CREDITS] {unit}/{service}: {row.credits_used}/{row.credits_limit} ({int(pct*100)}%) — APPROACHING LIMIT")
        row.alerted_80 = True
    db.session.commit()


def sync_lusha_credits_from_api(unit: str) -> dict:
    if not LUSHA_KEY:
        return {"synced": False, "reason": "no_key"}
    try:
        bal = sdrdir.get_lusha_balance()
        balance = bal.get("balance")
        if not isinstance(balance, (int, float)):
            return {"synced": False, "reason": bal.get("error") or "no_balance", "raw": bal.get("raw")}
        row = ensure_monthly_row(unit, "lusha")
        raw = bal.get("raw") or {}
        total = raw.get("creditsTotal") if isinstance(raw, dict) else None
        if not isinstance(total, (int, float)):
            total = raw.get("total") if isinstance(raw, dict) else None
        if isinstance(total, (int, float)):
            used = max(0, int(total) - int(balance))
        else:
            used = max(0, row.credits_limit - int(balance))
        row.credits_used = used
        row.last_sync_at = datetime.now(timezone.utc)
        row.details_json = {"source": "lusha_api", "raw": raw, "balance": balance}
        row.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        return {"synced": True, "used": used, "balance": balance, "total": total}
    except Exception as e:
        return {"synced": False, "reason": str(e)}


# ── Helpers DB ─────────────────────────────────────────────────────


def get_config(unit: str) -> Optional[SdrDirEngineConfig]:
    return SdrDirEngineConfig.query.filter_by(unit=unit).first()


def get_today_run(unit: str) -> Optional[SdrDirEngineRun]:
    today = date.today()
    return (
        SdrDirEngineRun.query.filter_by(unit=unit)
        .filter(db.func.date(SdrDirEngineRun.started_at) == today)
        .filter(SdrDirEngineRun.status.in_(["running", "completed", "partial", "failed"]))
        .order_by(SdrDirEngineRun.id.desc()).first()
    )


# ── processCompany: 1 empresa → contactos rankeados → push Lemlist ─


def process_company(company: SdrDirMasterCompany, config: SdrDirEngineConfig, dry_run: bool = False) -> dict:
    unit = company.unit
    tam = (company.tam or "").upper()
    want_phone = bool(config.tam_a_enrich_phone) if tam == "A" else bool(config.tam_bc_enrich_phone)
    phones_per_company = (config.tam_a_phones_per_company if tam == "A" else config.tam_bc_phones_per_company) or 0
    phones_needed = phones_per_company if want_phone else 0

    summary: dict[str, Any] = {
        "company_id": company.id, "company_name": company.company_name,
        "tam": tam, "want_phone": want_phone,
        "phones_requested": phones_needed, "phones_returned": 0,
        "contacts_found": 0, "contacts_pushed": 0,
        "apollo_calls": 0, "lusha_credits_used": 0,
        "queries_tried": [], "excluded_by_keyword": 0,
        "errors": [], "dry_run": dry_run,
        "candidates": [], "skipped_due_to_credits": False,
    }

    seniorities = parse_pipe(company.seniorities)
    departments = parse_pipe(company.departments)
    priority_titles = parse_pipe(company.priority_titles)
    exclude_kw = parse_pipe(company.exclude_keywords)
    country = (company.country or "Mexico").strip() or "Mexico"
    industry = ((company.apollo_industry or "").strip().lower()) or None

    # Apollo: query + fallbacks por apollo_alt_queries
    alt_queries_raw = [x.strip() for x in (company.apollo_alt_queries or "").split("|") if x.strip()]
    queries = [company.apollo_query or company.company_name] + alt_queries_raw

    people = []
    for q in queries:
        r = search_contacts_custom(query=q, seniorities=seniorities, departments=departments,
                                   country=country, industry=industry)
        summary["apollo_calls"] += r["apollo_calls"]
        summary["queries_tried"].append({"query": q, "hits": len(r["people"])})
        log.info(f"[ENGINE] {company.company_name} apollo q='{q}' -> {len(r['people'])} hits")
        if r["people"]:
            people = r["people"]
            break

    if not people:
        summary["errors"].append("apollo_no_match_after_fallbacks" if alt_queries_raw else "apollo_no_people")
        return summary

    # Reveal candidatos (buffer)
    top_n = config.max_contacts_per_company or 2
    candidates = []
    for p in people[: max(top_n * 4, top_n + 4)]:
        pid = p.get("id")
        if not pid:
            continue
        r = reveal_person(pid)
        summary["apollo_calls"] += 1
        if r:
            candidates.append(r)
            if len(candidates) >= top_n * 3:
                break
    summary["contacts_found"] = len(candidates)

    # Filtro exclude_keywords
    excluded_by_kw: dict[str, int] = {}
    if exclude_kw:
        kept = []
        for c in candidates:
            t = (c.get("title") or "").lower()
            excluded = False
            for kw in exclude_kw:
                if kw and kw in t:
                    excluded_by_kw[kw] = excluded_by_kw.get(kw, 0) + 1
                    excluded = True
                    break
            if not excluded:
                kept.append(c)
        summary["excluded_by_keyword"] = len(candidates) - len(kept)
        candidates = kept
        if summary["excluded_by_keyword"]:
            log.info(f"[ENGINE] {company.company_name} excluded_by_keyword: {excluded_by_kw}")

    if not candidates:
        summary["errors"].append("all_candidates_excluded_by_keyword")
        return summary

    # Score + two-pass CEO/GM relegation
    scored = []
    for c in candidates:
        r = score_candidate(c, priority_titles, exclude_kw)
        scored.append({"c": c, "score": r["score"], "breakdown": r["breakdown"], "is_ceo_or_gm": r["is_ceo_or_gm"]})

    has_strong_match = any(
        not s["is_ceo_or_gm"]
        and (s["breakdown"]["title_match"] > 0 or s["breakdown"]["department_match"] > 0)
        and s["score"] > STRONG_MATCH_THRESHOLD_AROMATEX
        for s in scored
    )
    for s in scored:
        if not s["is_ceo_or_gm"]:
            continue
        adj = -100 if has_strong_match else 80
        s["breakdown"]["ceo_relegation"] = adj
        s["score"] += adj

    # Sort: con email primero, luego score desc
    scored.sort(key=lambda s: (-(1 if s["c"].get("email") and "@" in s["c"]["email"] else 0), -s["score"]))
    selected_scored = scored[:top_n]
    selected = [s["c"] for s in selected_scored]

    if not selected:
        summary["errors"].append("no_email_in_apollo_reveal")
        return summary

    # Lusha enrich diferenciado por TAM (skip si dry_run)
    for c in selected:
        if dry_run:
            continue
        if not c.get("first_name") or not LUSHA_KEY:
            continue
        has_email = bool(c.get("email") and "@" in c["email"])
        need_email = not has_email
        need_phone = (not c.get("phone")) and want_phone and phones_needed > 0
        if not (need_email or need_phone):
            continue
        est_credits = (1 if need_email else 0) + (1 if need_phone else 0)
        budget = check_credits_budget(unit, "lusha", est_credits)
        if not budget["allow"]:
            summary["skipped_due_to_credits"] = True
            summary["errors"].append("lusha_budget_exhausted")
            log.warning(
                f"[ENGINE] {company.company_name} skipped Lusha: budget exhausted "
                f"({budget['current']}/{budget['limit']})"
            )
            break
        r = sdrdir.enrich_lusha(
            {"first_name": c["first_name"], "last_name": c.get("last_name", ""),
             "company": company.company_name},
            want_email=need_email, want_phone=need_phone,
        )
        summary["lusha_credits_used"] += r.get("credits") or 0
        record_credits_used(unit, "lusha", r.get("credits") or 0)
        if r.get("email") and need_email:
            c["email"] = r["email"]
        if r.get("phone") and need_phone:
            c["phone"] = r["phone"]
            phones_needed = max(0, phones_needed - 1)
            summary["phones_returned"] += 1

    def _build_candidate(s, idx):
        return {
            "name": s["c"].get("name"), "title": s["c"].get("title"),
            "email": s["c"].get("email"), "phone": s["c"].get("phone"),
            "linkedin": s["c"].get("linkedin"), "department": s["c"].get("department"),
            "seniority": s["c"].get("seniority"), "headline": s["c"].get("headline") or "",
            "score": s["score"], "score_breakdown": s["breakdown"],
            "would_be_selected": idx < top_n,
        }

    if dry_run:
        summary["candidates"] = [_build_candidate(s, i) for i, s in enumerate(scored)]
        summary["candidates_total"] = len(scored)
        summary["candidates_selected"] = min(top_n, len(scored))
        return summary

    summary["candidates"] = [_build_candidate(s, i) for i, s in enumerate(selected_scored)]

    # Insertar SdrDirSequence + push Lemlist
    camp = config.lemlist_master_campaign_id
    now = datetime.now(timezone.utc)
    for c in selected:
        if not c.get("email"):
            continue
        first_name = c.get("first_name") or (c.get("name") or "").split(" ")[0]
        wa_link = build_whatsapp_link(c.get("phone"), first_name, company.company_name, unit)
        lemlist_lead_id = None
        if camp:
            try:
                lemlist_lead_id = sdrdir.lemlist_add_lead(camp, {
                    "email": c["email"],
                    "first_name": first_name,
                    "last_name": c.get("last_name", ""),
                    "company_name": company.company_name,
                    "phone": c.get("phone") or "",
                    "linkedin": c.get("linkedin") or "",
                })
            except Exception as e:
                summary["errors"].append(f"lemlist_push_failed:{e}")
        else:
            summary["errors"].append("no_master_campaign_configured")

        seq = SdrDirSequence(
            company_name=company.company_name, company_domain="",
            contact_name=c.get("name"), contact_title=c.get("title"),
            contact_email=c["email"], contact_phone=c.get("phone") or "",
            contact_linkedin=c.get("linkedin") or "",
            whatsapp_verified=False,
            unit=unit, assigned_to=None, status="activa",
            current_step=0, first_channel="email",
            lemlist_campaign_id=camp, lemlist_lead_id=lemlist_lead_id,
            last_action_at=now, next_action_at=None,
            master_company_id=company.id, whatsapp_link=wa_link,
            lead_state="sin_respuesta", state_changed_at=now,
        )
        db.session.add(seq)
        summary["contacts_pushed"] += 1
    db.session.commit()
    return summary


# ── engine_run_daily_batch: entrada principal ──────────────────────


def engine_run_daily_batch(unit: str = "aromatex", dry_run: bool = False, force: bool = False,
                            override_max_companies: Optional[int] = None) -> dict:
    config = get_config(unit)
    if not config:
        return {"ok": False, "error": "no_config_for_unit"}
    if not force and not config.enabled:
        return {"ok": False, "error": "engine_disabled"}

    if not force:
        today = get_today_run(unit)
        if today and today.status == "completed":
            return {"ok": False, "error": "already_ran_today", "run_id": today.id}

    ensure_monthly_row(unit, "lusha")
    ensure_monthly_row(unit, "apollo")

    lusha_balance = None
    if not dry_run and LUSHA_KEY:
        try:
            sync_lusha_credits_from_api(unit)
        except Exception:
            pass
        try:
            bal = sdrdir.get_lusha_balance()
            lusha_balance = bal.get("balance")
            if isinstance(lusha_balance, (int, float)) and lusha_balance < (config.min_lusha_balance_alert or 50):
                return {"ok": False, "error": "lusha_balance_low", "balance": lusha_balance}
        except Exception:
            pass

    run = SdrDirEngineRun(unit=unit, status="running")
    db.session.add(run)
    db.session.commit()

    max_companies = override_max_companies or config.max_companies_per_day or 10
    companies = (
        SdrDirMasterCompany.query
        .filter_by(unit=unit, status="pending", requires_manual=False)
        .order_by(SdrDirMasterCompany.priority_order.asc())
        .limit(max_companies).all()
    )

    totals = {"attempted": 0, "processed": 0, "no_contacts": 0,
              "contacts_pushed": 0, "lusha_used": 0, "apollo_calls": 0,
              "errors": [], "skipped_due_to_credits": []}
    tam_breakdown = {"A": {"companies": 0, "phones_requested": 0, "phones_returned": 0},
                     "B": {"companies": 0, "phones_requested": 0, "phones_returned": 0},
                     "C": {"companies": 0, "phones_requested": 0, "phones_returned": 0}}
    details_per_company = []

    for co in companies:
        totals["attempted"] += 1
        if not dry_run:
            tam_a = (co.tam or "").upper() == "A"
            would_need_phone = bool(config.tam_a_enrich_phone) if tam_a else bool(config.tam_bc_enrich_phone)
            if would_need_phone:
                pre = check_credits_budget(co.unit, "lusha", 1)
                if not pre["allow"]:
                    co.status = "skipped"
                    co.last_attempt_at = datetime.now(timezone.utc)
                    co.skip_reason = "monthly_credits_exceeded_lusha"
                    co.updated_at = datetime.now(timezone.utc)
                    db.session.commit()
                    totals["skipped_due_to_credits"].append({"company_id": co.id, "name": co.company_name})
                    totals["errors"].append({"company_id": co.id, "error": "monthly_credits_exceeded_lusha"})
                    continue
            co.status = "processing"
            co.last_attempt_at = datetime.now(timezone.utc)
            co.updated_at = datetime.now(timezone.utc)
            db.session.commit()

        try:
            res = process_company(co, config, dry_run=dry_run)
        except Exception as e:
            totals["errors"].append({"company_id": co.id, "error": str(e)})
            if not dry_run:
                co.status = "pending"
                co.skip_reason = f"exception:{e}"
                co.updated_at = datetime.now(timezone.utc)
                db.session.commit()
            continue

        details_per_company.append(res)
        totals["contacts_pushed"] += res["contacts_pushed"]
        totals["lusha_used"] += res["lusha_credits_used"]
        totals["apollo_calls"] += res["apollo_calls"]

        tam_bucket = res["tam"] if res["tam"] in ("A", "B", "C") else None
        if tam_bucket:
            tam_breakdown[tam_bucket]["companies"] += 1
            tam_breakdown[tam_bucket]["phones_requested"] += res.get("phones_requested") or 0
            tam_breakdown[tam_bucket]["phones_returned"] += res.get("phones_returned") or 0

        if not dry_run:
            now = datetime.now(timezone.utc)
            if res["skipped_due_to_credits"] and res["contacts_pushed"] == 0:
                co.status = "skipped"
                co.last_attempt_at = now
                co.skip_reason = "monthly_credits_exceeded_lusha"
                co.updated_at = now
                totals["skipped_due_to_credits"].append({"company_id": co.id, "name": co.company_name})
            elif res["contacts_pushed"] > 0:
                co.status = "processed"
                co.processed_at = now
                co.contacts_found = res["contacts_found"]
                co.lusha_credits_used = res["lusha_credits_used"]
                co.updated_at = now
                totals["processed"] += 1
            else:
                reason = (res["errors"] or ["no_contacts_found"])[0]
                co.status = "no_contacts"
                co.last_attempt_at = now
                co.skip_reason = reason
                co.updated_at = now
                totals["no_contacts"] += 1
            db.session.commit()

    # Snapshot créditos después del run
    ym = current_year_month()
    lusha_row = SdrDirCreditsMonthly.query.filter_by(unit=unit, service="lusha", year_month=ym).first()
    apollo_row = SdrDirCreditsMonthly.query.filter_by(unit=unit, service="apollo", year_month=ym).first()
    credits_balance_after = {
        "lusha":  max(0, (lusha_row.credits_limit  - (lusha_row.credits_used  or 0))) if lusha_row  else None,
        "apollo": max(0, (apollo_row.credits_limit - (apollo_row.credits_used or 0))) if apollo_row else None,
    }

    details_payload = {
        "companies": details_per_company,
        "errors": totals["errors"],
        "credits_used_by_service": {"lusha": totals["lusha_used"], "apollo": totals["apollo_calls"]},
        "credits_balance_after": credits_balance_after,
        "tam_breakdown": tam_breakdown,
        "skipped_due_to_credits": totals["skipped_due_to_credits"],
        "lusha_balance": lusha_balance,
    }

    now = datetime.now(timezone.utc)
    if not dry_run:
        run.finished_at = now
        run.status = "completed"
        run.companies_attempted = totals["attempted"]
        run.companies_processed = totals["processed"]
        run.companies_no_contacts = totals["no_contacts"]
        run.contacts_pushed_to_lemlist = totals["contacts_pushed"]
        run.lusha_credits_used = totals["lusha_used"]
        run.apollo_calls = totals["apollo_calls"]
        run.details_json = details_payload
        config.last_run_at = now
        config.last_run_summary = json.dumps({
            "totals": totals, "lusha_balance": lusha_balance, "dry_run": False,
            "ran_at": now.isoformat(), "tam_breakdown": tam_breakdown,
            "credits_balance_after": credits_balance_after,
        }, default=str)
        config.updated_at = now
    else:
        run.finished_at = now
        run.status = "completed"
        run.companies_attempted = totals["attempted"]
        run.companies_processed = 0
        run.companies_no_contacts = 0
        run.contacts_pushed_to_lemlist = 0
        run.lusha_credits_used = 0
        run.apollo_calls = totals["apollo_calls"]
        run.details_json = {"dry_run": True, **details_payload}
        run.error_log = "dry_run"
    db.session.commit()

    return {
        "ok": True, "run_id": run.id, "unit": unit, "dry_run": dry_run,
        "lusha_balance": lusha_balance,
        "totals": totals, "companies": details_per_company,
        "tam_breakdown": tam_breakdown,
        "credits_balance_after": credits_balance_after,
    }


# ── CSV import ─────────────────────────────────────────────────────


def parse_csv_row(line: str) -> list[str]:
    """RFC 4180 mínimo: maneja campos con comas y comillas escapadas."""
    out = []
    cur = ""
    in_q = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_q:
            if ch == '"' and i + 1 < len(line) and line[i + 1] == '"':
                cur += '"'
                i += 1
            elif ch == '"':
                in_q = False
            else:
                cur += ch
        else:
            if ch == ",":
                out.append(cur)
                cur = ""
            elif ch == '"':
                in_q = True
            else:
                cur += ch
        i += 1
    out.append(cur)
    return out


def parse_csv(text: str) -> dict:
    raw_lines = [l for l in text.replace("\r", "").split("\n") if l]
    if not raw_lines:
        return {"header": [], "rows": []}
    # Re-asociar líneas con saltos dentro de comillas
    lines = []
    buf = ""
    for l in raw_lines:
        buf = buf + "\n" + l if buf else l
        if buf.count('"') % 2 == 0:
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)
    header = [h.strip() for h in parse_csv_row(lines[0])]
    rows = []
    for l in lines[1:]:
        cols = parse_csv_row(l)
        row = {}
        for j, h in enumerate(header):
            row[h] = (cols[j] if j < len(cols) else "").strip()
        rows.append(row)
    return {"header": header, "rows": rows}


CSV_V3_HEADERS = (
    "priority_order", "company_name", "apollo_query", "apollo_alt_queries",
    "apollo_industry", "sector", "tam", "origen", "sucursales", "estados", "country",
    "seniorities", "departments", "priority_titles", "exclude_keywords",
    "requires_manual", "notes",
)


def detect_csv_version(header: list[str]) -> dict:
    h = [x.strip().lower() for x in header]
    has_v3 = all(c in h for c in ("apollo_alt_queries", "apollo_industry", "country", "exclude_keywords"))
    if has_v3:
        order_ok = (len(h) == len(CSV_V3_HEADERS)) and all(h[i] == c for i, c in enumerate(CSV_V3_HEADERS))
        return {"version": "v3", "order_ok": order_ok, "header": h}
    return {"version": "legacy", "order_ok": True, "header": h}


def import_master_csv(csv_text: str, unit: str = "aromatex") -> dict:
    parsed = parse_csv(csv_text)
    rows = parsed["rows"]
    header = parsed["header"]
    det = detect_csv_version(header)
    imported, skipped, errors = 0, 0, 0
    error_rows = []
    for r in rows:
        try:
            name = r.get("company_name") or r.get("Empresa") or r.get("empresa")
            if not name:
                errors += 1
                error_rows.append({"row": r, "reason": "no_company_name"})
                continue
            existing = (
                SdrDirMasterCompany.query
                .filter(db.func.lower(SdrDirMasterCompany.company_name) == name.lower())
                .filter_by(unit=unit).first()
            )
            if existing:
                skipped += 1
                continue
            row = SdrDirMasterCompany(
                priority_order=int(r.get("priority_order") or 9999),
                company_name=name,
                apollo_query=r.get("apollo_query") or name,
                apollo_alt_queries=r.get("apollo_alt_queries") or None,
                apollo_industry=r.get("apollo_industry") or None,
                sector=r.get("sector") or "",
                tam=r.get("tam") or None,
                origen=r.get("origen") or None,
                sucursales=int(r["sucursales"]) if r.get("sucursales") else None,
                estados=r.get("estados") or None,
                country=r.get("country") or "Mexico",
                seniorities=r.get("seniorities") or None,
                departments=r.get("departments") or None,
                priority_titles=r.get("priority_titles") or None,
                exclude_keywords=r.get("exclude_keywords") or None,
                requires_manual=str(r.get("requires_manual", "")).lower() in ("si", "sí", "yes", "1", "true"),
                notes=r.get("notes") or None,
                unit=unit, status="pending",
            )
            db.session.add(row)
            imported += 1
        except Exception as e:
            errors += 1
            error_rows.append({"row": r, "reason": str(e)})
    db.session.commit()
    return {
        "imported": imported, "skipped": skipped, "errors": errors,
        "error_rows": error_rows[:10], "total": len(rows),
        "csv_version": det["version"], "v3_order_ok": det["order_ok"],
        "header_received": header,
    }
