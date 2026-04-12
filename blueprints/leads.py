# blueprints/leads.py
from flask import Blueprint, request, jsonify
from extensions import db, socketio
from models import Lead, EtapaPipeline, OrigenLead, Usuario
from icp_scoring import calcular_icp, INDUSTRIAS, TAMANOS
from actividad import log_actividad

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

    log_actividad("crear", "lead", lead.id, f"Lead creado: {lead.nombre} ({lead.telefono})")
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

    etapa_anterior = lead.etapa_pipeline.value
    lead.etapa_pipeline = nueva_etapa
    db.session.commit()

    log_actividad("mover", "lead", lead.id, f"{lead.nombre}: {etapa_anterior} → {nueva_etapa.value}")
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


@leads_bp.route("/mis-leads-hoy", methods=["GET"])
def mis_leads_hoy():
    """
    Retorna los leads del vendedor logueado que necesitan acción hoy:
    - Sin contactar (etapa Nuevo Lead)
    - Próximos a vencer cadencia (no respondieron, en etapas de contacto)
    - Respondieron (necesitan seguimiento manual)
    - En negociación activa (Cotización, Demo, Negociación)
    """
    from datetime import datetime, timezone, timedelta
    from blueprints.auth import get_vendedor_filter

    vendedor_id = get_vendedor_filter()
    base_q = Lead.query
    if vendedor_id:
        base_q = base_q.filter_by(usuario_asignado_id=vendedor_id)

    ahora = datetime.now(timezone.utc)
    hace_24h = ahora - timedelta(hours=24)
    hace_48h = ahora - timedelta(hours=48)

    # 1. Sin contactar (Nuevo Lead)
    sin_contactar = base_q.filter_by(
        etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
    ).order_by(Lead.fecha_creacion.desc()).all()

    # 2. Próximos a vencer cadencia (en contacto, no respondieron, último contacto > 20h)
    etapas_contacto = [EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
                       EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4]
    por_vencer = base_q.filter(
        Lead.etapa_pipeline.in_(etapas_contacto),
        Lead.respondio_ultimo_contacto == False,
        Lead.fecha_ultimo_contacto <= hace_24h,
    ).order_by(Lead.fecha_ultimo_contacto.asc()).all()

    # 3. Respondieron (necesitan seguimiento)
    respondieron = base_q.filter(
        Lead.respondio_ultimo_contacto == True,
        Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
    ).order_by(Lead.fecha_actualizacion.desc()).all()

    # 4. En negociación activa
    etapas_negociacion = [EtapaPipeline.COTIZACION, EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION]
    en_negociacion = base_q.filter(
        Lead.etapa_pipeline.in_(etapas_negociacion),
    ).order_by(Lead.fecha_actualizacion.desc()).all()

    return jsonify({
        "sin_contactar": [l.to_dict() for l in sin_contactar],
        "por_vencer": [l.to_dict() for l in por_vencer],
        "respondieron": [l.to_dict() for l in respondieron],
        "en_negociacion": [l.to_dict() for l in en_negociacion],
        "resumen": {
            "sin_contactar": len(sin_contactar),
            "por_vencer": len(por_vencer),
            "respondieron": len(respondieron),
            "en_negociacion": len(en_negociacion),
            "total_accion": len(sin_contactar) + len(por_vencer) + len(respondieron),
        },
    })


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
