# blueprints/oportunidades.py
"""
Oportunidades (Deals) — entidad pre-cierre. Reemplaza Zoho Deals para que
leads-manager pueda operar sin Zoho.

Endpoints bajo /api/oportunidades/.
"""
from datetime import datetime, timezone
from decimal import Decimal
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func, or_

import cierre_evidencia as CE
from extensions import db
from models import (
    Oportunidad, EtapaOportunidad, PROBABILIDAD_OPORTUNIDAD,
    Lead, EtapaPipeline, Usuario, Contact, Account, Sale,
)

oportunidades_bp = Blueprint("oportunidades", __name__)


# ── Helpers ────────────────────────────────────────────────────────


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_etapa(s):
    if not s:
        return None
    try:
        return EtapaOportunidad(s)
    except ValueError:
        return None


def _current_user_id():
    # Solo usuario_id (FK a tabla `usuarios`). user_id es FK a `users` y
    # rompe la FK violation cuando se usa como propietario_id (ver commit e71dd92).
    return session.get("usuario_id")


def _valid_user_id(uid):
    """Devuelve el uid si existe en la tabla usuarios. Si la sesión trae
    un UUID stale (usuario borrado, login por otro sistema, etc.) devuelve
    None para evitar FK violations."""
    if not uid:
        return None
    try:
        if db.session.get(Usuario, uid):
            return uid
    except Exception:
        pass
    return None


def _to_decimal(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (ValueError, TypeError):
        return None


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "si", "sí")


def _unit_from_marca(marca):
    raw = (marca or "").strip()
    if not raw:
        return "sin_un"
    low = raw.lower().replace(" ", "_")
    if low in ("aromatex_home", "aromatexhome"):
        return "aromatex"
    if low in ("aromatex", "pestex", "weldex", "nexo"):
        return low
    return low[:40]


def _account_ids_con_venta():
    """Cuentas que ya cerraron al menos una venta activa.

    Es la definición operativa de «cliente existente», y por lo tanto de
    upsell: venderle algo más a quien ya nos compra.

    NO se usa Account.is_cliente. Esa bandera solo se prende cuando cierra
    una OPORTUNIDAD, nunca cuando cierra un lead — y como todo el flujo
    comercial corre sobre leads, está prácticamente vacía. Filtrar por ella
    dejaría el tablero en blanco. La venta cerrada sí es un hecho, venga por
    donde venga.
    """
    ids = set()
    por_opp = (
        db.session.query(Oportunidad.account_id)
        .join(Sale, Sale.opportunity_id == Oportunidad.id)
        .filter(Oportunidad.account_id.isnot(None), Sale.status == "activa")
        .distinct()
    )
    por_lead = (
        db.session.query(Lead.account_id)
        .join(Sale, Sale.lead_id == Lead.id)
        .filter(Lead.account_id.isnot(None), Sale.status == "activa")
        .distinct()
    )
    for consulta in (por_opp, por_lead):
        ids.update(aid for (aid,) in consulta if aid)
    return ids


def _apply_upsell_scope(query):
    """?solo_upsell=1 → solo tratos sobre cuentas que ya compraron.

    Con el conjunto vacío el filtro deja el tablero en blanco, que es lo
    correcto: significa que todavía no hay clientes a quienes expandir.
    """
    if not _truthy(request.args.get("solo_upsell")):
        return query
    return query.filter(Oportunidad.account_id.in_(_account_ids_con_venta()))


def _apply_owner_scope(query):
    """Un vendedor ve sus upsells; dirección ve todos.

    Quien detecta la expansión es quien atiende la cuenta, así que el
    tablero tiene que servirle a él y no solo a gerencia.
    """
    from blueprints.auth import get_vendedor_filter
    uid = get_vendedor_filter()
    if not uid:
        return query
    return query.filter(Oportunidad.propietario_id == uid)


