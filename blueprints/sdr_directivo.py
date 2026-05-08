# blueprints/sdr_directivo.py
"""
SDR Directivo — endpoints de outreach a tomadores de decisión.
Rutas bajo /api/sdr-directivo/. Webhook Lemlist en /api/webhooks/lemlist.

Port de los routes /api/sdr-directivo/* y /api/webhooks/lemlist de
vendedores.cloud (server.js ~786-994). Engine + master CSV import +
credits granular se cubren en Round 2d.
"""
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from sqlalchemy import func

from extensions import db
from models import (
    SdrDirSequence, SdrDirHistory, SdrDirSuggestion, SdrDirMasterCompany,
    SdrDirEngineConfig, SdrDirEngineRun, SdrDirCreditsMonthly,
    Lead, OrigenLead, EtapaPipeline,
)
import sdr_directivo as sdrdir
import sdr_directivo_engine as engine

sdr_directivo_bp = Blueprint("sdr_directivo", __name__)
lemlist_webhook_bp = Blueprint("lemlist_webhook", __name__)


# ── Helpers ────────────────────────────────────────────────────────


def _norm(s: str) -> str:
    return (s or "").lower().strip()


# ── Apollo: suggestions / search / contacts ────────────────────────


@sdr_directivo_bp.route("/suggestions", methods=["GET"])
def suggestions():
    """Pide sugerencias a Apollo, filtra duplicadas vs sdr_dir_suggestions y
    sdr_dir_sequences, persiste las nuevas como pendientes."""
    unit = (request.args.get("unit") or "aromatex").lower()
    raw = sdrdir.suggest_companies(unit)
    out = []
    for c in raw:
        if len(out) >= 5:
            break
        name_l = _norm(c.get("name"))
        if not name_l:
            continue
        if db.session.query(SdrDirSuggestion.id).filter(
            func.lower(SdrDirSuggestion.company_name) == name_l,
            SdrDirSuggestion.unit == unit,
        ).first():
            continue
        if db.session.query(SdrDirSequence.id).filter(
            func.lower(SdrDirSequence.company_name) == name_l,
            SdrDirSequence.unit == unit,
        ).first():
            continue
        sug = SdrDirSuggestion(
            company_name=c.get("name"), company_domain=c.get("domain") or "",
            company_industry=c.get("industry") or "",
            company_size=str(c.get("size") or ""),
            company_country=c.get("country") or "Mexico",
            unit=unit, suggested_to=None,
        )
        db.session.add(sug)
        out.append(c)
    db.session.commit()
    return jsonify({"suggestions": out})


@sdr_directivo_bp.route("/search", methods=["GET"])
def search_companies():
    company = request.args.get("company")
    unit = request.args.get("unit") or "aromatex"
    if not company:
        return jsonify({"error": "company required"}), 400
    companies = sdrdir.search_companies(company, unit)
    companies.sort(key=lambda x: -(x.get("size") or 0))
    return jsonify({"companies": companies})


@sdr_directivo_bp.route("/contacts", methods=["GET"])
def contacts():
    domain = request.args.get("domain") or ""
    company = request.args.get("company") or ""
    unit = request.args.get("unit") or "aromatex"
    contacts_list = sdrdir.search_contacts(domain, company, unit)
    for c in contacts_list:
        if not c.get("phone") and c.get("first_name") and c.get("last_name"):
            phone = sdrdir.enrich_phone(c["first_name"], c["last_name"], company)
            if phone:
                c["phone"] = phone
        c["whatsapp_verified"] = sdrdir.verify_whatsapp(c.get("phone"))
    return jsonify({"contacts": contacts_list})


# ── Sequences: start / list / advance / status ─────────────────────


