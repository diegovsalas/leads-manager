# blueprints/auth.py
import os
from functools import wraps
from flask import Blueprint, request, redirect, url_for, session, render_template, jsonify, current_app
from extensions import db
from models import UserCRM

auth_bp = Blueprint("auth", __name__)


FULL_ACCESS_ROLES = {"developer", "super_admin"}
SCOPED_SUPER_ADMIN_ROLES = {
    "super_admin_aromatex",
    "super_admin_pestex",
    "super_admin_comercial",
    "super_admin_nexo",
}
ADMIN_ROLES = FULL_ACCESS_ROLES | SCOPED_SUPER_ADMIN_ROLES
COMMERCIAL_READ_ROLES = ADMIN_ROLES | {"gerente_comercial_aromatex"}

ROLE_UN_SCOPE = {
    "super_admin_aromatex": ("Aromatex",),
    "super_admin_comercial": ("Aromatex",),
    "gerente_comercial_aromatex": ("Aromatex",),
    "super_admin_pestex": ("Pestex",),
    "super_admin_nexo": ("Nexo",),
}


def rol_norm(value=None):
    """Normaliza roles guardados como texto: 'Super Admin Nexo' -> 'super_admin_nexo'."""
    raw = value if value is not None else session.get("user_rol", "")
    return (raw or "").lower().replace(" ", "_")


def is_developer_role(role=None):
    return rol_norm(role) == "developer"


def is_full_access_role(role=None):
    return rol_norm(role) in FULL_ACCESS_ROLES


def is_admin_role(role=None):
    return rol_norm(role) in ADMIN_ROLES


def is_commercial_read_role(role=None):
    return rol_norm(role) in COMMERCIAL_READ_ROLES


def allowed_units_for_role(role=None):
    """None = ve todas las UN. Tupla = alcance obligatorio por UN."""
    return ROLE_UN_SCOPE.get(rol_norm(role))


def effective_un_from_request(requested_un=None):
    """UN efectiva para filtrar datos según el rol de sesión.

    Para roles con alcance limitado, ignora cualquier ?un= fuera de su alcance.
    Para Developer/Super Admin legacy, respeta el filtro solicitado.
    """
    allowed = allowed_units_for_role()
    if not allowed:
        return requested_un
    from un_filter import normalizar_un
    requested = normalizar_un(requested_un)
    if requested and requested in allowed:
        return requested
    return allowed[0]


def require_role(roles):
    """
    Decorador para proteger rutas por rol.
    Uso: @require_role(['super_admin'])
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            normalized_roles = {rol_norm(r) for r in roles}
            current = rol_norm()
            allowed = current in normalized_roles
            # Compatibilidad: las rutas legacy @require_role(["super_admin"])
            # aceptan Developer y los cuatro Super Admin segmentados.
            if not allowed and "super_admin" in normalized_roles:
                allowed = is_admin_role()
            if not allowed:
                return jsonify({"error": "No autorizado", "rol_requerido": roles}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def get_vendedor_filter():
    """
    Retorna el usuario_id del vendedor logueado para filtrar queries.
    Si es super_admin, retorna None (ve todo).
    """
    if is_commercial_read_role():
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


# ─── Backup manual (admin) ──────────────────────────────────────────
@auth_bp.route("/api/admin/backup-now", methods=["POST"])
@require_role(["super_admin"])
def trigger_backup_now():
    """Dispara el backup diario inmediatamente. Útil para verificar config
    sin esperar al cron de las 3am. SECURITY-2026-06-24."""
    from backups import ejecutar_backup
    result = ejecutar_backup()
    return jsonify(result)
