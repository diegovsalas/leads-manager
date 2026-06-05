# blueprints/accounts.py
"""
Accounts (empresas) y Contacts (personas) — entidades reutilizables tipo
Zoho/HubSpot. Endpoints bajo /api/accounts/ y /api/contacts/.
"""
from datetime import datetime
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func, or_

from extensions import db
from models import Account, Contact, Lead, Oportunidad, CSAccount, Cotizacion

accounts_bp = Blueprint("accounts", __name__)
contacts_bp = Blueprint("contacts", __name__)


def _current_user_id():
    # Solo usuario_id (FK a usuarios). user_id es FK a users → FK violation si se usa como owner_id.
    return session.get("usuario_id")


# ── ACCOUNTS ───────────────────────────────────────────────────────


@accounts_bp.route("/", methods=["GET"])
def list_accounts():
    """Filtros: ?search= (nombre/rfc/comercial) ?owner= ?is_cliente= ?estado="""
    q = Account.query
    search = (request.args.get("search") or "").strip()
    owner = request.args.get("owner")
    is_cliente = request.args.get("is_cliente")
    estado = request.args.get("estado")

    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Account.nombre.ilike(like),
            Account.nombre_comercial.ilike(like),
            Account.rfc.ilike(like),
        ))
    if owner:
        q = q.filter(Account.owner_id == owner)
    if is_cliente in ("true", "1", "yes"):
        q = q.filter(Account.is_cliente.is_(True))
    if estado:
        q = q.filter(Account.estado == estado)

    rows = q.order_by(Account.nombre.asc()).limit(200).all()
    return jsonify([r.to_dict() for r in rows])


@accounts_bp.route("/search", methods=["GET"])
def search_accounts():
    """Autocomplete rápido. ?q= mínimo 2 chars, max 15 resultados."""
    q_str = (request.args.get("q") or "").strip()
    if len(q_str) < 2:
        return jsonify([])
    like = f"%{q_str}%"
    rows = (
        Account.query
        .filter(or_(
            Account.nombre.ilike(like),
            Account.nombre_comercial.ilike(like),
            Account.rfc.ilike(like),
        ))
        .order_by(Account.nombre.asc()).limit(15).all()
    )
    return jsonify([{
        "id": str(r.id), "nombre": r.nombre, "rfc": r.rfc,
        "nombre_comercial": r.nombre_comercial,
        "is_cliente": r.is_cliente,
    } for r in rows])


@accounts_bp.route("/<uuid:account_id>", methods=["GET"])
def get_account(account_id):
    acc = db.session.get(Account, account_id)
    if not acc:
        return jsonify({"error": "Account no encontrada"}), 404
    payload = acc.to_dict()
    # Include linked entities counts and lists
    leads = (
        Lead.query.filter(Lead.account_id == acc.id)
        .order_by(Lead.fecha_creacion.desc()).all()
    )
    opps = Oportunidad.query.filter(Oportunidad.account_id == acc.id).all()
    contacts = Contact.query.filter(Contact.account_id == acc.id).all()
    cotizaciones = []
    if leads:
        cotizaciones = (
            Cotizacion.query
            .filter(Cotizacion.lead_id.in_([l.id for l in leads]))
            .order_by(Cotizacion.fecha.desc()).all()
        )
    payload["counts"] = {
        "leads": len(leads), "oportunidades": len(opps),
        "contactos": len(contacts), "cotizaciones": len(cotizaciones),
    }
    payload["leads"] = [l.to_dict() for l in leads]
    payload["oportunidades"] = [o.to_dict() for o in opps]
    payload["contactos"] = [c.to_dict() for c in contacts]
    payload["cotizaciones"] = [c.to_dict() for c in cotizaciones]
    payload["valor_pipe_abierto"] = sum(
        float(o.valor or 0) for o in opps
        if o.etapa and o.etapa.value not in ("Cerrado Ganado", "Cerrado Perdido")
    )
    payload["valor_ganado_total"] = sum(
        float(o.valor or 0) for o in opps
        if o.etapa and o.etapa.value == "Cerrado Ganado"
    )

    # MRR y datos de Customer Success (si la Account está enlazada a CSAccount)
    payload["cs"] = None
    if acc.cs_account_id:
        cs = db.session.get(CSAccount, acc.cs_account_id)
        if cs:
            payload["cs"] = {
                "id": str(cs.id),
                "client_id": cs.client_id or "",
                "mrr": float(cs.mrr or 0),
                "arr_proyectado": float(cs.arr_proyectado or 0),
                "sucursales": cs.sucursales or 0,
                "unidades_contratadas": cs.unidades_contratadas or "",
                "tier": cs.tier or "",
                "nps": cs.nps,
                "pulso": cs.pulso,
            }
    return jsonify(payload)


