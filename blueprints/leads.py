# blueprints/leads.py
from flask import Blueprint, request, jsonify
from extensions import db, socketio
from models import Lead, EtapaPipeline, OrigenLead, Usuario
from icp_scoring import calcular_icp, INDUSTRIAS, TAMANOS

leads_bp = Blueprint("leads", __name__)

# Origenes que activan auto-asignacion Round-Robin
ORIGENES_AUTO_ASSIGN = {"Meta Ads"}


def _apply_icp(lead):
    """Calcula y aplica ICP score/nivel al lead. Auto-flag nurturing."""
    score, nivel = calcular_icp(
        tipo_industria=lead.tipo_industria,
        tamano_empresa=lead.tamano_empresa,
        num_sucursales=lead.num_sucursales,
        tipo_cliente=lead.tipo_cliente,
        respondio_ultimo_contacto=lead.respondio_ultimo_contacto,
    )
    lead.icp_score = score
    lead.icp_nivel = nivel
    # C y D entran a nurturing automatico
    if nivel in ("C", "D"):
        lead.en_nurturing = True
    elif lead.en_nurturing and nivel in ("A", "B"):
        lead.en_nurturing = False


@leads_bp.route("/", methods=["GET"])
def listar_leads():
    leads = Lead.query.order_by(Lead.fecha_actualizacion.desc()).all()
    return jsonify([l.to_dict() for l in leads])


@leads_bp.route("/<uuid:lead_id>", methods=["GET"])
def obtener_lead(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404
    return jsonify(lead.to_dict())


@leads_bp.route("/", methods=["POST"])
def crear_lead():
    """
    Crea un lead. Auto-asignacion Round-Robin SOLO para Meta Ads.
    Para manual/web/prospeccion se asigna al vendedor que el usuario elija.
    """
    data = request.get_json() or {}

    origen_valor = data.get("origen", "")
    origen_enum = None
    if origen_valor:
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

    # Solo auto-asignar para campanas digitales (Meta Ads)
    if origen_valor in ORIGENES_AUTO_ASSIGN and marca:
        from asignacion import asignar_lead_comercial
        try:
            lead = asignar_lead_comercial({
                "telefono":           data.get("telefono"),
                "nombre":             data.get("nombre", "Sin nombre"),
                "origen":             origen_valor,
                "marca_interes":      marca,
                "valor_estimado":     valor,
                "cantidad_productos": cantidad,
                "precio_unitario":    precio,
            })
            socketio.emit("nuevo_lead", lead.to_dict())
            return jsonify(lead.to_dict()), 201
        except ValueError:
            pass  # Sin vendedores disponibles, crear sin asignar

    # Crear lead con asignacion manual (o sin asignar)
    lead = Lead(
        nombre=data.get("nombre", "Sin nombre"),
        telefono=data.get("telefono"),
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
    _apply_icp(lead)
    db.session.add(lead)
    db.session.commit()

    socketio.emit("nuevo_lead", lead.to_dict())
    return jsonify(lead.to_dict()), 201


@leads_bp.route("/<uuid:lead_id>/mover", methods=["PATCH"])
def mover_lead(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}
    try:
        nueva_etapa = EtapaPipeline(data.get("etapa_pipeline"))
    except (ValueError, KeyError):
        return jsonify({"error": "Etapa invalida"}), 400

    lead.etapa_pipeline = nueva_etapa
    db.session.commit()

    socketio.emit("lead_movido", {
        "lead_id": str(lead.id),
        "etapa_pipeline": nueva_etapa.value,
    })
    return jsonify(lead.to_dict())


@leads_bp.route("/<uuid:lead_id>", methods=["PUT"])
def actualizar_lead(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}
    for campo in ["nombre", "telefono", "marca_interes", "cantidad_productos",
                   "precio_unitario", "valor_estimado", "motivo_perdida",
                   "usuario_asignado_id", "tipo_industria", "tamano_empresa",
                   "num_sucursales", "tipo_cliente"]:
        if campo in data:
            setattr(lead, campo, data[campo])

    if "etapa_pipeline" in data:
        try:
            lead.etapa_pipeline = EtapaPipeline(data["etapa_pipeline"])
        except ValueError:
            return jsonify({"error": "Etapa invalida"}), 400

    # Recalcular ICP si se modificaron campos relevantes
    icp_fields = {"tipo_industria", "tamano_empresa", "num_sucursales", "tipo_cliente"}
    if icp_fields & set(data.keys()):
        _apply_icp(lead)

    db.session.commit()
    return jsonify(lead.to_dict())


@leads_bp.route("/icp-opciones", methods=["GET"])
def icp_opciones():
    """Retorna las opciones de industria y tamaño para el formulario."""
    return jsonify({"industrias": INDUSTRIAS, "tamanos": TAMANOS})


@leads_bp.route("/<uuid:lead_id>/registrar_respuesta", methods=["POST"])
def registrar_respuesta(lead_id):
    """
    Registra que el lead respondió (mensaje entrante de WhatsApp).
    Detiene la cadencia automatica para este lead.
    """
    from datetime import datetime, timezone

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    lead.respondio_ultimo_contacto = True
    lead.fecha_ultimo_contacto = datetime.now(timezone.utc)

    db.session.commit()

    return jsonify({
        "ok": True,
        "lead_id": str(lead.id),
        "respondio": True,
        "etapa": lead.etapa_pipeline.value,
    })
