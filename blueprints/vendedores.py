# blueprints/vendedores.py
import secrets
import string
from flask import Blueprint, request, jsonify
from extensions import db
from models import Usuario, RolComercial, UserCRM, RolCRM
from blueprints.auth import require_role

vendedores_bp = Blueprint("vendedores", __name__)


@vendedores_bp.route("/", methods=["GET"])
def listar():
    """Lista vendedores (perfil comercial) enriquecidos con info de login.
    FEAT-2026-06-29: filtro global ?un= por especialidad_marca."""
    from un_filter import usuario_pertenece_a_un
    from flask import request as _req
    vendedores_all = Usuario.query.order_by(Usuario.nombre).all()
    un = _req.args.get("un")
    if un:
        vendedores = [v for v in vendedores_all
                      if usuario_pertenece_a_un(v.especialidad_marca, un)]
    else:
        vendedores = vendedores_all
    # Lookup de logins vinculados (1 query)
    logins = {
        str(u.usuario_id): u for u in UserCRM.query
        .filter(UserCRM.usuario_id.in_([v.id for v in vendedores]))
        .all()
    }
    out = []
    for v in vendedores:
        d = v.to_dict()
        login = logins.get(str(v.id))
        d["login"] = {
            "users_crm_id": str(login.id) if login else None,
            "correo":       login.correo if login else None,
            "rol":          login.rol.value if login else None,
            "activo":       login.activo if login else False,
        } if login else None
        out.append(d)
    return jsonify(out)


