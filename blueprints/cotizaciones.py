# blueprints/cotizaciones.py
"""
Cotizaciones PDF — Grupo Avantex
- Preview: vendedor confirma datos antes de generar
- Generate: crea PDF y lo almacena
- Send: envía por correo (Resend) y/o WhatsApp
"""
import os
import base64
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, session, send_file
from extensions import db
from models import Cotizacion, Lead
from cotizador import generar_pdf, folio_siguiente, IVA_RATE

cotizaciones_bp = Blueprint("cotizaciones", __name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")


@cotizaciones_bp.route("/preview/<uuid:lead_id>", methods=["GET"])
def preview(lead_id):
    """
    Retorna datos pre-llenados del lead para que el vendedor confirme
    antes de generar la cotización.
    """
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    vendedor = lead.usuario_asignado
    valor = lead.valor_calculado

    return jsonify({
        "lead_id": str(lead.id),
        "nombre_cliente": lead.nombre or "",
        "telefono_cliente": lead.telefono or "",
        "empresa_cliente": "",
        "direccion_cliente": "",
        "correo_cliente": "",
        "marca": lead.marca_interes or "Aromatex",
        "vendedor_nombre": vendedor.nombre if vendedor else session.get("user_nombre", ""),
        "items": [
            {
                "servicio": lead.marca_interes or "Servicio",
                "descripcion": "Servicio de " + (lead.marca_interes or "Grupo Avantex").lower(),
                "cantidad": lead.cantidad_productos or 1,
                "frecuencia": "Cada 1 mes",
                "precio_unitario": float(lead.precio_unitario) if lead.precio_unitario else 0,
                "descuento_pct": 0,
            }
        ] if lead.precio_unitario else [],
        "condiciones_pago": "PUE",
        "vigencia_dias": 15,
    })


@cotizaciones_bp.route("/generar", methods=["POST"])
def generar():
    """
    Genera la cotización PDF con los datos confirmados por el vendedor.
    Guarda en BD y retorna el ID.
    """
    data = request.get_json() or {}
    lead_id = data.get("lead_id")

    if not lead_id:
        return jsonify({"error": "lead_id requerido"}), 400

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    items = data.get("items", [])
    if not items:
        return jsonify({"error": "Agrega al menos un producto/servicio"}), 400

    # Calculate totals
    subtotal = 0
    for item in items:
        precio = float(item.get("precio_unitario", 0))
        cant = int(item.get("cantidad", 1))
        desc_pct = float(item.get("descuento_pct", 0))
        precio_desc = precio * (1 - desc_pct / 100)
        subtotal += precio * cant

    descuento_pct = float(data.get("descuento_pct", 0))
    descuento_total = sum(
        float(i.get("precio_unitario", 0)) * int(i.get("cantidad", 1)) * float(i.get("descuento_pct", 0)) / 100
        for i in items
    )
    neto = subtotal - descuento_total
    iva = neto * IVA_RATE
    total = neto + iva

    folio = folio_siguiente(db.session)

    cotizacion = Cotizacion(
        lead_id=lead_id,
        folio=folio,
        contenido=f"Cotización {folio} para {data.get('nombre_cliente', '')}",
        generado_por=session.get("user_nombre", "Sistema"),
        nombre_cliente=data.get("nombre_cliente", lead.nombre),
        empresa_cliente=data.get("empresa_cliente"),
        direccion_cliente=data.get("direccion_cliente"),
        telefono_cliente=data.get("telefono_cliente", lead.telefono),
        correo_cliente=data.get("correo_cliente"),
        marca=data.get("marca", lead.marca_interes),
        items=items,
        subtotal=subtotal,
        descuento_pct=descuento_pct,
        descuento_monto=descuento_total,
        iva=iva,
        total=total,
        condiciones_pago=data.get("condiciones_pago", "PUE"),
        vigencia_dias=data.get("vigencia_dias", 15),
        vendedor_nombre=data.get("vendedor_nombre", ""),
    )
    db.session.add(cotizacion)
    db.session.commit()

    return jsonify(cotizacion.to_dict()), 201


@cotizaciones_bp.route("/<uuid:cot_id>/pdf", methods=["GET"])
def descargar_pdf(cot_id):
    """Genera y descarga el PDF de una cotización."""
    cot = db.session.get(Cotizacion, cot_id)
    if not cot:
        return jsonify({"error": "Cotización no encontrada"}), 404

    pdf_data = {
        "folio": cot.folio,
        "fecha": cot.fecha.strftime("%d-%m-%Y"),
        "marca": cot.marca or "Aromatex",
        "nombre_cliente": cot.nombre_cliente,
        "empresa_cliente": cot.empresa_cliente,
        "direccion_cliente": cot.direccion_cliente,
        "vendedor_nombre": cot.vendedor_nombre,
        "condiciones_pago": cot.condiciones_pago,
        "vigencia_dias": cot.vigencia_dias,
        "items": cot.items or [],
    }

    pdf_bytes = generar_pdf(pdf_data)

    import io
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{cot.folio}.pdf",
    )