@sdr_directivo_bp.route("/start-sequence", methods=["POST"])
def start_sequence():
    """Crea una secuencia (paso 1 = enviado), opcionalmente sube el lead a
    Lemlist si la unidad tiene email sender configurado."""
    data = request.get_json() or {}
    unit = data.get("unit") or "aromatex"
    company_name = data.get("company_name") or ""
    contact_name = data.get("contact_name") or ""
    contact_email = data.get("contact_email") or ""
    contact_phone = data.get("contact_phone") or ""
    contact_linkedin = data.get("contact_linkedin") or ""
    whatsapp_verified = bool(data.get("whatsapp_verified"))
    first_message = data.get("first_message") or ""

    first_channel = "whatsapp" if whatsapp_verified else "email"
    now = datetime.now(timezone.utc)
    next_action = now + timedelta(days=2)

    campaign_id = None
    lead_id = None
    sender = sdrdir.SENDERS.get(unit) or {}
    if sender.get("email") and contact_email:
        campaign_name = f"SDR-{unit}-{(company_name or '')[:30]}-{now.strftime('%Y%m%d')}"
        campaign_id = sdrdir.lemlist_create_campaign(campaign_name)
        if campaign_id:
            parts = (contact_name or "").split(" ")
            lead_id = sdrdir.lemlist_add_lead(campaign_id, {
                "first_name": parts[0] if parts else "",
                "last_name": " ".join(parts[1:]) if len(parts) > 1 else "",
                "email": contact_email, "phone": contact_phone,
                "linkedin": contact_linkedin, "company_name": company_name,
            })

    seq = SdrDirSequence(
        company_name=company_name,
        company_domain=data.get("company_domain") or "",
        contact_name=contact_name,
        contact_title=data.get("contact_title") or "",
        contact_email=contact_email, contact_phone=contact_phone,
        contact_linkedin=contact_linkedin,
        whatsapp_verified=whatsapp_verified, unit=unit,
        assigned_to=None, status="activa", current_step=1,
        first_channel=first_channel,
        lemlist_campaign_id=campaign_id, lemlist_lead_id=lead_id,
        last_action_at=now, next_action_at=next_action,
    )
    db.session.add(seq)
    db.session.flush()

    step_def = sdrdir.get_step_def(first_channel, 0)
    db.session.add(SdrDirHistory(
        sequence_id=seq.id, step_number=1, channel=step_def["channel"],
        message_preview=(first_message or "")[:500], status="enviado",
    ))
    db.session.commit()
    return jsonify(seq.to_dict())


@sdr_directivo_bp.route("/sequences", methods=["GET"])
def list_sequences():
    q = SdrDirSequence.query
    status_f = request.args.get("status")
    unit_f = request.args.get("unit")
    if status_f:
        q = q.filter(SdrDirSequence.status == status_f)
    if unit_f:
        q = q.filter(SdrDirSequence.unit == unit_f)

    # Orden: activa first, respondio second, otros después; luego next_action_at asc
    rows = q.order_by(
        db.case(
            (SdrDirSequence.status == "activa", 0),
            (SdrDirSequence.status == "respondio", -1),
            else_=1,
        ),
        SdrDirSequence.next_action_at.asc().nullslast(),
    ).all()

    out = []
    for s in rows:
        d = s.to_dict()
        d["history"] = [h.to_dict() for h in s.history.order_by(SdrDirHistory.step_number).all()]
        out.append(d)
    return jsonify(out)


@sdr_directivo_bp.route("/<int:seq_id>/advance", methods=["PUT"])
def advance(seq_id: int):
    seq = db.session.get(SdrDirSequence, seq_id)
    if not seq:
        return jsonify({"error": "Not found"}), 404

    next_step = seq.current_step + 1
    step_def = sdrdir.get_step_def(seq.first_channel, seq.current_step)
    msg = sdrdir.get_step_message(
        seq.unit, seq.current_step, seq.first_channel, seq.contact_name, seq.company_name
    )
    db.session.add(SdrDirHistory(
        sequence_id=seq.id, step_number=next_step, channel=step_def["channel"],
        message_preview=(msg or "")[:500], status="enviado",
    ))

    now = datetime.now(timezone.utc)
    if next_step >= 8:
        seq.current_step = 8
        seq.status = "descartado"
        seq.last_action_at = now
        seq.next_action_at = None
    else:
        delta = (sdrdir.STEP_DAYS[next_step] or 0) - (sdrdir.STEP_DAYS[next_step - 1] or 0)
        seq.current_step = next_step
        seq.last_action_at = now
        seq.next_action_at = now + timedelta(days=delta)
    db.session.commit()

    out = seq.to_dict()
    out["history"] = [h.to_dict() for h in seq.history.order_by(SdrDirHistory.step_number).all()]
    return jsonify(out)


