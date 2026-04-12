# blueprints/auth.py
from functools import wraps
from flask import Blueprint, request, redirect, url_for, session, render_template, jsonify
from extensions import db
from models import UserCRM

auth_bp = Blueprint("auth", __name__)


def require_role(roles):
    """
    Decorador para proteger rutas por rol.
    Uso: @require_role(['super_admin'])
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_rol = session.get("user_rol", "")
            # Normalizar: "Super Admin" → "super_admin"
            rol_norm = user_rol.lower().replace(" ", "_")
            if rol_norm not in roles:
                return jsonify({"error": "No autorizado", "rol_requerido": roles}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def get_vendedor_filter():
    """
    Retorna el usuario_id del vendedor logueado para filtrar queries.
    Si es super_admin, retorna None (ve todo).
    """
    rol = session.get("user_rol", "")
    if rol.lower().replace(" ", "_") == "super_admin":
        return None
    return session.get("usuario_id")


@auth_bp.route("/login", methods=["GET"])
def login_page():
    if session.get("user_id"):
        return redirect(url_for("index"))
    return render_template("auth/login.html")


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.form
    correo = (data.get("correo") or "").strip().lower()
    password = data.get("password") or ""

    user = UserCRM.query.filter(
        db.func.lower(UserCRM.correo) == correo,
        UserCRM.activo.is_(True),
    ).first()

    if not user or not user.check_password(password):
        return render_template("auth/login.html", error="Correo o contraseña incorrectos")

    session.permanent = True
    session["user_id"] = str(user.id)
    session["user_nombre"] = user.nombre
    session["user_correo"] = user.correo
    session["user_rol"] = user.rol.value
    session["usuario_id"] = str(user.usuario_id) if user.usuario_id else None

    from actividad import log_actividad
    log_actividad("login", "usuario", user.id, f"{user.nombre} ({user.correo})")

    return redirect(url_for("index"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/api/me")
def me():
    if not session.get("user_id"):
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({
        "id": session["user_id"],
        "nombre": session["user_nombre"],
        "correo": session["user_correo"],
        "rol": session["user_rol"],
        "usuario_id": session.get("usuario_id"),
    })
