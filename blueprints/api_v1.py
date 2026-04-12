# blueprints/api_v1.py
"""
API v1 — Endpoints para sistemas externos (bots, integraciones).
Autenticación: Header X-API-Key
Cada endpoint requiere un permiso específico.
"""
from flask import Blueprint, request, jsonify
from extensions import db
from models import Lead, EtapaPipeline, OrigenLead, Usuario
from blueprints.apikeys import require_api_key

api_v1_bp = Blueprint("api_v1", __name__)


# ──────────────────────────────────────────────
# Leads
# ──────────────────────────────────────────────
@api_v1_bp.route("/leads", methods=["GET"])
@require_api_key("leads:read")
def listar_leads():
    """
    Lista leads. Soporta filtros por query params:
    ?etapa=Nuevo Lead&marca=Aromatex&limit=50&offset=0
    """
    query = Lead.query.order_by(Lead.fecha_actualizacion.desc())

    etapa = request.args.get("etapa")
    if etapa:
        try:
            query = query.filter_by(etapa_pipeline=EtapaPipeline(etapa))
        except ValueError:
            return jsonify({"error": f"Etapa inválida: {etapa}"}), 400

    marca = request.args.get("marca")
    if marca:
        query = query.filter_by(marca_interes=marca)

    origen = request.args.get("origen")
    if origen:
        try:
            query = query.filter_by(origen=OrigenLead(origen))
        except ValueError:
            pass

    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    total = query.count()
    leads = query.offset(offset).limit(limit).all()

    return jsonify({
        "total": total,
        "limit": limit,
        "offset": offset,
        "leads": [l.to_dict() for l in leads],
    })


@api_v1_bp.route("/leads/<uuid:lead_id>", methods=["GET"])
@require_api_key("leads:read")
def obtener_lead(lead_id):
    """Obtiene un lead por ID."""
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404
    return jsonify(lead.to_dict())


@api_v1_bp.route("/leads", methods=["POST"])
@require_api_key("leads:write")
def crear_lead():
    """
    Crea un lead desde sistema externo.
    Body: { "nombre": "...", "telefono": "...", "origen": "...", "marca_interes": "..." }
    Si origen es "Meta Ads" y hay marca, auto-asigna por Round-Robin.
    """
    data = request.get_json() or {}

    telefono = data.get("telefono", "").strip()
    if not telefono:
        return jsonify({"error": "telefono es requerido"}), 400

    # Check duplicate
    existente = Lead.query.filter_by(telefono=telefono).first()
    if existente:
        return jsonify({
            "error": "Lead ya existe con ese teléfono",
            "lead_id": str(existente.id),
            "etapa": existente.etapa_pipeline.value,
        }), 409

    origen_valor = data.get("origen", "Web")
    origen_enum = None
    try:
        origen_enum = OrigenLead(origen_valor)
    except ValueError:
        origen_enum = None

    marca = data.get("marca_interes", "")
    cantidad = data.get("cantidad_productos")
    precio = data.get("precio_unitario")
    valor = data.get("valor_estimado")
    if cantidad and precio and not valor:
        valor = float(cantidad) * float(precio)

    # Auto-assign for Meta Ads
    if origen_valor == "Meta Ads" and marca:
        from asignacion import asignar_lead_comercial
        try:
            lead = asignar_lead_comercial({
                "telefono": telefono,
                "nombre": data.get("nombre", "Sin nombre"),
                "origen": origen_valor,
                "marca_interes": marca,
                "valor_estimado": valor,
                "cantidad_productos": cantidad,
                "precio_unitario": precio,
            })
            from extensions import socketio
            socketio.emit("nuevo_lead", lead.to_dict())
            return jsonify(lead.to_dict()), 201
        except ValueError:
            pass

    lead = Lead(
        nombre=data.get("nombre", "Sin nombre"),
        telefono=telefono,
        origen=origen_enum,
        marca_interes=marca,
        etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        cantidad_productos=cantidad,
        precio_unitario=precio,
        valor_estimado=valor,
        usuario_asignado_id=data.get("usuario_asignado_id"),
        tipo_industria=data.get("tipo_industria"),
        tamano_empresa=data.get("tamano_empresa"),
        num_sucursales=data.get("num_sucursales"),
        tipo_cliente=data.get("tipo_cliente"),
    )

    from icp_scoring import calcular_icp
    score, nivel = calcular_icp(
        tipo_industria=lead.tipo_industria,
        tamano_empresa=lead.tamano_empresa,
        num_sucursales=lead.num_sucursales,
        tipo_cliente=lead.tipo_cliente,
    )
    lead.icp_score = score
    lead.icp_nivel = nivel
    if nivel in ("C", "D"):
        lead.en_nurturing = True

    db.session.add(lead)
    db.session.commit()

    from extensions import socketio
    socketio.emit("nuevo_lead", lead.to_dict())
    return jsonify(lead.to_dict()), 201


@api_v1_bp.route("/leads/<uuid:lead_id>/mover", methods=["PATCH"])
@require_api_key("leads:write")
def mover_lead(lead_id):
    """
    Mueve un lead a otra etapa.
    Body: { "etapa_pipeline": "Cotización" }
    """
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}
    try:
        nueva_etapa = EtapaPipeline(data.get("etapa_pipeline"))
    except (ValueError, KeyError):
        return jsonify({
            "error": "Etapa inválida",
            "etapas_validas": [e.value for e in EtapaPipeline],
        }), 400

    lead.etapa_pipeline = nueva_etapa
    db.session.commit()

    from extensions import socketio
    socketio.emit("lead_movido", {
        "lead_id": str(lead.id),
        "etapa_pipeline": nueva_etapa.value,
    })
    return jsonify(lead.to_dict())


@api_v1_bp.route("/leads/<uuid:lead_id>/respuesta", methods=["POST"])
@require_api_key("leads:write")
def registrar_respuesta(lead_id):
    """Marca que el lead respondió (detiene cadencia automática)."""
    from datetime import datetime, timezone

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    lead.respondio_ultimo_contacto = True
    lead.fecha_ultimo_contacto = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({"ok": True, "lead_id": str(lead.id), "etapa": lead.etapa_pipeline.value})


# ──────────────────────────────────────────────
# Vendedores
# ──────────────────────────────────────────────
@api_v1_bp.route("/vendedores", methods=["GET"])
@require_api_key("vendedores:read")
def listar_vendedores():
    """Lista vendedores activos."""
    vendedores = Usuario.query.filter_by(en_turno=True).order_by(Usuario.nombre).all()
    return jsonify([v.to_dict() for v in vendedores])


# ──────────────────────────────────────────────
# Pipeline info
# ──────────────────────────────────────────────
@api_v1_bp.route("/pipeline/etapas", methods=["GET"])
@require_api_key("leads:read")
def listar_etapas():
    """Lista todas las etapas del pipeline con conteo."""
    resultado = {}
    for etapa in EtapaPipeline:
        count = Lead.query.filter_by(etapa_pipeline=etapa).count()
        resultado[etapa.value] = count
    return jsonify(resultado)


# ──────────────────────────────────────────────
# Health check (sin auth)
# ──────────────────────────────────────────────
@api_v1_bp.route("/health", methods=["GET"])
def health():
    """Health check público."""
    return jsonify({"status": "ok", "version": "1.0", "sistema": "Leads Manager Avantex"})