@sdr_directivo_bp.route("/<int:seq_id>/status", methods=["PUT"])
def update_status(seq_id: int):
    seq = db.session.get(SdrDirSequence, seq_id)
    if not seq:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    status = data.get("status")
    notes = data.get("notes")
    now = datetime.now(timezone.utc)

    seq.status = status or seq.status
    seq.last_action_at = now
    if notes is not None:
        seq.notes = notes

    if status == "respondio":
        seq.next_action_at = None
        seq.paused_reason = "respondio"
        if seq.lemlist_campaign_id:
            sdrdir.lemlist_pause_campaign(seq.lemlist_campaign_id)
        # Crear lead correspondiente
        if seq.contact_phone:
            try:
                lead = Lead(
                    telefono=seq.contact_phone,
                    nombre=seq.contact_name or "",
                    empresa_nombre=seq.company_name or "",
                    origen=OrigenLead.PROSPECCION,
                    tipo_cliente=f"{seq.contact_title or ''} | {seq.company_domain or ''}".strip(" |"),
                    etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
                )
                db.session.add(lead)
            except Exception:
                # phone duplicado o constraint — no rompemos el status update
                db.session.rollback()
                seq = db.session.get(SdrDirSequence, seq_id)
                seq.status = status
                seq.last_action_at = now
                seq.next_action_at = None
                seq.paused_reason = "respondio"
                if notes is not None:
                    seq.notes = notes
    elif status in ("cerrado", "descartado"):
        seq.next_action_at = None
        if status == "descartado":
            db.session.query(SdrDirSuggestion).filter(
                func.lower(SdrDirSuggestion.company_name) == _norm(seq.company_name),
                SdrDirSuggestion.unit == seq.unit,
            ).update({"status": "descartado"}, synchronize_session=False)
    elif status == "activa":
        seq.next_action_at = now
        seq.paused_reason = None

    db.session.commit()
    out = seq.to_dict()
    out["history"] = [h.to_dict() for h in seq.history.order_by(SdrDirHistory.step_number).all()]
    return jsonify(out)


@sdr_directivo_bp.route("/stats", methods=["GET"])
def stats():
    base = SdrDirSequence.query
    total = base.count()
    active = base.filter(SdrDirSequence.status == "activa").count()
    responded = base.filter(SdrDirSequence.status == "respondio").count()
    closed = base.filter(SdrDirSequence.status == "cerrado").count()
    by_step = (
        db.session.query(SdrDirSequence.current_step, func.count())
        .filter(SdrDirSequence.status == "activa")
        .group_by(SdrDirSequence.current_step).order_by(SdrDirSequence.current_step).all()
    )
    by_unit = (
        db.session.query(SdrDirSequence.unit, func.count())
        .filter(SdrDirSequence.status.in_(["activa", "respondio"]))
        .group_by(SdrDirSequence.unit).all()
    )
    sug_pending = SdrDirSuggestion.query.filter(SdrDirSuggestion.status == "pendiente").count()
    sug_used = SdrDirSuggestion.query.filter(SdrDirSuggestion.status != "pendiente").count()
    rate = f"{(responded / total * 100):.1f}" if total else "0"
    return jsonify({
        "total": total, "active": active, "responded": responded, "closed": closed,
        "responseRate": rate,
        "byStep": [{"current_step": s, "c": c} for s, c in by_step],
        "byUnit": [{"unit": u, "c": c} for u, c in by_unit],
        "sugPending": sug_pending, "sugUsed": sug_used,
    })


# ── Master companies CRUD (sin CSV import — eso va en Round 2d) ────