def _gen_password(length: int = 12) -> str:
    """Genera password temporal: letras + dígitos, sin chars ambiguos."""
    alphabet = string.ascii_letters + string.digits
    # Filtrar chars ambiguos
    alphabet = alphabet.replace("0", "").replace("O", "").replace("l", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


@vendedores_bp.route("/full", methods=["POST"])
@require_role(["super_admin"])
def crear_completo():
    """Alta completa de vendedor:
      1. Crea el perfil comercial (usuarios)
      2. Crea la cuenta de login (users_crm) con email + password
      3. Vincula ambos

    Body JSON:
      nombre (str, requerido)
      correo (str, requerido) — email corporativo para login
      password (str, opcional) — si no se da, se genera y se devuelve
      especialidad_marca (list[str], opcional) — ["Aromatex","Pestex","Weldex","Nexo","Aromatex Home"]
      zona_cobertura (list[str], opcional) — estados de México
      telefono_whatsapp (str, opcional)
      gmail_address (str, opcional) — Gmail corp para monitoreo
      rol_comercial (str, default "Asesor Comercial")
      rol_login (str, default "Vendedor") — Vendedor | KAM | Super Admin
      en_turno (bool, default True)
    """
    data = request.get_json() or {}
    nombre = (data.get("nombre") or "").strip()
    correo = (data.get("correo") or "").strip().lower()
    if not nombre:
        return jsonify({"error": "Nombre requerido"}), 400
    if not correo:
        return jsonify({"error": "Correo (email) requerido"}), 400

    # Verificar email único en users_crm
    if UserCRM.query.filter(db.func.lower(UserCRM.correo) == correo).first():
        return jsonify({"error": f"Ya existe un usuario con correo {correo}"}), 409

    # Password: si no viene, lo generamos
    password = (data.get("password") or "").strip() or _gen_password()
    if len(password) < 8:
        return jsonify({"error": "Password debe tener al menos 8 caracteres"}), 400

    try:
        rol_com = RolComercial(data.get("rol_comercial", "Asesor Comercial"))
    except ValueError:
        rol_com = RolComercial.ASESOR_COMERCIAL
    try:
        rol_lg = RolCRM(data.get("rol_login", "Vendedor"))
    except ValueError:
        rol_lg = RolCRM.VENDEDOR

    # 1. Perfil comercial
    perfil = Usuario(
        nombre=nombre,
        telefono_whatsapp=data.get("telefono_whatsapp") or None,
        rol_comercial=rol_com,
        especialidad_marca=list(data.get("especialidad_marca") or []),
        zona_cobertura=list(data.get("zona_cobertura") or []),
        en_turno=bool(data.get("en_turno", True)),
        gmail_address=(data.get("gmail_address") or "").strip().lower() or None,
    )
    db.session.add(perfil)
    try:
        db.session.flush()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error creando perfil: {e}"}), 500

    # 2. Login + vínculo
    login = UserCRM(
        nombre=nombre,
        correo=correo,
        rol=rol_lg,
        activo=True,
        usuario_id=perfil.id,
    )
    login.set_password(password)
    db.session.add(login)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error creando login: {e}"}), 500

    return jsonify({
        "ok": True,
        "perfil":        perfil.to_dict(),
        "login": {
            "id":     str(login.id),
            "correo": login.correo,
            "rol":    login.rol.value,
        },
        "password_temporal": password if not (data.get("password") or "").strip() else None,
    }), 201


@vendedores_bp.route("/<uuid:vendedor_id>/full", methods=["PUT"])
@require_role(["super_admin"])
def actualizar_completo(vendedor_id):
    """Actualiza perfil + login vinculado en una sola llamada.
    Si el vendedor NO tiene login y el payload trae correo, CREA el login
    desde cero (genera password temporal si no se proporciona) y lo vincula.
    """
    perfil = db.session.get(Usuario, vendedor_id)
    if not perfil:
        return jsonify({"error": "Vendedor no encontrado"}), 404
    login = UserCRM.query.filter_by(usuario_id=perfil.id).first()
    data = request.get_json() or {}

    # ── Perfil comercial ─────────────────────────────────────────────
    if "nombre" in data and data["nombre"]:
        perfil.nombre = data["nombre"].strip()
        if login: login.nombre = data["nombre"].strip()
    if "telefono_whatsapp" in data:
        perfil.telefono_whatsapp = (data["telefono_whatsapp"] or None)
    if "especialidad_marca" in data and isinstance(data["especialidad_marca"], list):
        perfil.especialidad_marca = data["especialidad_marca"]
    if "zona_cobertura" in data and isinstance(data["zona_cobertura"], list):
        perfil.zona_cobertura = data["zona_cobertura"]
    if "en_turno" in data:
        perfil.en_turno = bool(data["en_turno"])
    if "gmail_address" in data:
        perfil.gmail_address = (data["gmail_address"] or "").strip().lower() or None
    if "rol_comercial" in data:
        try: perfil.rol_comercial = RolComercial(data["rol_comercial"])
        except ValueError: pass

    password_generado = None  # solo set si se crea login nuevo y se genera password

    # ── Login (crear si no existe O actualizar) ──────────────────────
    correo_in = (data.get("correo") or "").strip().lower()

    if not login:
        # No tiene cuenta de login. Si trae correo, la creamos.
        if correo_in:
            # Validar correo único en users_crm
            dup = UserCRM.query.filter(db.func.lower(UserCRM.correo) == correo_in).first()
            if dup:
                return jsonify({"error": f"Ya existe un usuario con correo {correo_in}"}), 409
            try:
                rol_lg = RolCRM(data.get("rol_login", "Vendedor"))
            except ValueError:
                rol_lg = RolCRM.VENDEDOR
            password = (data.get("password") or "").strip()
            if not password:
                password = _gen_password()
                password_generado = password
            if len(password) < 8:
                return jsonify({"error": "Password debe tener al menos 8 caracteres"}), 400
            login = UserCRM(
                nombre=perfil.nombre,
                correo=correo_in,
                rol=rol_lg,
                activo=bool(data.get("activo", True)),
                usuario_id=perfil.id,
            )
            login.set_password(password)
            db.session.add(login)
    else:
        # Ya tiene login: actualizar
        if correo_in and correo_in != login.correo:
            dup = UserCRM.query.filter(
                db.func.lower(UserCRM.correo) == correo_in,
                UserCRM.id != login.id,
            ).first()
            if dup:
                return jsonify({"error": f"Correo {correo_in} ya está usado"}), 409
            login.correo = correo_in
        if "rol_login" in data:
            try: login.rol = RolCRM(data["rol_login"])
            except ValueError: pass
        if "activo" in data:
            login.activo = bool(data["activo"])
        new_pw = (data.get("password") or "").strip()
        if new_pw:
            if len(new_pw) < 8:
                return jsonify({"error": "Password debe tener al menos 8 caracteres"}), 400
            login.set_password(new_pw)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "perfil": perfil.to_dict(),
        "login":  {"id": str(login.id), "correo": login.correo, "rol": login.rol.value, "activo": login.activo} if login else None,
        "password_temporal": password_generado,  # solo populado si se creó login y se generó password
    })


@vendedores_bp.route("/<uuid:vendedor_id>/reset-password", methods=["POST"])
@require_role(["super_admin"])
def reset_password(vendedor_id):
    """Genera password temporal nuevo para un vendedor. Lo devuelve para
    que el admin se lo comparta. El vendedor lo cambia al entrar."""
    perfil = db.session.get(Usuario, vendedor_id)
    if not perfil:
        return jsonify({"error": "Vendedor no encontrado"}), 404
    login = UserCRM.query.filter_by(usuario_id=perfil.id).first()
    if not login:
        return jsonify({"error": "Vendedor no tiene cuenta de login"}), 400
    new_pw = _gen_password()
    login.set_password(new_pw)
    db.session.commit()
    return jsonify({"ok": True, "password_temporal": new_pw, "correo": login.correo})


@vendedores_bp.route("/", methods=["POST"])
@require_role(["super_admin"])
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
@require_role(["super_admin"])
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
@require_role(["super_admin"])
def eliminar(vendedor_id):
    vendedor = db.session.get(Usuario, vendedor_id)
    if not vendedor:
        return jsonify({"error": "No encontrado"}), 404
    db.session.delete(vendedor)
    db.session.commit()
    return jsonify({"ok": True})