def _apply_role_un_scope(query):
    """Aplica alcance de UN del rol logueado a oportunidades."""
    from blueprints.auth import effective_un_from_request
    from un_filter import normalizar_un
    scoped_un = effective_un_from_request(request.args.get("marca") or request.args.get("un"))
    canon = normalizar_un(scoped_un)
    if not canon:
        return query
    aliases = {
        "Aromatex": ("aromatex", "aromatex home", "aromatex_home", "aromatexhome"),
        "Pestex": ("pestex",),
        "Weldex": ("weldex",),
        "Nexo": ("nexo",),
    }.get(canon, ())
    return query.filter(or_(
        Oportunidad.marca_interes.is_(None),
        Oportunidad.marca_interes == "",
        func.lower(Oportunidad.marca_interes).in_(aliases),
    ))


def _calc_commission(sale_type: str, commission_type: str | None,
                     monthly_amount: float, total_amount: float) -> tuple[float, float]:
    rate = 1.0 if commission_type == "autogenerado" else 0.5
    if sale_type in ("suscripcion_nueva", "upsell"):
        return rate, (monthly_amount or 0) * rate
    return rate, (total_amount or 0) * 0.08


def _find_duplicate_open_opportunity(account_id=None, lead_id=None, marca=None, empresa=None,
                                     sitio=None, exclude_id=None):
    """Evita duplicar el mismo deal abierto para una empresa/UN/sucursal.

    Se permite tener varias oportunidades para la misma empresa si son de
    distinta UN, de distinta sucursal, o si el caller manda allow_duplicate.

    FEAT-2026-09-08: el sitio entra a la clave. Clientes como Quick Learning
    se venden sucursal por sucursal — N deals abiertos, misma empresa, misma
    UN — y antes el segundo rebotaba con 409. La salida era mandar
    allow_duplicate=true siempre, lo que apagaba el guardia también para el
    caso que sí importa: dos vendedores trabajando la misma plaza sin saberlo.
    """
    empresa_norm = (empresa or "").strip().lower()
    sitio_norm = (sitio or "").strip().lower()
    if not account_id and not lead_id and not empresa_norm:
        return None

    q = Oportunidad.query.filter(
        Oportunidad.etapa.notin_([
            EtapaOportunidad.CIERRE_GANADO,
            EtapaOportunidad.CIERRE_PERDIDO,
        ])
    )
    if exclude_id:
        q = q.filter(Oportunidad.id != exclude_id)
    if account_id and empresa_norm:
        q = q.filter(or_(
            Oportunidad.account_id == account_id,
            func.lower(Oportunidad.empresa) == empresa_norm,
        ))
    elif account_id:
        q = q.filter(Oportunidad.account_id == account_id)
    else:
        filters = []
        if lead_id:
            filters.append(Oportunidad.lead_id == lead_id)
        if empresa_norm:
            filters.append(func.lower(Oportunidad.empresa) == empresa_norm)
        q = q.filter(or_(*filters))
    if marca:
        q = q.filter(func.lower(Oportunidad.marca_interes) == str(marca).lower())
    else:
        q = q.filter(or_(Oportunidad.marca_interes.is_(None), Oportunidad.marca_interes == ""))
    # Sin sitio en ninguno de los dos lados = el comportamiento de siempre.
    # Con sitio, solo choca contra la MISMA sucursal.
    if sitio_norm:
        q = q.filter(func.lower(func.coalesce(Oportunidad.sitio, "")) == sitio_norm)
    else:
        q = q.filter(or_(Oportunidad.sitio.is_(None), Oportunidad.sitio == ""))
    return q.order_by(Oportunidad.fecha_actualizacion.desc()).first()


def _sale_type_por_defecto(op, monthly):
    """suscripcion_nueva la PRIMERA vez que esta cuenta compra esta unidad;
    upsell de ahí en adelante.

    FEAT-2026-09-08: vender sucursal por sucursal genera N ventas sobre la
    misma cuenta. Con el default anterior las ocho entraban como
    'suscripcion_nueva' y la tasa de venta nueva se pagaba ocho veces sobre
    el mismo cliente. El vendedor puede seguir mandando sale_type explícito;
    esto solo cambia qué se asume cuando no lo manda.
    """
    if monthly <= 0:
        return "servicio_unico"
    if not op.account_id:
        return "suscripcion_nueva"
    ya_compro = (
        db.session.query(Sale.id)
        .join(Oportunidad, Sale.opportunity_id == Oportunidad.id)
        .filter(
            Oportunidad.account_id == op.account_id,
            Oportunidad.id != op.id,
            Sale.unit == _unit_from_marca(op.marca_interes),
            Sale.status == "activa",
        )
        .first()
    )
    return "upsell" if ya_compro else "suscripcion_nueva"