@accounts_bp.route("/", methods=["POST"])
def create_account():
    data = request.get_json() or {}
    if not data.get("nombre"):
        return jsonify({"error": "nombre requerido"}), 400
    # Idempotente: si existe el RFC, devolver el existente
    rfc = (data.get("rfc") or "").strip().upper() or None
    if rfc:
        existing = Account.query.filter(Account.rfc == rfc).first()
        if existing:
            return jsonify(existing.to_dict()), 200
    # Idempotente por nombre exacto también
    nombre = data["nombre"].strip()
    existing = Account.query.filter(func.lower(Account.nombre) == nombre.lower()).first()
    if existing:
        return jsonify(existing.to_dict()), 200

    acc = Account(
        nombre=nombre,
        nombre_comercial=data.get("nombre_comercial"),
        rfc=rfc,
        industria=data.get("industria"),
        tamano=data.get("tamano"),
        num_sucursales=data.get("num_sucursales"),
        website=data.get("website"),
        telefono=data.get("telefono"),
        direccion=data.get("direccion"),
        ciudad=data.get("ciudad"),
        estado=data.get("estado"),
        pais=data.get("pais") or "México",
        owner_id=data.get("owner_id") or _current_user_id(),
        is_cliente=bool(data.get("is_cliente", False)),
        notas=data.get("notas"),
        cs_account_id=data.get("cs_account_id"),
        zoho_account_id=data.get("zoho_account_id"),
        customer_master_id=data.get("customer_master_id"),
    )
    db.session.add(acc)
    db.session.commit()
    return jsonify(acc.to_dict()), 201


@accounts_bp.route("/<uuid:account_id>", methods=["PATCH"])
def update_account(account_id):
    acc = db.session.get(Account, account_id)
    if not acc:
        return jsonify({"error": "Account no encontrada"}), 404
    data = request.get_json() or {}
    for fld in ("nombre", "nombre_comercial", "rfc", "industria", "tamano",
                "num_sucursales", "website", "telefono", "direccion",
                "ciudad", "estado", "pais", "owner_id", "is_cliente",
                "notas", "cs_account_id", "zoho_account_id",
                "customer_master_id"):
        if fld in data:
            setattr(acc, fld, data[fld])
    db.session.commit()
    return jsonify(acc.to_dict())


@accounts_bp.route("/<uuid:account_id>", methods=["DELETE"])
def delete_account(account_id):
    acc = db.session.get(Account, account_id)
    if not acc:
        return jsonify({"error": "Account no encontrada"}), 404
    # Detach FKs en Leads y Oportunidades antes de eliminar (suave)
    Lead.query.filter(Lead.account_id == acc.id).update({"account_id": None}, synchronize_session=False)
    Oportunidad.query.filter(Oportunidad.account_id == acc.id).update({"account_id": None}, synchronize_session=False)
    db.session.delete(acc)
    db.session.commit()
    return jsonify({"ok": True})


# ── CONTACTS ───────────────────────────────────────────────────────


@contacts_bp.route("/", methods=["GET"])
def list_contacts():
    """Filtros: ?account_id= ?search= (nombre/email)"""
    q = Contact.query
    account_id = request.args.get("account_id")
    search = (request.args.get("search") or "").strip()
    if account_id:
        q = q.filter(Contact.account_id == account_id)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Contact.nombre.ilike(like),
            Contact.apellido.ilike(like),
            Contact.email.ilike(like),
            Contact.telefono.like(f"%{search}%"),
        ))
    rows = q.order_by(Contact.is_primary.desc(), Contact.nombre.asc()).limit(300).all()
    return jsonify([r.to_dict() for r in rows])


