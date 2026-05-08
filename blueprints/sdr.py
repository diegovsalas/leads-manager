# blueprints/sdr.py
"""
SDR (Sales Development Rep) — endpoints de prospección y asignación.
Rutas bajo /api/sdr/.

Port de los endpoints /api/sdr/* de vendedores.cloud (server.js ~668-784).
La lógica de búsqueda externa vive en sdr_prospector.
"""
import re
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session
from sqlalchemy import or_, func

from extensions import db
from models import SdrResult, Lead, OrigenLead, EtapaPipeline
import sdr_prospector

sdr_bp = Blueprint("sdr", __name__)


def _norm_name(s: str) -> str:
    return (s or "").lower().strip()


def _digits_tail10(s: str) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


@sdr_bp.route("/search", methods=["GET"])
def search():
    """GET /api/sdr/search?state=&giro=&unit=&limit=
    Llama al prospector, deduplica contra sdr_results y leads,
    persiste corporativos y nuevos, devuelve los locales únicos."""
    state = request.args.get("state") or "Nuevo Leon"
    giro = request.args.get("giro") or "restaurantes"
    unit = request.args.get("unit") or "pestex"
    try:
        limit = int(request.args.get("limit") or 10)
    except ValueError:
        limit = 10

    try:
        raw = sdr_prospector.search_prospects(state, giro, unit, limit)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    local = raw.get("local") or []
    corporates = raw.get("corporates") or []

    # Persistir corporativos (status='corporativo') si no existen
    corp_saved = 0
    for c in corporates:
        name_norm = _norm_name(c.get("name"))
        if not name_norm:
            continue
        exists = (
            db.session.query(SdrResult.id)
            .filter(func.lower(SdrResult.business_name) == name_norm)
            .first()
        )
        if exists:
            continue
        row = SdrResult(
            business_name=c.get("name") or "",
            instagram_handle=c.get("instagram_handle") or "",
            whatsapp=c.get("whatsapp") or "",
            address=c.get("address") or "",
            state=state, city=c.get("city") or "",
            rating=c.get("rating"), reviews=c.get("reviews") or 0,
            branches=c.get("branches") or 1,
            source=c.get("source") or "", unit=unit,
            meta_ad_url=c.get("meta_ad_url") or "",
            facebook_url=c.get("facebook_url") or "",
            website=c.get("website") or "",
            maps_url=c.get("maps_url") or "",
            wa_source=c.get("wa_source") or "",
            status="corporativo",
        )
        db.session.add(row)
        corp_saved += 1
    db.session.commit()

    # Deduplicar locales contra sdr_results y leads
    filtered = []
    for r in local:
        name_norm = _norm_name(r.get("name"))
        phone_tail = _digits_tail10(r.get("whatsapp") or r.get("phone") or "")

        # Skip si ya existe en sdr_results como asignado/descartado/corporativo
        if name_norm:
            seen = (
                db.session.query(SdrResult.id, SdrResult.status)
                .filter(func.lower(SdrResult.business_name) == name_norm)
                .filter(SdrResult.status.in_(["asignado", "descartado", "corporativo"]))
                .first()
            )
            if seen:
                continue

        if len(phone_tail) >= 10:
            seen_phone = (
                db.session.query(SdrResult.id)
                .filter(SdrResult.whatsapp.like(f"%{phone_tail}"))
                .filter(SdrResult.status.in_(["asignado", "descartado", "corporativo"]))
                .first()
            )
            if seen_phone:
                continue

        # Marcar como duplicate si ya está como Lead
        if name_norm:
            existing_lead = (
                db.session.query(Lead.id)
                .filter(func.lower(Lead.empresa_nombre) == name_norm)
                .first()
            )
            if existing_lead:
                r["duplicate"] = True
                r["duplicate_type"] = "lead"
        if not r.get("duplicate") and len(phone_tail) >= 10:
            existing_phone = (
                db.session.query(Lead.id)
                .filter(Lead.telefono.like(f"%{phone_tail}"))
                .first()
            )
            if existing_phone:
                r["duplicate"] = True
                r["duplicate_type"] = "lead"

        # Si ya existe como 'nuevo' en sdr_results, reusar el ID
        existing_nuevo = (
            db.session.query(SdrResult)
            .filter(func.lower(SdrResult.business_name) == name_norm)
            .filter(SdrResult.status == "nuevo")
            .first()
        ) if name_norm else None
        if existing_nuevo:
            r["sdr_id"] = existing_nuevo.id

        # Si es genuinamente nuevo, persistir como 'nuevo'
        if not existing_nuevo and not r.get("duplicate"):
            row = SdrResult(
                business_name=r.get("name") or "",
                instagram_handle=r.get("instagram_handle") or "",
                whatsapp=r.get("whatsapp") or "",
                address=r.get("address") or "",
                state=state, city=r.get("city") or "",
                rating=r.get("rating"), reviews=r.get("reviews") or 0,
                branches=r.get("branches") or 1,
                source=r.get("source") or "", unit=unit,
                meta_ad_url=r.get("meta_ad_url") or "",
                facebook_url=r.get("facebook_url") or "",
                website=r.get("website") or "",
                maps_url=r.get("maps_url") or "",
                wa_source=r.get("wa_source") or "",
                status="nuevo",
            )
            db.session.add(row)
            db.session.flush()
            r["sdr_id"] = row.id

        filtered.append(r)
    db.session.commit()

    return jsonify({
        "results": filtered,
        "corporates_filtered": len(corporates),
        "corporates_saved": corp_saved,
    })


