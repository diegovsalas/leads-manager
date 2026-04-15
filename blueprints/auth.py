# blueprints/auth.py
import os
from functools import wraps
from flask import Blueprint, request, redirect, url_for, session, render_template, jsonify, current_app
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


def is_kam():
    """Retorna True si el usuario logueado es KAM."""
    return session.get("user_rol", "").upper() == "KAM"


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

    # KAMs van directo al CS Dashboard
    if user.rol.value.upper() == "KAM":
        return redirect("/cs/")
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


# ──────────────────────────────────────────────
# Google SSO
# ──────────────────────────────────────────────
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
ALLOWED_DOMAIN = "grupoavantex.com"


@auth_bp.route("/auth/google")
def google_login():
    """Inicia flujo OAuth 2.0 con Google."""
    import secrets
    import urllib.parse

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    if not client_id:
        return render_template("auth/login.html", error="Google SSO no configurado.")

    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state

    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "https://leads-manager-avantex.onrender.com/auth/google/callback",
    )

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": ALLOWED_DOMAIN,  # hint: solo cuentas @grupoavantex.com
        "access_type": "online",
        "prompt": "select_account",
    })

    return redirect(f"{GOOGLE_AUTH_URL}?{params}")


@auth_bp.route("/auth/google/callback")
def google_callback():
    """Maneja el callback de Google OAuth 2.0."""
    import requests as http

    # Verificar state CSRF
    state = request.args.get("state", "")
    if state != session.pop("oauth_state", None):
        return render_template("auth/login.html", error="Error de seguridad en SSO. Intenta de nuevo.")

    error = request.args.get("error")
    if error:
        return render_template("auth/login.html", error=f"Google rechazó el acceso: {error}")

    code = request.args.get("code")
    if not code:
        return render_template("auth/login.html", error="No se recibió código de Google.")

    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    redirect_uri = os.getenv(
        "GOOGLE_REDIRECT_URI",
        "https://leads-manager-avantex.onrender.com/auth/google/callback",
    )

    # Intercambiar code por tokens
    token_resp = http.post(GOOGLE_TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=10)

    if not token_resp.ok:
        return render_template("auth/login.html", error="Error al obtener token de Google.")

    access_token = token_resp.json().get("access_token")

    # Obtener datos del usuario
    userinfo_resp = http.get(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if not userinfo_resp.ok:
        return render_template("auth/login.html", error="No se pudo obtener información del usuario.")

    info = userinfo_resp.json()
    correo = (info.get("email") or "").lower()
    nombre = info.get("name") or correo

    # Validar dominio
    if not correo.endswith(f"@{ALLOWED_DOMAIN}"):
        return render_template(
            "auth/login.html",
            error=f"Solo se permiten cuentas @{ALLOWED_DOMAIN}.",
        )

    # Buscar usuario existente en CRM
    user = UserCRM.query.filter(
        db.func.lower(UserCRM.correo) == correo,
        UserCRM.activo.is_(True),
    ).first()

    if not user:
        return render_template(
            "auth/login.html",
            error=f"El correo {correo} no tiene acceso al CRM. Contacta al administrador.",
        )

    # Crear sesión
    session.permanent = True
    session["user_id"] = str(user.id)
    session["user_nombre"] = user.nombre
    session["user_correo"] = user.correo
    session["user_rol"] = user.rol.value
    session["usuario_id"] = str(user.usuario_id) if user.usuario_id else None

    from actividad import log_actividad
    log_actividad("login_google", "usuario", user.id, f"{user.nombre} ({user.correo}) via Google SSO")

    if user.rol.value.upper() == "KAM":
        return redirect("/cs/")
    return redirect(url_for("index"))