@sdr_directivo_bp.route("/master", methods=["GET"])
def list_master():
    unit = (request.args.get("unit") or "aromatex").lower()
    status_f = request.args.get("status")
    try:
        limit = min(int(request.args.get("limit") or 50), 500)
        page = max(int(request.args.get("page") or 1), 1)
    except ValueError:
        limit, page = 50, 1
    offset = (page - 1) * limit

    q = SdrDirMasterCompany.query.filter(SdrDirMasterCompany.unit == unit)
    if status_f:
        q = q.filter(SdrDirMasterCompany.status == status_f)
    total = q.count()
    rows = q.order_by(
        SdrDirMasterCompany.priority_order.asc(),
        SdrDirMasterCompany.id.asc(),
    ).limit(limit).offset(offset).all()

    by_status_rows = (
        db.session.query(SdrDirMasterCompany.status, func.count())
        .filter(SdrDirMasterCompany.unit == unit)
        .group_by(SdrDirMasterCompany.status).all()
    )
    by_status = [{"status": s, "c": c} for s, c in by_status_rows]
    return jsonify({"total": total, "page": page, "limit": limit,
                    "rows": [r.to_dict() for r in rows], "byStatus": by_status})


@sdr_directivo_bp.route("/master/<int:master_id>", methods=["PATCH"])
def patch_master(master_id: int):
    row = db.session.get(SdrDirMasterCompany, master_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json() or {}
    allowed = {"status", "requires_manual", "notes", "priority_order",
               "seniorities", "departments", "priority_titles", "apollo_query"}
    touched = False
    for k in allowed:
        if k in data:
            setattr(row, k, data[k])
            touched = True
    if not touched:
        return jsonify({"error": "No fields to update"}), 400
    db.session.commit()
    return jsonify(row.to_dict())


@sdr_directivo_bp.route("/master/<int:master_id>", methods=["DELETE"])
def delete_master(master_id: int):
    row = db.session.get(SdrDirMasterCompany, master_id)
    if not row:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"ok": True})


@sdr_directivo_bp.route("/master/import", methods=["POST"])
def import_master_json():
    """Acepta:
      - Content-Type text/csv (CSV crudo en body)
      - Content-Type application/json con {csv: "..."} o {rows: [...]}
    """
    unit = (request.args.get("unit") or "aromatex").lower()

    # Caso 1: CSV crudo en body
    ct = (request.content_type or "").lower()
    if ct.startswith("text/csv") or ct.startswith("text/plain"):
        csv_text = request.get_data(as_text=True)
        if not csv_text:
            return jsonify({"error": "empty CSV body"}), 400
        return jsonify(engine.import_master_csv(csv_text, unit))

    # Caso 2: JSON con csv string
    payload = request.get_json(silent=True) or {}
    if isinstance(payload.get("csv"), str) and payload["csv"]:
        return jsonify(engine.import_master_csv(payload["csv"], unit))

    rows = payload.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "Send text/csv body, JSON {csv:'...'} or {rows:[...]}"}), 400

    imported, skipped, errors = 0, 0, 0
    error_rows = []
    for r in rows:
        try:
            name = r.get("company_name")
            if not name:
                errors += 1
                error_rows.append({"row": r, "reason": "no_company_name"})
                continue
            exists = (
                db.session.query(SdrDirMasterCompany.id)
                .filter(func.lower(SdrDirMasterCompany.company_name) == _norm(name))
                .filter(SdrDirMasterCompany.unit == unit).first()
            )
            if exists:
                skipped += 1
                continue
            row = SdrDirMasterCompany(
                priority_order=int(r.get("priority_order") or 9999),
                company_name=name,
                apollo_query=r.get("apollo_query") or name,
                apollo_alt_queries=r.get("apollo_alt_queries"),
                apollo_industry=r.get("apollo_industry"),
                sector=r.get("sector") or "",
                tam=r.get("tam"), origen=r.get("origen"),
                sucursales=int(r.get("sucursales")) if r.get("sucursales") else None,
                estados=r.get("estados"), country=r.get("country") or "Mexico",
                seniorities=r.get("seniorities"), departments=r.get("departments"),
                priority_titles=r.get("priority_titles"),
                exclude_keywords=r.get("exclude_keywords"),
                requires_manual=str(r.get("requires_manual", "")).lower() in ("si", "sí", "yes", "1", "true"),
                notes=r.get("notes"), unit=unit, status="pending",
            )
            db.session.add(row)
            imported += 1
        except Exception as e:
            errors += 1
            error_rows.append({"row": r, "reason": str(e)})
    db.session.commit()
    return jsonify({"imported": imported, "skipped": skipped, "errors": errors,
                    "total": len(rows), "errorRows": error_rows[:10]})