@sdr_bp.route("/assign", methods=["POST"])
def assign():
    """POST /api/sdr/assign — marca SdrResult asignado y crea un Lead."""
    data = request.get_json() or {}
    sdr_id = data.get("sdr_id")
    business_name = data.get("business_name") or ""
    whatsapp = data.get("whatsapp") or ""
    address = data.get("address") or ""
    state = data.get("state") or ""
    city = data.get("city") or ""
    unit = data.get("unit") or ""
    instagram_handle = data.get("instagram_handle") or ""

    if not whatsapp:
        return jsonify({"error": "whatsapp requerido para crear Lead"}), 400

    # Marcar SdrResult o crear+marcar
    if sdr_id:
        sdr = db.session.get(SdrResult, int(sdr_id))
        if sdr:
            sdr.status = "asignado"
            sdr.assigned_at = datetime.now(timezone.utc)
    else:
        new_sdr = SdrResult(
            business_name=business_name, instagram_handle=instagram_handle,
            whatsapp=whatsapp, address=address, state=state, city=city,
            unit=unit, status="asignado",
            assigned_at=datetime.now(timezone.utc),
        )
        db.session.add(new_sdr)

    # Crear Lead
    notes_parts = []
    if address:
        notes_parts.append(address)
    if instagram_handle:
        notes_parts.append(f"@{instagram_handle}")
    notes = " | ".join(notes_parts) if notes_parts else None

    detail_parts = []
    if instagram_handle:
        detail_parts.append(f"IG: @{instagram_handle}")
    detail_parts.append("SDR Prospector")
    detail = " | ".join(detail_parts)

    try:
        lead = Lead(
            telefono=whatsapp,
            nombre=business_name,
            empresa_nombre=business_name,
            origen=OrigenLead.PROSPECCION,
            tipo_cliente=detail,
            estado_cliente=state or None,
            etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        )
        db.session.add(lead)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"crear lead falló: {e}"}), 500

    return jsonify({"ok": True, "lead_id": str(lead.id)})


@sdr_bp.route("/discard", methods=["POST"])
def discard():
    """POST /api/sdr/discard — marca un SdrResult como descartado."""
    data = request.get_json() or {}
    sdr_id = data.get("sdr_id")
    if sdr_id:
        sdr = db.session.get(SdrResult, int(sdr_id))
        if sdr:
            sdr.status = "descartado"
            db.session.commit()
    return jsonify({"ok": True})


@sdr_bp.route("/templates", methods=["GET"])
def templates():
    """GET /api/sdr/templates — devuelve los templates de WhatsApp por unidad."""
    return jsonify(sdr_prospector.get_templates())


@sdr_bp.route("/wa-link", methods=["GET"])
def wa_link():
    """GET /api/sdr/wa-link?name=&phone=&unit= — construye el link wa.me con
    el mensaje pre-rellenado del template."""
    name = request.args.get("name") or ""
    phone = request.args.get("phone") or ""
    unit = request.args.get("unit") or "aromatex"
    if not phone:
        return jsonify({"error": "phone requerido"}), 400
    return jsonify({"url": sdr_prospector.generate_whatsapp_link(name, phone, unit)})
