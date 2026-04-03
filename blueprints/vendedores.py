# blueprints/vendedores.py
from flask import Blueprint, request, jsonify
from extensions import db
from models import Usuario, RolComercial

vendedores_bp = Blueprint("vendedores", __name__)


@vendedores_bp.route("/", methods=["GET"])
def listar():
    vendedores = Usuario.query.order_by(Usuario.nombre).all()
    return jsonify([v.to_dict() for v in vendedores])


@vendedores_bp.route("/", methods=["POST"])
def crear():
    data = request.get_json() or {}
    nombre = data.get("nombre", "").strip()
    if not nombre:
        return jsonify({"error": "Nombre requerido"}), 400

    rol_valor = data.get("rol_comercial", "Asesor Comercial")
    try:
        rol = RolComercial(rol_valor)
    except ValueError:
        rol = RolComercial.ASESOR_COMERCIAL

    vendedor = Usuario(
        nombre=nombre,
        telefono_whatsapp=data.get("telefono_whatsapp", ""),
        rol_comercial=rol,
        especialidad_marca=data.get("especialidad_marca", []),
        en_turno=data.get("en_turno", True),
    )
    db.session.add(vendedor)
    db.session.commit()
    return jsonify(vendedor.to_dict()), 201


@vendedores_bp.route("/<uuid:vendedor_id>", methods=["PUT"])
def actualizar(vendedor_id):
    vendedor = db.session.get(Usuario, vendedor_id)
    if not vendedor:
        return jsonify({"error": "No encontrado"}), 404

    data = request.get_json() or {}
    if "nombre" in data:
        vendedor.nombre = data["nombre"]
    if "telefono_whatsapp" in data:
        vendedor.telefono_whatsapp = data["telefono_whatsapp"]
    if "especialidad_marca" in data:
        vendedor.especialidad_marca = data["especialidad_marca"]
    if "en_turno" in data:
        vendedor.en_turno = data["en_turno"]
    if "rol_comercial" in data:
        try:
            vendedor.rol_comercial = RolComercial(data["rol_comercial"])
        except ValueError:
            pass

    db.session.commit()
    return jsonify(vendedor.to_dict())


@vendedores_bp.route("/<uuid:vendedor_id>", methods=["DELETE"])
def eliminar(vendedor_id):
    vendedor = db.session.get(Usuario, vendedor_id)
    if not vendedor:
        return jsonify({"error": "No encontrado"}), 404
    db.session.delete(vendedor)
    db.session.commit()
    return jsonify({"ok": True})