# ── Lemlist webhook receiver ───────────────────────────────────────


_LEMLIST_STATE_MAP = {
    "emailsReplied":           ("interesado", "Replied on email"),
    "linkedinReplied":         ("interesado", "Replied on LinkedIn"),
    "meetingBooked":           ("interesado", "Meeting booked"),
    "emailsBounced":           ("rebote",     "Email bounced"),
    "emailsUnsubscribed":      ("no_interesado", "Unsubscribed"),
    "campaignFinishedNoReply": ("sin_respuesta", "Sequence completed"),
}


@lemlist_webhook_bp.route("/lemlist", methods=["POST"])
def lemlist_webhook():
    """Recibe eventos Lemlist y mapea a lead_state. Idempotente."""
    body = request.get_json(silent=True) or {}
    evt_type = body.get("type")
    email = body.get("email")
    if not email:
        return jsonify({"ok": True, "reason": "no_email"})

    seq = (
        SdrDirSequence.query.filter(SdrDirSequence.contact_email == email)
        .order_by(SdrDirSequence.id.desc()).first()
    )
    if not seq:
        return jsonify({"ok": True, "reason": "no_sequence"})

    mapped = _LEMLIST_STATE_MAP.get(evt_type)
    if mapped:
        new_state, reason = mapped
        seq.lead_state = new_state
        seq.state_reason = reason
        seq.state_changed_at = datetime.now(timezone.utc)

        if new_state == "interesado" and seq.status == "activa":
            seq.status = "respondio"
            seq.paused_reason = "email_reply"
            seq.next_action_at = None
            seq.last_action_at = datetime.now(timezone.utc)
            if seq.lemlist_campaign_id:
                try:
                    sdrdir.lemlist_pause_campaign(seq.lemlist_campaign_id)
                except Exception:
                    pass
        elif new_state == "rebote":
            seq.notes = (seq.notes or "") + " [EMAIL BOUNCED]"
        elif new_state == "no_interesado" and seq.status == "activa":
            seq.status = "descartado"
            seq.paused_reason = "unsubscribed"
            seq.next_action_at = None
        db.session.commit()
    return jsonify({"ok": True})


# ── Engine control ─────────────────────────────────────────────────


@sdr_directivo_bp.route("/engine/status", methods=["GET"])
def engine_status():
    unit = (request.args.get("unit") or "aromatex").lower()
    config = SdrDirEngineConfig.query.filter_by(unit=unit).first()
    last_run = (
        SdrDirEngineRun.query.filter_by(unit=unit)
        .order_by(SdrDirEngineRun.id.desc()).first()
    )
    master_stats_rows = (
        db.session.query(SdrDirMasterCompany.status, func.count())
        .filter(SdrDirMasterCompany.unit == unit)
        .group_by(SdrDirMasterCompany.status).all()
    )
    seq_in_lemlist = (
        SdrDirSequence.query.filter_by(unit=unit)
        .filter(SdrDirSequence.lemlist_campaign_id.isnot(None))
        .filter(SdrDirSequence.master_company_id.isnot(None)).count()
    )
    last_30d_credits = (
        db.session.query(func.coalesce(func.sum(SdrDirEngineRun.lusha_credits_used), 0))
        .filter(SdrDirEngineRun.unit == unit)
        .filter(SdrDirEngineRun.started_at >= datetime.now(timezone.utc) - timedelta(days=30))
        .scalar() or 0
    )
    lusha_balance = None
    try:
        lusha_balance = sdrdir.get_lusha_balance().get("balance")
    except Exception:
        pass
    for svc in ("lusha", "apollo"):
        try:
            engine.ensure_monthly_row(unit, svc)
        except Exception:
            pass
    try:
        engine.sync_lusha_credits_from_api(unit)
    except Exception:
        pass
    ym = engine.current_year_month()
    monthly = (
        SdrDirCreditsMonthly.query.filter_by(unit=unit, year_month=ym)
        .order_by(SdrDirCreditsMonthly.service).all()
    )
    return jsonify({
        "config": config.to_dict() if config else None,
        "lastRun": last_run.to_dict() if last_run else None,
        "masterStats": [{"status": s, "c": c} for s, c in master_stats_rows],
        "seqInLemlist": seq_in_lemlist,
        "last30dCredits": int(last_30d_credits or 0),
        "lushaBalance": lusha_balance,
        "monthlyCredits": [r.to_dict() for r in monthly],
        "year_month": ym,
    })


