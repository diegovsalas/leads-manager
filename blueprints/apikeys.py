# blueprints/apikeys.py
"""
API Key management + middleware para acceso externo controlado.

Autenticación: Header `X-API-Key: <key>`

Permisos granulares:
  - leads:read      → GET /api/v1/leads
  - leads:write     → POST /api/v1/leads, PATCH mover
  - vendedores:read → GET /api/v1/vendedores
  - webhooks:write  → POST eventos desde sistemas externos

Uso:
  from blueprints.apikeys import require_api_key
  @require_api_key("leads:read")
  def mi_endpoint(): ...
"""
import secrets
from functools import wraps
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, session, g
from extensions import db
from models import ApiKey
from blueprints.auth import require_role

apikeys_bp = Blueprint("apikeys", __name__)

# ── Permisos disponibles ──
PERMISOS_DISPONIBLES = [
    "leads:read",
    "leads:write",
    "vendedores:read",
    "dashboard:read",
    "webhooks:write",
]


# ──────────────────────────────────────────────
# Middleware: autenticación por API key
# ──────────────────────────────────────────────
def require_api_key(permiso):
    """
    Decorador que valida X-API-Key header y verifica permiso.
    Uso: @require_api_key("leads:read")
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = request.headers.get("X-API-Key")
            if not key:
                return jsonify({
                    "error": "API key requerida",
                    "detalle": "Envía el header X-API-Key con tu clave",
                }), 401

            api_key = ApiKey.query.filter_by(api_key=key, activo=True).first()
            if not api_key:
                return jsonify({"error": "API key inválida o desactivada"}), 403

            if permiso not in (api_key.permisos or []):
                return jsonify({
                    "error": "Permiso insuficiente",
                    "requiere": permiso,
                    "tienes": api_key.permisos,
                }), 403

            # Track usage
            api_key.ultimo_uso = datetime.now(timezone.utc)
            api_key.usos = (api_key.usos or 0) + 1
            db.session.commit()

            g.api_key = api_key
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ──────────────────────────────────────────────
# Admin: CRUD de API keys (solo Super Admin)
# ──────────────────────────────────────────────
@apikeys_bp.route("/", methods=["GET"])
@require_role(["super_admin"])
def listar():
    """Lista todas las API keys (sin mostrar la key completa)."""
    keys = ApiKey.query.order_by(ApiKey.fecha_creacion.desc()).all()
    return jsonify([k.to_dict() for k in keys])


@apikeys_bp.route("/", methods=["POST"])
@require_role(["super_admin"])
def crear():
    """
    Crea una nueva API key.
    Body: { "nombre": "Bot Ventas", "permisos": ["leads:read", "leads:write"] }
    La key completa solo se muestra UNA VEZ en la respuesta.
    """
    data = request.get_json() or {}
    nombre = data.get("nombre", "").strip()
    if not nombre:
        return jsonify({"error": "nombre es requerido"}), 400

    permisos = data.get("permisos", ["leads:read"])
    # Validar permisos
    for p in permisos:
        if p not in PERMISOS_DISPONIBLES:
            return jsonify({
                "error": f"Permiso inválido: {p}",
                "disponibles": PERMISOS_DISPONIBLES,
            }), 400

    # Generar key segura
    raw_key = secrets.token_urlsafe(32)
    api_key = f"avx_{raw_key}"

    key = ApiKey(
        nombre=nombre,
        api_key=api_key,
        permisos=permisos,
        creado_por=session.get("user_id"),
    )
    db.session.add(key)
    db.session.commit()

    return jsonify({
        "mensaje": "API key creada. Guárdala, no se volverá a mostrar completa.",
        **key.to_dict_full(),
    }), 201


@apikeys_bp.route("/<uuid:key_id>/toggle", methods=["POST"])
@require_role(["super_admin"])
def toggle(key_id):
    """Activa/desactiva una API key."""
    key = db.session.get(ApiKey, key_id)
    if not key:
        return jsonify({"error": "API key no encontrada"}), 404

    key.activo = not key.activo
    db.session.commit()
    return jsonify(key.to_dict())


@apikeys_bp.route("/<uuid:key_id>", methods=["DELETE"])
@require_role(["super_admin"])
def eliminar(key_id):
    """Elimina una API key permanentemente."""
    key = db.session.get(ApiKey, key_id)
    if not key:
        return jsonify({"error": "API key no encontrada"}), 404

    db.session.delete(key)
    db.session.commit()
    return jsonify({"ok": True})


@apikeys_bp.route("/permisos", methods=["GET"])
@require_role(["super_admin"])
def listar_permisos():
    """Lista permisos disponibles."""
    return jsonify(PERMISOS_DISPONIBLES)
