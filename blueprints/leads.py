# blueprints/leads.py
from flask import Blueprint, request, jsonify
from extensions import db, socketio
from models import Lead, EtapaPipeline

leads_bp = Blueprint("leads", __name__)


@leads_bp.route("/", methods=["GET"])
def listar_leads():
    """Lista todos los leads ordenados por última actualización."""
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
    Crea un lead manualmente (desde el frontend).
    Si se envía marca_interes, intenta asignación automática Round-Robin.
    """
    data = request.get_json() or {}
    marca = data.get("marca_interes", "")

    if marca:
        from asignacion import asignar_lead_comercial
        try:
            lead = asignar_lead_comercial({
                "telefono":      data.get("telefono"),
                "nombre":        data.get("nombre", "Sin nombre"),
                "origen":        data.get("origen", "Web"),
                "marca_interes": marca,
                "valor_estimado": data.get("valor_estimado"),
            })
            socketio.emit("nuevo_lead", lead.to_dict())
            return jsonify(lead.to_dict()), 201
        except ValueError:
            pass  # Crear sin asignar si no hay vendedores

    from models import OrigenLead
    origen_valor = data.get("origen")
    origen_enum = None
    if origen_valor:
        try:
            origen_enum = OrigenLead(origen_valor)
        except ValueError:
            origen_enum = None

    # Calcular valor si viene cantidad y precio
    cantidad = data.get("cantidad_productos")
    precio   = data.get("precio_unitario")
    valor    = data.get("valor_estimado")
    if cantidad and precio and not valor:
        valor = float(cantidad) * float(precio)

    lead = Lead(
        nombre              = data.get("nombre", "Sin nombre"),
        telefono            = data.get("telefono"),
        origen              = origen_enum,
        marca_interes       = marca,
        etapa_pipeline      = EtapaPipeline.NUEVO_LEAD,
        cantidad_productos  = cantidad,
        precio_unitario     = precio,
        valor_estimado      = valor,
        usuario_asignado_id = data.get("usuario_asignado_id"),
    )
    db.session.add(lead)
    db.session.commit()

    socketio.emit("nuevo_lead", lead.to_dict())
    return jsonify(lead.to_dict()), 201


@leads_bp.route("/<uuid:lead_id>/mover", methods=["PATCH"])
def mover_lead(lead_id):
    """
    Mueve un lead a otra etapa del Kanban (drag-and-drop).
    Body: { "etapa_pipeline": "Calificando" }
    """
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data  = request.get_json() or {}
    nueva_etapa_valor = data.get("etapa_pipeline")

    try:
        nueva_etapa = EtapaPipeline(nueva_etapa_valor)
    except (ValueError, KeyError):
        etapas_validas = [e.value for e in EtapaPipeline]
        return jsonify({"error": f"Etapa inválida. Válidas: {etapas_validas}"}), 400

    lead.etapa_pipeline = nueva_etapa
    db.session.commit()

    socketio.emit("lead_movido", {
        "lead_id":       str(lead.id),
        "etapa_pipeline": nueva_etapa.value,
    })

    return jsonify(lead.to_dict())


@leads_bp.route("/<uuid:lead_id>", methods=["PUT"])
def actualizar_lead(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}

    for campo in ["nombre", "telefono", "marca_interes", "cantidad_productos", "precio_unitario", "valor_estimado", "motivo_perdida", "usuario_asignado_id"]:
        if campo in data:
            setattr(lead, campo, data[campo])

    if "etapa_pipeline" in data:
        try:
            lead.etapa_pipeline = EtapaPipeline(data["etapa_pipeline"])
        except ValueError:
            return jsonify({"error": "Etapa inválida"}), 400

    db.session.commit()
    return jsonify(lead.to_dict())