@sdr_directivo_bp.route("/engine/runs", methods=["GET"])
def engine_runs():
    unit = (request.args.get("unit") or "aromatex").lower()
    try:
        limit = min(int(request.args.get("limit") or 30), 100)
    except ValueError:
        limit = 30
    rows = (
        SdrDirEngineRun.query.filter_by(unit=unit)
        .order_by(SdrDirEngineRun.id.desc()).limit(limit).all()
    )
    return jsonify([r.to_dict() for r in rows])


@sdr_directivo_bp.route("/engine/config", methods=["PUT"])
def engine_config_put():
    data = request.get_json() or {}
    unit = (data.get("unit") or "aromatex").lower()
    config = SdrDirEngineConfig.query.filter_by(unit=unit).first()
    if not config:
        config = SdrDirEngineConfig(unit=unit, credits_limit=600 if unit else None) if False else SdrDirEngineConfig(unit=unit)
        db.session.add(config)

    allowed = ("enabled", "max_companies_per_day", "max_contacts_per_company",
               "max_lusha_credits_per_day", "min_lusha_balance_alert",
               "lemlist_master_campaign_id", "cron_hour", "cron_minute",
               "tam_a_enrich_phone", "tam_bc_enrich_phone",
               "tam_a_phones_per_company", "tam_bc_phones_per_company",
               "lusha_monthly_limit", "lusha_hard_cap", "lusha_alert_threshold")
    bool_fields = {"enabled", "tam_a_enrich_phone", "tam_bc_enrich_phone", "lusha_hard_cap"}
    touched = False
    for k in allowed:
        if k in data:
            v = bool(data[k]) if k in bool_fields else data[k]
            setattr(config, k, v)
            touched = True
    if not touched:
        return jsonify({"error": "No fields"}), 400
    db.session.commit()

    # Propagar cambios de límites Lusha al row del mes actual
    if any(k in data for k in ("lusha_monthly_limit", "lusha_hard_cap", "lusha_alert_threshold")):
        ym = engine.current_year_month()
        row = SdrDirCreditsMonthly.query.filter_by(unit=unit, service="lusha", year_month=ym).first()
        if row:
            if "lusha_monthly_limit" in data:
                row.credits_limit = int(data["lusha_monthly_limit"]) or 600
            if "lusha_hard_cap" in data:
                row.hard_cap = bool(data["lusha_hard_cap"])
            if "lusha_alert_threshold" in data:
                row.alert_threshold = float(data["lusha_alert_threshold"]) or 0.8
            row.updated_at = datetime.now(timezone.utc)
            db.session.commit()

    return jsonify(config.to_dict())


@sdr_directivo_bp.route("/engine/toggle", methods=["POST"])
def engine_toggle():
    data = request.get_json() or {}
    unit = (data.get("unit") or "aromatex").lower()
    enabled = bool(data.get("enabled"))
    config = SdrDirEngineConfig.query.filter_by(unit=unit).first()
    if not config:
        return jsonify({"error": "no_config_for_unit"}), 400
    config.enabled = enabled
    config.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(config.to_dict())


@sdr_directivo_bp.route("/engine/run-now", methods=["POST"])
def engine_run_now():
    data = request.get_json() or {}
    unit = (data.get("unit") or "aromatex").lower()
    dry_run = bool(data.get("dry_run"))
    override_max = data.get("override_max_companies")
    try:
        override_max = int(override_max) if override_max else None
    except (ValueError, TypeError):
        override_max = None
    try:
        result = engine.engine_run_daily_batch(
            unit=unit, dry_run=dry_run, force=True,
            override_max_companies=override_max,
        )
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


# ── Credits status / estimate / config ─────────────────────────────