@cotizaciones_bp.route("/<uuid:cot_id>/enviar-correo", methods=["POST"])
def enviar_correo(cot_id):
    """Envía la cotización por correo al cliente usando Resend."""
    if not RESEND_API_KEY:
        return jsonify({"error": "RESEND_API_KEY no configurada"}), 500

    cot = db.session.get(Cotizacion, cot_id)
    if not cot:
        return jsonify({"error": "Cotización no encontrada"}), 404

    correo = cot.correo_cliente
    if not correo:
        data = request.get_json() or {}
        correo = data.get("correo")
    if not correo:
        return jsonify({"error": "correo del cliente requerido"}), 400

    # Generate PDF
    pdf_data = {
        "folio": cot.folio,
        "fecha": cot.fecha.strftime("%d-%m-%Y"),
        "marca": cot.marca or "Aromatex",
        "nombre_cliente": cot.nombre_cliente,
        "empresa_cliente": cot.empresa_cliente,
        "direccion_cliente": cot.direccion_cliente,
        "vendedor_nombre": cot.vendedor_nombre,
        "condiciones_pago": cot.condiciones_pago,
        "vigencia_dias": cot.vigencia_dias,
        "items": cot.items or [],
    }
    pdf_bytes = generar_pdf(pdf_data)

    import resend
    resend.api_key = RESEND_API_KEY

    marca = cot.marca or "Grupo Avantex"
    try:
        resend.Emails.send({
            "from": f"CRM Avantex <crm@grupoavantex.com>",
            "to": [correo],
            "reply_to": "diegovelazquez@grupoavantex.com",
            "subject": f"Cotización {cot.folio} — {marca}",
            "html": f"""<div style="font-family:Arial,sans-serif;color:#333;">
                <p>Estimado/a <strong>{cot.nombre_cliente}</strong>,</p>
                <p>Adjunto encontrará la cotización <strong>{cot.folio}</strong> de <strong>{marca}</strong>.</p>
                <p>Quedo a sus órdenes para cualquier duda.</p>
                <p>Saludos cordiales,<br><strong>{cot.vendedor_nombre}</strong><br>{marca} — Grupo Avantex<br>Tel: 8183354805</p>
            </div>""",
            "attachments": [{
                "filename": f"{cot.folio}.pdf",
                "content": list(pdf_bytes),
            }],
        })

        cot.enviada_correo = True
        cot.correo_cliente = correo
        db.session.commit()

        return jsonify({"ok": True, "correo": correo})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@cotizaciones_bp.route("/lead/<uuid:lead_id>", methods=["GET"])
def listar_por_lead(lead_id):
    """Lista cotizaciones de un lead."""
    cots = Cotizacion.query.filter_by(lead_id=lead_id).order_by(Cotizacion.fecha.desc()).all()
    return jsonify([c.to_dict() for c in cots])