@contacts_bp.route("/search", methods=["GET"])
def search_contacts():
    q_str = (request.args.get("q") or "").strip()
    if len(q_str) < 2:
        return jsonify([])
    like = f"%{q_str}%"
    rows = (
        Contact.query.filter(or_(
            Contact.nombre.ilike(like),
            Contact.apellido.ilike(like),
            Contact.email.ilike(like),
        )).order_by(Contact.nombre.asc()).limit(15).all()
    )
    return jsonify([{
        "id": str(r.id), "nombre_completo": r.nombre_completo,
        "email": r.email, "telefono": r.telefono,
        "puesto": r.puesto,
        "account_id": str(r.account_id) if r.account_id else None,
        "account_nombre": r.account.nombre if r.account else None,
    } for r in rows])


@contacts_bp.route("/<uuid:contact_id>", methods=["GET"])
def get_contact(contact_id):
    c = db.session.get(Contact, contact_id)
    if not c:
        return jsonify({"error": "Contact no encontrado"}), 404
    return jsonify(c.to_dict())


@contacts_bp.route("/", methods=["POST"])
def create_contact():
    data = request.get_json() or {}
    if not data.get("nombre"):
        return jsonify({"error": "nombre requerido"}), 400
    # Idempotente por email si viene
    email = (data.get("email") or "").strip().lower() or None
    if email:
        existing = Contact.query.filter(func.lower(Contact.email) == email).first()
        if existing:
            return jsonify(existing.to_dict()), 200

    # Si is_primary=True, desmarcar otros primary del mismo account
    account_id = data.get("account_id")
    if data.get("is_primary") and account_id:
        Contact.query.filter(
            Contact.account_id == account_id,
            Contact.is_primary.is_(True),
        ).update({"is_primary": False}, synchronize_session=False)

    c = Contact(
        nombre=data["nombre"], apellido=data.get("apellido"),
        email=email, telefono=data.get("telefono"),
        whatsapp=data.get("whatsapp"), puesto=data.get("puesto"),
        departamento=data.get("departamento"), linkedin=data.get("linkedin"),
        account_id=account_id,
        is_primary=bool(data.get("is_primary", False)),
        notas=data.get("notas"),
    )
    db.session.add(c)
    db.session.commit()
    return jsonify(c.to_dict()), 201


@contacts_bp.route("/<uuid:contact_id>", methods=["PATCH"])
def update_contact(contact_id):
    c = db.session.get(Contact, contact_id)
    if not c:
        return jsonify({"error": "Contact no encontrado"}), 404
    data = request.get_json() or {}

    # Si pasa is_primary=True, desmarcar otros del mismo account
    if data.get("is_primary") and c.account_id:
        Contact.query.filter(
            Contact.account_id == c.account_id,
            Contact.id != c.id,
            Contact.is_primary.is_(True),
        ).update({"is_primary": False}, synchronize_session=False)

    for fld in ("nombre", "apellido", "email", "telefono", "whatsapp",
                "puesto", "departamento", "linkedin", "account_id",
                "is_primary", "notas"):
        if fld in data:
            setattr(c, fld, data[fld])
    db.session.commit()
    return jsonify(c.to_dict())


@contacts_bp.route("/<uuid:contact_id>", methods=["DELETE"])
def delete_contact(contact_id):
    c = db.session.get(Contact, contact_id)
    if not c:
        return jsonify({"error": "Contact no encontrado"}), 404
    Lead.query.filter(Lead.contact_id == c.id).update({"contact_id": None}, synchronize_session=False)
    Oportunidad.query.filter(Oportunidad.contact_id == c.id).update({"contact_id": None}, synchronize_session=False)
    db.session.delete(c)
    db.session.commit()
    return jsonify({"ok": True})