@sdr_directivo_bp.route("/credits/status", methods=["GET"])
def credits_status():
    unit = (request.args.get("unit") or "aromatex").lower()
    ym = engine.current_year_month()
    for svc in ("lusha", "apollo"):
        try:
            engine.ensure_monthly_row(unit, svc)
        except Exception:
            pass
    lusha_sync = None
    try:
        lusha_sync = engine.sync_lusha_credits_from_api(unit)
    except Exception:
        pass
    rows = (
        SdrDirCreditsMonthly.query.filter_by(unit=unit, year_month=ym)
        .order_by(SdrDirCreditsMonthly.service).all()
    )
    today = datetime.now(timezone.utc).date()
    if today.month == 12:
        reset_at = today.replace(year=today.year + 1, month=1, day=1)
    else:
        reset_at = today.replace(month=today.month + 1, day=1)
    return jsonify({
        "unit": unit, "year_month": ym, "reset_at": reset_at.isoformat(),
        "services": [r.to_dict() for r in rows], "lusha_sync": lusha_sync,
    })


@sdr_directivo_bp.route("/credits/estimate", methods=["GET"])
def credits_estimate():
    unit = (request.args.get("unit") or "aromatex").lower()
    try:
        n = max(1, min(int(request.args.get("n") or 10), 200))
    except ValueError:
        n = 10
    config = SdrDirEngineConfig.query.filter_by(unit=unit).first()
    if not config:
        return jsonify({"error": "no_config_for_unit"}), 400
    companies = (
        SdrDirMasterCompany.query
        .filter_by(unit=unit, status="pending", requires_manual=False)
        .order_by(SdrDirMasterCompany.priority_order.asc()).limit(n).all()
    )
    breakdown = {"A": 0, "B": 0, "C": 0, "other": 0}
    phones_a, phones_bc = 0, 0
    for co in companies:
        t = (co.tam or "").upper()
        if t == "A":
            breakdown["A"] += 1
            if config.tam_a_enrich_phone:
                phones_a += config.tam_a_phones_per_company or 0
        elif t == "B":
            breakdown["B"] += 1
            if config.tam_bc_enrich_phone:
                phones_bc += config.tam_bc_phones_per_company or 0
        elif t == "C":
            breakdown["C"] += 1
            if config.tam_bc_enrich_phone:
                phones_bc += config.tam_bc_phones_per_company or 0
        else:
            breakdown["other"] += 1
    est_lusha = phones_a + phones_bc
    ym = engine.current_year_month()
    lusha_row = SdrDirCreditsMonthly.query.filter_by(unit=unit, service="lusha", year_month=ym).first()
    current = lusha_row.credits_used or 0 if lusha_row else 0
    limit = lusha_row.credits_limit if lusha_row else 600
    after = current + est_lusha
    return jsonify({
        "unit": unit, "requested_n": n, "sample_size": len(companies),
        "breakdown": breakdown,
        "phones_estimated": {"tam_a": phones_a, "tam_bc": phones_bc},
        "estimated_lusha_credits": est_lusha,
        "current_used": current, "limit": limit, "after_used": after,
        "will_exceed": (after > limit) and bool(lusha_row.hard_cap if lusha_row else False),
        "threshold_pct": (after / limit) if limit else 0,
    })


@sdr_directivo_bp.route("/credits/config", methods=["PUT"])
def credits_config_put():
    data = request.get_json() or {}
    unit = (data.get("unit") or "aromatex").lower()
    service = (data.get("service") or "lusha").lower()
    ym = engine.current_year_month()
    row = engine.ensure_monthly_row(unit, service)
    touched = False
    if "credits_limit" in data:
        row.credits_limit = int(data["credits_limit"]) or 0
        touched = True
    if "hard_cap" in data:
        row.hard_cap = bool(data["hard_cap"])
        touched = True
    if "alert_threshold" in data:
        row.alert_threshold = float(data["alert_threshold"]) or 0.8
        touched = True
    if "credits_used" in data:
        row.credits_used = int(data["credits_used"]) or 0
        touched = True
    for k in ("alerted_80", "alerted_95", "alerted_100"):
        if k in data:
            setattr(row, k, bool(data[k]))
            touched = True
    if not touched:
        return jsonify({"error": "No fields"}), 400
    row.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(row.to_dict())