def _sync_sale_from_oportunidad(op):
    """Cierre ganado: refleja la oportunidad en Sales de forma idempotente."""
    if op.etapa != EtapaOportunidad.CIERRE_GANADO:
        return None
    if not op.id:
        db.session.flush()

    monthly = float(op.monthly_amount or 0)
    total = float(op.valor or 0)
    sale_type = op.sale_type or _sale_type_por_defecto(op, monthly)
    if sale_type in ("suscripcion_nueva", "upsell") and monthly <= 0:
        monthly = total
    sale_category = "recurrente" if sale_type in ("suscripcion_nueva", "upsell") or monthly > 0 else "eventual"
    commission_type = "lead_otorgado" if op.lead_id else "autogenerado"
    rate, amount = _calc_commission(sale_type, commission_type, monthly, total)

    sale = Sale.query.filter(Sale.opportunity_id == op.id).first()
    if not sale:
        sale = Sale(opportunity_id=op.id)
        db.session.add(sale)

    sale.lead_id = op.lead_id
    sale.user_id = op.propietario_id
    sale.unit = _unit_from_marca(op.marca_interes)
    sale.sale_type = sale_type
    sale.sale_category = sale_category
    sale.uen = op.marca_interes
    sale.monthly_amount = Decimal(str(monthly))
    sale.total_amount = Decimal(str(total))
    sale.commission_type = commission_type
    sale.commission_rate = Decimal(str(rate))
    sale.commission_amount = Decimal(str(amount))
    sale.status = "activa"

    if op.lead and op.lead.origen:
        sale.lead_source = op.lead.origen.value

    if op.account_id:
        acc = db.session.get(Account, op.account_id)
        if acc:
            acc.is_cliente = True

    if op.lead:
        op.lead.etapa_pipeline = EtapaPipeline.CIERRE_GANADO
        if op.valor and not op.lead.valor_estimado:
            op.lead.valor_estimado = op.valor

    return sale


# ── List + filters ────────────────────────────────────────────────


