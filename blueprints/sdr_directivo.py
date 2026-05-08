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
    Lead, OrigenLead, EtapaPipeline,
)
import sdr_directivo as sdrdir

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
    """Acepta JSON {rows: [...]}. CSV body se cubre en Round 2d."""
    unit = (request.args.get("unit") or "aromatex").lower()
    payload = request.get_json(silent=True) or {}
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return jsonify({"error": "Send JSON {rows:[...]}"}), 400

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