@oportunidades_bp.route("/", methods=["GET"])
def list_oportunidades():
    """Filtros: ?etapa=&marca=&propietario=&search="""
    q = Oportunidad.query
    etapa = _parse_etapa(request.args.get("etapa"))
    marca = request.args.get("marca")
    propietario = request.args.get("propietario")
    search = (request.args.get("search") or "").strip()

    if etapa:
        q = q.filter(Oportunidad.etapa == etapa)
    if marca:
        q = q.filter(Oportunidad.marca_interes == marca)
    if propietario:
        q = q.filter(Oportunidad.propietario_id == propietario)
    q = _apply_owner_scope(q)
    q = _apply_upsell_scope(q)
    if search:
        like = f"%{search}%"
        q = q.filter(or_(
            Oportunidad.nombre.ilike(like),
            Oportunidad.empresa.ilike(like),
            Oportunidad.contacto_nombre.ilike(like),
        ))

    q = _apply_role_un_scope(q)
    rows = q.order_by(Oportunidad.fecha_actualizacion.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@oportunidades_bp.route("/<uuid:opp_id>", methods=["GET"])
def get_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    return jsonify(op.to_dict())


@oportunidades_bp.route("/cuentas-upsell", methods=["GET"])
def cuentas_upsell():
    """Cuentas que ya compraron, para el buscador del modal de upsell.

    Existe aparte de /api/accounts/search porque ese devuelve prospectos
    también: si el vendedor eligiera uno, el trato se crearía y no
    aparecería en el tablero —queda filtrado por no ser cliente— sin que
    nada le explique por qué. Mejor no ofrecérselos.
    """
    term = (request.args.get("q") or "").strip()
    if len(term) < 3:
        return jsonify([])
    ids = _account_ids_con_venta()
    if not ids:
        return jsonify([])
    like = f"%{term}%"
    rows = (
        Account.query
        .filter(
            Account.id.in_(ids),
            or_(Account.nombre.ilike(like),
                Account.nombre_comercial.ilike(like),
                Account.rfc.ilike(like),
                Account.client_id.ilike(like)),
        )
        .order_by(Account.nombre)
        .limit(20)
        .all()
    )
    return jsonify([{
        "id": str(a.id), "nombre": a.nombre,
        "client_id": a.client_id or "", "rfc": a.rfc or "",
    } for a in rows])


# ── Kanban view: agrupado por etapa ───────────────────────────────


@oportunidades_bp.route("/kanban", methods=["GET"])
def kanban():
    """Devuelve oportunidades agrupadas por etapa para vista Kanban.
    Filtros opcionales: ?marca=&propietario="""
    marca = request.args.get("marca")
    propietario = request.args.get("propietario")
    q = Oportunidad.query
    if marca:
        q = q.filter(Oportunidad.marca_interes == marca)
    if propietario:
        q = q.filter(Oportunidad.propietario_id == propietario)
    q = _apply_role_un_scope(q)
    q = _apply_owner_scope(q)
    q = _apply_upsell_scope(q)
    rows = q.order_by(Oportunidad.fecha_actualizacion.desc()).all()

    grouped = {}
    for et in EtapaOportunidad:
        grouped[et.value] = {
            "etapa": et.value,
            "probabilidad_default": PROBABILIDAD_OPORTUNIDAD.get(et, 0),
            "items": [],
            "valor_total": 0.0,
            "valor_ponderado_total": 0.0,
            "count": 0,
        }
    for op in rows:
        key = op.etapa.value if op.etapa else None
        if key and key in grouped:
            d = op.to_dict()
            grouped[key]["items"].append(d)
            grouped[key]["valor_total"] += d["valor"]
            grouped[key]["valor_ponderado_total"] += d["valor_ponderado"]
            grouped[key]["count"] += 1

    # Sumario global
    abiertas = [op for op in rows if op.etapa not in
                (EtapaOportunidad.CIERRE_GANADO, EtapaOportunidad.CIERRE_PERDIDO)]
    total_abierto = sum(float(o.valor or 0) for o in abiertas)
    total_ponderado = sum(o.valor_ponderado for o in abiertas)
    ganadas = [op for op in rows if op.etapa == EtapaOportunidad.CIERRE_GANADO]
    perdidas = [op for op in rows if op.etapa == EtapaOportunidad.CIERRE_PERDIDO]

    return jsonify({
        "etapas": [grouped[e.value] for e in EtapaOportunidad],
        "summary": {
            "abiertas_count": len(abiertas),
            "abiertas_valor": total_abierto,
            "abiertas_valor_ponderado": round(total_ponderado, 2),
            "ganadas_count": len(ganadas),
            "ganadas_valor": sum(float(o.valor or 0) for o in ganadas),
            "perdidas_count": len(perdidas),
            "perdidas_valor": sum(float(o.valor or 0) for o in perdidas),
        },
    })


# ── Create ────────────────────────────────────────────────────────


@oportunidades_bp.route("/", methods=["POST"])
def create_oportunidad():
    """Crea una oportunidad. Si no se pasa propietario_id, asigna al
    usuario en sesión."""
    data = request.get_json() or {}
    if not data.get("nombre"):
        return jsonify({"error": "nombre es requerido"}), 400
    etapa = _parse_etapa(data.get("etapa")) or EtapaOportunidad.CALIFICACION

    # Auto-vincular/crear Contact si vienen datos suficientes y no se pasó contact_id
    contact_id = data.get("contact_id")
    account_id = data.get("account_id")
    empresa_str = (data.get("empresa") or "").strip()
    if not account_id and empresa_str:
        existing_acc = Account.query.filter(func.lower(Account.nombre) == empresa_str.lower()).first()
        if existing_acc:
            account_id = existing_acc.id
        else:
            new_acc = Account(
                nombre=empresa_str,
                estado=data.get("estado_cliente"),
                num_sucursales=data.get("num_sucursales"),
                owner_id=_valid_user_id(data.get("propietario_id") or _current_user_id()),
            )
            db.session.add(new_acc)
            db.session.flush()
            account_id = new_acc.id

    marca_interes = data.get("marca_interes")
    sitio_str = (data.get("sitio") or "").strip() or None
    if not _truthy(data.get("allow_duplicate")):
        dup = _find_duplicate_open_opportunity(
            account_id=account_id,
            lead_id=data.get("lead_id"),
            marca=marca_interes,
            empresa=empresa_str,
            sitio=sitio_str,
        )
        if dup:
            return jsonify({
                "error": ("Ya existe una oportunidad abierta para esta empresa/lead, "
                      "unidad de negocio y sucursal."),
                "duplicate": dup.to_dict(),
            }), 409

    contacto_nombre = (data.get("contacto_nombre") or "").strip()
    contacto_telefono = (data.get("contacto_telefono") or "").strip()
    contacto_email = (data.get("contacto_email") or "").strip().lower() or None
    if not contact_id and contacto_nombre and (contacto_telefono or contacto_email):
        # Dedup por email primero, después por teléfono dentro del mismo account
        existing_c = None
        if contacto_email:
            existing_c = Contact.query.filter(
                db.func.lower(Contact.email) == contacto_email
            ).first()
        if not existing_c and contacto_telefono:
            q_dup = Contact.query.filter(Contact.telefono == contacto_telefono)
            if account_id:
                q_dup = q_dup.filter(Contact.account_id == account_id)
            existing_c = q_dup.first()
        if existing_c:
            contact_id = existing_c.id
        else:
            new_c = Contact(
                nombre=contacto_nombre,
                telefono=contacto_telefono or None,
                email=contacto_email,
                account_id=account_id,
            )
            db.session.add(new_c)
            db.session.flush()
            contact_id = new_c.id

    op = Oportunidad(
        nombre=data["nombre"],
        empresa=empresa_str or data.get("empresa"),
        contacto_nombre=contacto_nombre or None,
        contacto_telefono=contacto_telefono or None,
        contacto_email=contacto_email,
        valor=_to_decimal(data.get("valor")) or Decimal("0"),
        moneda=data.get("moneda") or "MXN",
        fecha_cierre_esperada=_parse_date(data.get("fecha_cierre_esperada")),
        etapa=etapa,
        propietario_id=_valid_user_id(data.get("propietario_id") or _current_user_id()),
        marca_interes=marca_interes,
        estado_cliente=data.get("estado_cliente"),
        num_sucursales=data.get("num_sucursales"),
        sitio=sitio_str,
        monthly_amount=_to_decimal(data.get("monthly_amount")),
        sale_type=data.get("sale_type"),
        notas=data.get("notas"),
        lead_id=data.get("lead_id"),
        zoho_deal_id=data.get("zoho_deal_id"),
        account_id=account_id,
        contact_id=contact_id,
    )
    if "probabilidad" in data:
        try:
            op.probabilidad = max(0, min(100, int(data["probabilidad"])))
        except (ValueError, TypeError):
            pass
    if op.etapa == EtapaOportunidad.CIERRE_GANADO:
        err = _nace_cerrada_sin_respaldo(op.marca_interes)
        if err:
            db.session.rollback()
            return jsonify({"error": err, "requiere_evidencia": True,
                            "tipos": CE.TIPOS, "max_tipos": CE.MAX_TIPOS}), 422
    db.session.add(op)
    if op.etapa == EtapaOportunidad.CIERRE_GANADO:
        _sync_sale_from_oportunidad(op)
    db.session.commit()
    return jsonify(op.to_dict()), 201


# ── Update ────────────────────────────────────────────────────────


@oportunidades_bp.route("/<uuid:opp_id>", methods=["PATCH"])
def update_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    data = request.get_json() or {}

    for fld in ("nombre", "empresa", "contacto_nombre", "contacto_telefono",
                "contacto_email", "moneda", "marca_interes", "estado_cliente",
                "num_sucursales", "sitio", "sale_type", "notas", "motivo_perdida",
                "propietario_id", "account_id", "contact_id"):
        if fld in data:
            setattr(op, fld, data[fld])

    if "valor" in data:
        op.valor = _to_decimal(data["valor"]) or Decimal("0")
    if "monthly_amount" in data:
        op.monthly_amount = _to_decimal(data["monthly_amount"])
    if "fecha_cierre_esperada" in data:
        op.fecha_cierre_esperada = _parse_date(data["fecha_cierre_esperada"])
    if "etapa" in data:
        new_etapa = _parse_etapa(data["etapa"])
        if new_etapa:
            if new_etapa == EtapaOportunidad.CIERRE_GANADO:
                err = _falta_evidencia(op)
                if err:
                    db.session.rollback()
                    return jsonify({"error": err, "requiere_evidencia": True,
                                    "tipos": CE.TIPOS, "max_tipos": CE.MAX_TIPOS}), 422
            op.etapa = new_etapa
            _propagate_close_to_lead(op)
            _sync_sale_from_oportunidad(op)
    if "probabilidad" in data:
        try:
            op.probabilidad = max(0, min(100, int(data["probabilidad"])))
        except (ValueError, TypeError):
            pass

    if (
        op.etapa not in (EtapaOportunidad.CIERRE_GANADO, EtapaOportunidad.CIERRE_PERDIDO)
        and not _truthy(data.get("allow_duplicate"))
    ):
        dup = _find_duplicate_open_opportunity(
            account_id=op.account_id,
            lead_id=op.lead_id,
            marca=op.marca_interes,
            empresa=op.empresa,
            sitio=op.sitio,
            exclude_id=op.id,
        )
        if dup:
            return jsonify({
                "error": "Ya existe otra oportunidad abierta para esta empresa/lead y unidad de negocio.",
                "duplicate": dup.to_dict(),
            }), 409

    if op.etapa == EtapaOportunidad.CIERRE_GANADO:
        _sync_sale_from_oportunidad(op)

    db.session.commit()
    return jsonify(op.to_dict())


def _nace_cerrada_sin_respaldo(marca):
    """Una oportunidad no puede nacer ya en Cerrado Ganado si su unidad exige
    respaldo: todavía no existe el id al que colgarle los archivos.

    Sin este corte, crear el deal directamente en Cerrado Ganado sería la
    forma de saltarse el gate por completo.
    """
    if not CE.requiere_evidencia(marca):
        return None
    return ("Una venta de Pestex no puede crearse ya cerrada: créala en otra "
            "etapa y ciérrala adjuntando el respaldo (orden de compra, "
            "contrato, correo, WhatsApp o cita en Operandium / iGeo).")


def _falta_evidencia(op):
    """Mensaje de error si esta oportunidad no puede cerrarse como ganada
    por falta de respaldo, o None si puede.

    FEAT-2026-09-08: Pestex vende con crédito a 30 días, así que al cerrar
    todavía no hay factura. El respaldo la sustituye como prueba en ese
    momento — no la reemplaza después.
    """
    ok, err = CE.validar_para_cierre("oportunidad", op.id, op.marca_interes)
    return None if ok else err


def _propagate_close_to_lead(op):
    """Cuando la Oportunidad pasa a Cerrado Ganado/Perdido, mueve el Lead
    linkeado a la misma etapa. Si el Lead ya está cerrado, no toca."""
    if not op.lead_id:
        return
    if op.etapa not in (EtapaOportunidad.CIERRE_GANADO, EtapaOportunidad.CIERRE_PERDIDO):
        return
    lead = db.session.get(Lead, op.lead_id)
    if not lead or not lead.etapa_pipeline:
        return
    if lead.etapa_pipeline in (EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO):
        return  # ya cerrado, no piso

    # FIX-2026-09-08: cuando se vende sucursal por sucursal, las N
    # oportunidades cuelgan del mismo lead. Cerrar la primera mandaba el lead
    # a Cerrado Ganado y las otras siete quedaban vivas colgando de un lead
    # que ya había desaparecido del pipe activo. El lead solo se cierra
    # cuando ya no queda ninguna oportunidad abierta.
    quedan_abiertas = (
        db.session.query(Oportunidad.id)
        .filter(
            Oportunidad.lead_id == lead.id,
            Oportunidad.id != op.id,
            Oportunidad.etapa.notin_([
                EtapaOportunidad.CIERRE_GANADO,
                EtapaOportunidad.CIERRE_PERDIDO,
            ]),
        )
        .first()
    )
    if quedan_abiertas:
        return

    lead.etapa_pipeline = (EtapaPipeline.CIERRE_GANADO
                            if op.etapa == EtapaOportunidad.CIERRE_GANADO
                            else EtapaPipeline.CIERRE_PERDIDO)


@oportunidades_bp.route("/<uuid:opp_id>/mover", methods=["PATCH"])
def mover_oportunidad(opp_id):
    """Atajo para drag&drop del Kanban: solo cambia etapa + probabilidad."""
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    data = request.get_json() or {}
    nueva = _parse_etapa(data.get("etapa"))
    if not nueva:
        return jsonify({"error": "Etapa inválida"}), 400
    # El drag&drop del kanban es la vía más usada para cerrar, y era la única
    # sin ninguna validación. El gate va aquí o no sirve de nada.
    if nueva == EtapaOportunidad.CIERRE_GANADO:
        err = _falta_evidencia(op)
        if err:
            return jsonify({"error": err, "requiere_evidencia": True,
                            "tipos": CE.TIPOS, "max_tipos": CE.MAX_TIPOS}), 422
    op.etapa = nueva
    _propagate_close_to_lead(op)
    _sync_sale_from_oportunidad(op)
    db.session.commit()
    return jsonify(op.to_dict())


@oportunidades_bp.route("/<uuid:opp_id>/evidencia-cierre", methods=["GET", "POST"])
def evidencia_cierre(opp_id):
    """Respaldo de la venta. GET lista lo cargado; POST sube archivos.

    POST es multipart: `tipo` (1..MAX_TIPOS repeticiones) y un campo de
    archivo `evidencia_<tipo>` por cada tipo marcado. Se guarda ANTES de
    mover la etapa; el cierre valida contra lo persistido, así que subir y
    cerrar pueden ser dos pasos sin que se pierda nada en medio.
    """
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404

    if request.method == "GET":
        return jsonify({
            "evidencias": [e.to_dict() for e in CE.evidencias_de("oportunidad", op.id)],
            "requerida": CE.requiere_evidencia(op.marca_interes),
            "tipos": CE.TIPOS, "max_tipos": CE.MAX_TIPOS,
        })

    tipos = request.form.getlist("tipo")
    creadas, err = CE.guardar("oportunidad", op.id, tipos, request.files,
                              subido_por=_valid_user_id(_current_user_id()))
    if err:
        db.session.rollback()
        return jsonify({"error": err}), 400
    db.session.commit()
    return jsonify({"evidencias": [e.to_dict() for e in creadas]}), 201


@oportunidades_bp.route("/<uuid:opp_id>", methods=["DELETE"])
def delete_oportunidad(opp_id):
    op = db.session.get(Oportunidad, opp_id)
    if not op:
        return jsonify({"error": "Oportunidad no encontrada"}), 404
    db.session.delete(op)
    db.session.commit()
    return jsonify({"ok": True})


# ── Convertir Lead → Oportunidad ──────────────────────────────────


@oportunidades_bp.route("/from-lead/<uuid:lead_id>", methods=["POST"])
def from_lead(lead_id):
    """Crea una Oportunidad pre-rellenada con datos del Lead. El body puede
    sobrescribir cualquier campo. Mantiene FK lead_id → traza el origen."""
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404
    data = request.get_json() or {}
    account_id = data.get("account_id") or lead.account_id
    contact_id = data.get("contact_id") or lead.contact_id
    marca_interes = data.get("marca_interes") or lead.marca_interes

    if not _truthy(data.get("allow_duplicate")):
        dup = _find_duplicate_open_opportunity(
            account_id=account_id,
            lead_id=lead.id,
            marca=marca_interes,
            empresa=data.get("empresa") or lead.empresa_nombre,
            sitio=(data.get("sitio") or "").strip() or None,
        )
        if dup:
            return jsonify({
                "error": ("Ya existe una oportunidad abierta para este lead/empresa, "
                          "unidad de negocio y sucursal."),
                "duplicate": dup.to_dict(),
            }), 409

    # Defaults desde el Lead
    valor_inicial = (
        _to_decimal(data.get("valor")) or
        (lead.valor_estimado and Decimal(str(lead.valor_estimado))) or
        Decimal("0")
    )
    monthly = (
        _to_decimal(data.get("monthly_amount")) or
        (lead.precio_unitario and Decimal(str(lead.precio_unitario))) or
        None
    )
    op = Oportunidad(
        nombre=data.get("nombre") or f"{lead.empresa_nombre or lead.nombre} — {lead.marca_interes or ''}".strip(" —"),
        empresa=data.get("empresa") or lead.empresa_nombre,
        contacto_nombre=data.get("contacto_nombre") or lead.nombre,
        contacto_telefono=data.get("contacto_telefono") or lead.telefono,
        contacto_email=data.get("contacto_email"),
        valor=valor_inicial,
        moneda=data.get("moneda") or "MXN",
        fecha_cierre_esperada=_parse_date(data.get("fecha_cierre_esperada")),
        etapa=_parse_etapa(data.get("etapa")) or EtapaOportunidad.CALIFICACION,
        propietario_id=_valid_user_id(
            data.get("propietario_id")
            or (str(lead.usuario_asignado_id) if lead.usuario_asignado_id else None)
            or _current_user_id()
        ),
        marca_interes=marca_interes,
        estado_cliente=data.get("estado_cliente") or lead.estado_cliente,
        num_sucursales=data.get("num_sucursales") or lead.num_sucursales,
        sitio=(data.get("sitio") or "").strip() or None,
        monthly_amount=monthly,
        sale_type=data.get("sale_type"),
        notas=data.get("notas") or lead.notas,
        lead_id=lead.id,
        account_id=account_id,
        contact_id=contact_id,
    )
    if op.etapa == EtapaOportunidad.CIERRE_GANADO:
        err = _nace_cerrada_sin_respaldo(op.marca_interes)
        if err:
            db.session.rollback()
            return jsonify({"error": err, "requiere_evidencia": True,
                            "tipos": CE.TIPOS, "max_tipos": CE.MAX_TIPOS}), 422
    db.session.add(op)
    if op.etapa == EtapaOportunidad.CIERRE_GANADO:
        _sync_sale_from_oportunidad(op)
    # Marca lead como convertido subiéndolo de etapa si todavía no llegó
    if lead.etapa_pipeline and lead.etapa_pipeline.value not in ("Cerrado Ganado", "Cerrado Perdido"):
        if lead.etapa_pipeline not in (EtapaPipeline.NEGOCIACION, EtapaPipeline.COTIZACION,
                                         EtapaPipeline.DEMO):
            lead.etapa_pipeline = EtapaPipeline.COTIZACION
    db.session.commit()
    return jsonify(op.to_dict()), 201


# ── Stats ─────────────────────────────────────────────────────────


@oportunidades_bp.route("/stats", methods=["GET"])
def stats():
    base = Oportunidad.query
    rows = (
        base.with_entities(Oportunidad.etapa, func.count(),
                           func.coalesce(func.sum(Oportunidad.valor), 0))
        .group_by(Oportunidad.etapa).all()
    )
    by_etapa = []
    total_count = 0
    total_valor = 0.0
    for et, c, v in rows:
        by_etapa.append({
            "etapa": et.value if et else "—",
            "count": int(c), "valor": float(v or 0),
            "probabilidad": PROBABILIDAD_OPORTUNIDAD.get(et, 0),
        })
        total_count += int(c)
        total_valor += float(v or 0)

    by_marca = (
        base.with_entities(Oportunidad.marca_interes, func.count(),
                           func.coalesce(func.sum(Oportunidad.valor), 0))
        .filter(Oportunidad.marca_interes.isnot(None))
        .group_by(Oportunidad.marca_interes).all()
    )
    return jsonify({
        "total_count": total_count, "total_valor": total_valor,
        "by_etapa": by_etapa,
        "by_marca": [{"marca": m, "count": int(c), "valor": float(v or 0)}
                      for m, c, v in by_marca],
    })
