# blueprints/leads.py
import re
import uuid
from flask import Blueprint, request, jsonify, session
from sqlalchemy import or_, func
from extensions import db, socketio
from models import Lead, EtapaPipeline, OrigenLead, Usuario
from icp_scoring import calcular_icp, INDUSTRIAS, TAMANOS
from actividad import log_actividad
from meta_conversions import send_pipeline_event

leads_bp = Blueprint("leads", __name__)

# Origenes que activan auto-asignacion Round-Robin
ORIGENES_AUTO_ASSIGN = {"Meta Ads"}


def _apply_icp(lead):
    """Calcula y aplica ICP score/nivel al lead. Auto-flag nurturing.
    Si el lead tiene Empresa linkeada, prefiere los datos de la Empresa
    (industria, tamaño, sucursales) por sobre los del lead. Eso evita
    el doble-data y permite que actualizar la empresa recalcule el ICP
    de todos sus leads."""
    industria = lead.tipo_industria
    tamano = lead.tamano_empresa
    sucursales = lead.num_sucursales
    if lead.account_id:
        from models import Account
        acc = db.session.get(Account, lead.account_id)
        if acc:
            industria = acc.industria or industria
            tamano = acc.tamano or tamano
            sucursales = acc.num_sucursales if acc.num_sucursales is not None else sucursales
    score, nivel = calcular_icp(
        tipo_industria=industria,
        tamano_empresa=tamano,
        num_sucursales=sucursales,
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
    """Lista leads. SECURITY-2026-06-24: vendedor solo ve los suyos.
    Super Admin ve todo (puede pasar ?vendedor=<uuid> para filtrar a uno)."""
    from blueprints.auth import get_vendedor_filter
    vendedor_id = get_vendedor_filter()  # None si admin, uuid si vendedor
    q = Lead.query
    if vendedor_id:
        # Vendedor solo ve sus propios leads
        q = q.filter(Lead.usuario_asignado_id == vendedor_id)
    else:
        # Admin puede filtrar opcionalmente
        filtro = (request.args.get("vendedor") or "").strip()
        if filtro == "sin_asignar":
            q = q.filter(Lead.usuario_asignado_id.is_(None))
        elif filtro:
            try:
                uuid.UUID(filtro)
                q = q.filter(Lead.usuario_asignado_id == filtro)
            except (ValueError, TypeError):
                pass
    leads = q.order_by(Lead.fecha_actualizacion.desc()).all()
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
    # FIX-2026-06-23: validar sesión activa ANTES de cualquier procesamiento.
    # Si el vendedor dejó el modal abierto mucho tiempo y se le venció la
    # sesión, retornamos 401 claro para que recargue y vuelva a entrar,
    # en vez de crear un lead "huérfano" sin asignar.
    if not session.get("user_id"):
        return jsonify({
            "error": "Tu sesión expiró. Recarga la página y vuelve a iniciar sesión. Tu información no se guardó.",
            "session_expired": True,
        }), 401

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
        # Override: si está seteada META_LEADS_ASSIGNEE_USUARIO_ID, todos los
        # leads de Meta van directo a ese usuario (no Round-Robin).
        import os as _os
        from models import Usuario
        override_uid = _os.environ.get("META_LEADS_ASSIGNEE_USUARIO_ID", "").strip()
        if override_uid:
            target_user = db.session.get(Usuario, override_uid)
            if target_user:
                # Asignar directo y crear el lead manualmente
                etapa = EtapaPipeline.NUEVO_LEAD
                lead = Lead(
                    nombre=data.get("nombre", "Sin nombre"),
                    telefono=data.get("telefono"),
                    empresa_nombre=data.get("empresa_nombre") or data.get("empresa"),
                    estado_cliente=data.get("estado_cliente") or data.get("estado"),
                    origen=origen_enum,
                    marca_interes=marca,
                    etapa_pipeline=etapa,
                    cantidad_productos=cantidad,
                    precio_unitario=precio,
                    valor_estimado=valor,
                    usuario_asignado_id=target_user.id,
                    tipo_industria=data.get("tipo_industria"),
                    tamano_empresa=data.get("tamano_empresa"),
                    num_sucursales=data.get("num_sucursales"),
                    tipo_cliente=data.get("tipo_cliente"),
                    tipo_venta=data.get("tipo_venta"),
                    notas=data.get("notas"),
                )
                try:
                    _apply_icp(lead)
                except Exception:
                    pass
                db.session.add(lead)
                try:
                    db.session.commit()
                    socketio.emit("nuevo_lead", lead.to_dict())
                    return jsonify(lead.to_dict()), 201
                except Exception as e:
                    db.session.rollback()
                    from flask import current_app
                    current_app.logger.warning("[crear_lead Meta override] falló, fallback RR: %s", e)
                    # Cae al fallback round-robin de abajo

        # Fallback: Round-Robin tradicional
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

    # Etapa override (default NUEVO_LEAD; modal manual permite "calificado" → COTIZACION)
    etapa = EtapaPipeline.NUEVO_LEAD
    etapa_str = data.get("etapa_pipeline")
    if etapa_str:
        try:
            etapa = EtapaPipeline(etapa_str)
        except ValueError:
            pass

    # Asignación: orden de prioridad
    #   1. usuario_asignado_id explícito en el payload
    #   2. session["usuario_id"] (perfil comercial, populado en login)
    #   3. Fallback: derivar de users_crm.usuario_id usando session["user_id"]
    #      Esto evita que un usuario con sesión "stale" (logueado antes de
    #      vincular su perfil) cree leads sin asignar.
    asignado = data.get("usuario_asignado_id") or session.get("usuario_id")
    if not asignado and session.get("user_id"):
        from models import UserCRM
        uc = db.session.get(UserCRM, session["user_id"])
        if uc and uc.usuario_id:
            asignado = str(uc.usuario_id)
            session["usuario_id"] = asignado  # refresca para próximas requests

    # Validar tipo_cliente contra el CHECK del DB (Recurrente|Eventual) — todo
    # lo demás (incluido "Nuevo" del modal viejo) se mapea a NULL para evitar
    # IntegrityError silencioso.
    tipo_cliente_raw = data.get("tipo_cliente")
    tipo_cliente_val = tipo_cliente_raw if tipo_cliente_raw in ("Recurrente", "Eventual") else None

    # Auto-vincular Account si viene empresa_nombre o explicit account_id
    from models import Account, Contact
    from flask import current_app
    import traceback as _tb

    account_id = data.get("account_id")
    empresa_str = (data.get("empresa_nombre") or data.get("empresa") or "").strip()
    _estado_in     = data.get("estado_cliente") or data.get("estado")
    _industria_in  = data.get("tipo_industria")
    _tamano_in     = data.get("tamano_empresa")
    _sucursales_in = data.get("num_sucursales")
    step = "init"
    try:
        if not account_id and empresa_str:
            step = "buscar_account"
            existing = Account.query.filter(
                db.func.lower(Account.nombre) == empresa_str.lower()
            ).first()
            if existing:
                account_id = existing.id
                # Backfill: si la cuenta vieja no tenía estos datos y el modal
                # los provee ahora, llenarlos (no overwrite si ya hay valor).
                if _estado_in     and not existing.estado:           existing.estado          = _estado_in
                if _industria_in  and not existing.industria:        existing.industria       = _industria_in
                if _tamano_in     and not existing.tamano:           existing.tamano          = _tamano_in
                if _sucursales_in and not existing.num_sucursales:   existing.num_sucursales  = _sucursales_in
                if asignado       and not existing.owner_id:         existing.owner_id        = asignado
            else:
                step = "crear_account"
                new_acc = Account(
                    nombre=empresa_str,
                    estado=_estado_in,
                    num_sucursales=_sucursales_in,
                    industria=_industria_in,
                    tamano=_tamano_in,
                    owner_id=asignado,
                )
                db.session.add(new_acc)
                try:
                    db.session.flush()
                    account_id = new_acc.id
                except Exception as race_e:
                    # FIX-2026-06-23: race condition — otro vendedor creó la
                    # misma empresa mientras buscábamos. Rollback el flush
                    # del Account y reusar el existente.
                    err_str = str(race_e).lower()
                    if "unique" in err_str or "duplicate" in err_str:
                        db.session.rollback()
                        existing = Account.query.filter(
                            db.func.lower(Account.nombre) == empresa_str.lower()
                        ).first()
                        if existing:
                            account_id = existing.id
                            current_app.logger.info(
                                f"[crear_lead] race detectada en empresa '{empresa_str}', "
                                f"reusando account_id={existing.id}"
                            )
                        else:
                            raise  # no era race, re-lanzar
                    else:
                        raise

        # Auto-vincular Contact si viene nombre+telefono y no existe
        contact_id = data.get("contact_id")
        nombre_contacto = data.get("nombre")
        if not contact_id and nombre_contacto and data.get("telefono"):
            step = "buscar_contact"
            existing_c = Contact.query.filter(Contact.telefono == data["telefono"]).first()
            if existing_c:
                contact_id = existing_c.id
            else:
                step = "crear_contact"
                new_c = Contact(
                    nombre=nombre_contacto, telefono=data["telefono"],
                    whatsapp=data["telefono"], account_id=account_id,
                )
                db.session.add(new_c)
                db.session.flush()
                contact_id = new_c.id

        step = "construir_lead"
        lead = Lead(
            nombre=data.get("nombre", "Sin nombre"),
            telefono=data.get("telefono"),
            empresa_nombre=empresa_str or None,  # legacy compat
            account_id=account_id,
            contact_id=contact_id,
            estado_cliente=data.get("estado_cliente") or data.get("estado"),
            origen=origen_enum,
            marca_interes=marca,
            etapa_pipeline=etapa,
            cantidad_productos=cantidad,
            precio_unitario=precio,
            valor_estimado=valor,
            usuario_asignado_id=asignado,
            tipo_industria=data.get("tipo_industria"),
            tamano_empresa=data.get("tamano_empresa"),
            num_sucursales=data.get("num_sucursales"),
            tipo_cliente=tipo_cliente_val,
            tipo_venta=data.get("tipo_venta"),
            notas=data.get("notas"),
        )

        step = "icp"
        # FIX-2026-06-23: savepoint para _apply_icp. Si la función modifica
        # state pero falla a mitad (ej. consulta a Account.industria explota),
        # el SAVEPOINT permite hacer rollback solo de esa porción sin perder
        # el Account/Contact ya creados arriba.
        try:
            with db.session.begin_nested():
                _apply_icp(lead)
        except Exception as icp_err:
            current_app.logger.warning("[crear_lead] _apply_icp falló (continúo): %s", icp_err)

        step = "commit"
        db.session.add(lead)
        db.session.commit()

        step = "post_commit"
        try:
            log_actividad("crear", "lead", lead.id, f"Lead creado: {lead.nombre} ({lead.telefono})")
        except Exception as e:
            current_app.logger.warning("[crear_lead] log_actividad falló: %s", e)
        # FIX-2026-06-23: socketio.emit en background con gevent.spawn para
        # que el response no se bloquee si el broker está lento o caído.
        # Antes si el socket tardaba, el vendedor veía "Pensando..." más
        # tiempo del necesario aunque su lead ya estaba en BD.
        try:
            import gevent
            lead_dict = lead.to_dict()
            gevent.spawn(lambda: socketio.emit("nuevo_lead", lead_dict))
        except Exception as e:
            current_app.logger.warning("[crear_lead] socketio.emit falló: %s", e)

        return jsonify(lead.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        current_app.logger.error("[crear_lead] step=%s falló: %s\n%s", step, e, _tb.format_exc())
        # Caso típico: dup por teléfono
        if step == "commit":
            existing = Lead.query.filter_by(telefono=data.get("telefono")).first()
            if existing:
                return jsonify({"error": f"Ya existe un lead con este teléfono: {existing.nombre}", "lead": existing.to_dict()}), 409
        msg = str(getattr(e, "orig", e))[:400]
        return jsonify({
            "error": f"Error en paso '{step}': {type(e).__name__}: {msg}",
            "step": step,
        }), 500


@leads_bp.route("/check-duplicate", methods=["GET"])
def check_duplicate():
    """GET /api/leads/check-duplicate?phone=52... — devuelve el Lead existente
    si hay match por últimos 10 dígitos. Para validación inline del modal."""
    phone = request.args.get("phone", "").strip()
    digits = re.sub(r"\D", "", phone)[-10:]
    if len(digits) < 10:
        return jsonify({"duplicate": False})
    lead = (
        Lead.query
        .filter(Lead.telefono.like(f"%{digits}"))
        .order_by(Lead.fecha_creacion.desc())
        .first()
    )
    if not lead:
        return jsonify({"duplicate": False})
    asignado_nombre = lead.usuario_asignado.nombre if lead.usuario_asignado else None
    return jsonify({
        "duplicate": True,
        "lead": {
            "id": str(lead.id),
            "nombre": lead.nombre,
            "empresa_nombre": lead.empresa_nombre,
            "telefono": lead.telefono,
            "etapa": lead.etapa_pipeline.value if lead.etapa_pipeline else None,
            "asignado_a": asignado_nombre,
        },
    })


@leads_bp.route("/empresa-search", methods=["GET"])
def empresa_search():
    """GET /api/leads/empresa-search?q=foo — autocomplete por empresa_nombre.
    Devuelve hasta 10 nombres únicos para el modal."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    rows = (
        db.session.query(Lead.empresa_nombre)
        .filter(Lead.empresa_nombre.isnot(None), Lead.empresa_nombre != "")
        .filter(Lead.empresa_nombre.ilike(like))
        .distinct().limit(10).all()
    )
    return jsonify([r[0] for r in rows])


@leads_bp.route("/me", methods=["GET"])
def me():
    """Datos del usuario en sesión + sus marcas (especialidad_marca de Usuario).
    Para que el modal sepa qué unidades mostrar (multi-tenant)."""
    user_id = session.get("user_id")
    usuario_id = session.get("usuario_id")
    rol = session.get("user_rol", "")
    nombre = session.get("user_nombre", "")

    marcas = []
    if usuario_id:
        u = db.session.get(Usuario, usuario_id)
        if u and u.especialidad_marca:
            marcas = list(u.especialidad_marca)

    is_admin = rol.lower().replace(" ", "_") == "super_admin"
    return jsonify({
        "user_id": user_id, "usuario_id": usuario_id,
        "nombre": nombre, "rol": rol, "is_admin": is_admin,
        "marcas": marcas,  # ej. ["Aromatex", "Pestex"]
    })


@leads_bp.route("/<uuid:lead_id>/mover", methods=["PATCH"])
def mover_lead(lead_id):
    from blueprints.auth import get_vendedor_filter
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    # SECURITY-2026-06-24: vendedor solo puede mover SUS leads
    vendedor_id = get_vendedor_filter()
    if vendedor_id and str(lead.usuario_asignado_id) != str(vendedor_id):
        return jsonify({"error": "No tienes permisos sobre este lead"}), 403

    data = request.get_json() or {}
    try:
        nueva_etapa = EtapaPipeline(data.get("etapa_pipeline"))
    except (ValueError, KeyError):
        return jsonify({"error": "Etapa invalida"}), 400

    # SECURITY-2026-06-24: validar transiciones de etapa (state machine)
    # Reglas:
    #   - Admin puede mover libre
    #   - Vendedor: solo orden lineal o regresar; Cerrado Ganado SOLO desde
    #     Cotización/Demo/Negociación/Presentación; Cerrado Perdido siempre OK
    #     (descalificar en cualquier momento); de un Cerrado NO se puede salir.
    if vendedor_id:  # solo vendedor (admin pasa libre)
        orden = [
            EtapaPipeline.NUEVO_LEAD, EtapaPipeline.CONTACTO_1, EtapaPipeline.CONTACTO_2,
            EtapaPipeline.CONTACTO_3, EtapaPipeline.CONTACTO_4, EtapaPipeline.PRESENTACION,
            EtapaPipeline.COTIZACION, EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION,
        ]
        etapas_cerradas = (EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO)
        etapas_pre_ganado = (EtapaPipeline.PRESENTACION, EtapaPipeline.COTIZACION,
                             EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION)
        actual = lead.etapa_pipeline

        if actual in etapas_cerradas:
            return jsonify({"error": f"Lead ya está '{actual.value}' — no puede salir de ahí"}), 400

        if nueva_etapa == EtapaPipeline.CIERRE_PERDIDO:
            pass  # descalificar siempre permitido
        elif nueva_etapa == EtapaPipeline.CIERRE_GANADO:
            if actual not in etapas_pre_ganado:
                return jsonify({"error": f"Solo se puede Cerrar Ganado desde Presentación/Cotización/Demo/Negociación (estás en '{actual.value}')"}), 400
        else:
            # Mover entre etapas del pipeline normal — permitir adelantar o regresar
            # mientras ambas estén en el orden lineal. Bloquea brincos a etapas inexistentes.
            if nueva_etapa not in orden or actual not in orden:
                return jsonify({"error": "Transición no permitida"}), 400

    etapa_anterior = lead.etapa_pipeline.value
    lead.etapa_pipeline = nueva_etapa
    db.session.commit()

    log_actividad("mover", "lead", lead.id, f"{lead.nombre}: {etapa_anterior} → {nueva_etapa.value}")
    socketio.emit("lead_movido", {
        "lead_id": str(lead.id),
        "etapa_pipeline": nueva_etapa.value,
    })

    # Enviar evento de conversión a Meta CAPI
    send_pipeline_event(lead, nueva_etapa.value)

    return jsonify(lead.to_dict())


@leads_bp.route("/<uuid:lead_id>", methods=["PUT"])
def actualizar_lead(lead_id):
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}

    # Validar UUIDs antes de asignar para no romper la query
    for uuid_field in ("account_id", "contact_id"):
        if uuid_field in data and data[uuid_field] not in (None, ""):
            try:
                uuid.UUID(str(data[uuid_field]))
            except (ValueError, TypeError):
                return jsonify({"error": f"{uuid_field} inválido"}), 400

    for campo in ["nombre", "telefono", "marca_interes", "cantidad_productos",
                   "precio_unitario", "valor_estimado", "motivo_perdida",
                   "usuario_asignado_id", "tipo_industria", "tamano_empresa",
                   "num_sucursales", "tipo_cliente", "tipo_venta", "notas",
                   "account_id", "contact_id",
                   # FEAT 24-jun-2026: campos de factura para cierre ganado
                   "factura_numero", "factura_monto", "factura_notas"]:
        if campo in data:
            # Normalizar empty string a None para UUIDs
            value = data[campo]
            if campo in ("account_id", "contact_id") and value == "":
                value = None
            setattr(lead, campo, value)

    # factura_fecha como Date (viene "YYYY-MM-DD" del frontend)
    if "factura_fecha" in data:
        from datetime import date as _date
        v = data["factura_fecha"]
        if v in (None, ""):
            lead.factura_fecha = None
        else:
            try:
                lead.factura_fecha = _date.fromisoformat(v[:10])
            except (ValueError, TypeError):
                return jsonify({"error": "factura_fecha inválida (formato YYYY-MM-DD)"}), 400

    # factura_registrada_at: timestamp ISO con TZ
    if "factura_registrada_at" in data:
        from datetime import datetime as _dt
        v = data["factura_registrada_at"]
        if v in (None, ""):
            lead.factura_registrada_at = None
        else:
            try:
                lead.factura_registrada_at = _dt.fromisoformat(v.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass  # ignorar si formato malo

    if "etapa_pipeline" in data:
        try:
            nueva_etapa = EtapaPipeline(data["etapa_pipeline"])
        except ValueError:
            return jsonify({"error": "Etapa invalida"}), 400
        # SECURITY-2026-06-24: si es vendedor, validar transición igual que /mover
        from blueprints.auth import get_vendedor_filter as _gvf
        _vid = _gvf()
        if _vid and lead.etapa_pipeline != nueva_etapa:
            etapas_pre_ganado = (EtapaPipeline.PRESENTACION, EtapaPipeline.COTIZACION,
                                 EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION)
            etapas_cerradas = (EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO)
            if lead.etapa_pipeline in etapas_cerradas:
                return jsonify({"error": f"Lead ya está '{lead.etapa_pipeline.value}' — no puede salir de ahí"}), 400
            if nueva_etapa == EtapaPipeline.CIERRE_GANADO and lead.etapa_pipeline not in etapas_pre_ganado:
                return jsonify({"error": "Solo se puede Cerrar Ganado desde Presentación/Cotización/Demo/Negociación"}), 400
        lead.etapa_pipeline = nueva_etapa

    # Recalcular ICP si se modificaron campos relevantes
    icp_fields = {"tipo_industria", "tamano_empresa", "num_sucursales", "tipo_cliente"}
    if icp_fields & set(data.keys()):
        _apply_icp(lead)

    db.session.commit()
    return jsonify(lead.to_dict())


@leads_bp.route("/<uuid:lead_id>", methods=["DELETE"])
def eliminar_lead(lead_id):
    """Elimina un lead. Descubre dinámicamente todos los FKs a leads.id
    desde information_schema de la DB real y aplica SET NULL o DELETE
    según si la columna del child es nullable. Sirve cualquier schema."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError, SQLAlchemyError
    from flask import current_app
    import traceback
    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    lead_nombre = lead.nombre or "(sin nombre)"
    lid_str = str(lead_id)
    meta_lid = lead.meta_lead_id  # Capturar antes del delete

    # BUGFIX 24-jun-2026: si el lead vino de Meta, registrar el meta_lead_id
    # en meta_leads_dismissed para que el polling no lo recree en el siguiente
    # tick (cada 5 min). Antes: borrabas lead Meta → 5 min después reaparecía.
    if meta_lid:
        try:
            with db.engine.begin() as conn:
                conn.execute(text("""
                    INSERT INTO meta_leads_dismissed
                      (meta_lead_id, lead_id, lead_nombre, dismissed_by)
                    VALUES (:mid, :lid, :nom, :uid)
                    ON CONFLICT (meta_lead_id) DO NOTHING
                """), {
                    "mid": meta_lid, "lid": lid_str, "nom": lead_nombre[:200],
                    "uid": session.get("user_id"),
                })
            current_app.logger.info(
                "[delete-lead] meta_lead_id %s registrado en dismissed", meta_lid,
            )
        except Exception as e:
            current_app.logger.warning("[delete-lead] dismiss meta falló: %s", e)

    # Descubrir todos los FKs que apuntan a leads.id
    fk_discovery = text("""
        SELECT
            tc.table_name AS child_table,
            kcu.column_name AS child_column,
            c.is_nullable
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.key_column_usage AS kcu
            ON tc.constraint_name = kcu.constraint_name
           AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage AS ccu
            ON ccu.constraint_name = tc.constraint_name
           AND ccu.table_schema = tc.table_schema
        JOIN information_schema.columns c
            ON c.table_name = tc.table_name
           AND c.column_name = kcu.column_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND ccu.table_name = 'leads'
          AND ccu.column_name = 'id'
    """)
    try:
        fks = db.session.execute(fk_discovery).fetchall()
    except Exception as e:
        current_app.logger.error("[delete-lead] FK discovery falló: %s", e)
        fks = []

    current_app.logger.info(
        "[delete-lead] %s FKs reales a leads.id: %s",
        len(fks), [(r[0], r[1], r[2]) for r in fks],
    )

    cleanups_done = []
    cleanups_skipped = []
    for child_table, child_col, is_nullable in fks:
        if is_nullable == "YES":
            sql = f'UPDATE "{child_table}" SET "{child_col}" = NULL WHERE "{child_col}" = :id'
            label = f"NULL {child_table}.{child_col}"
        else:
            sql = f'DELETE FROM "{child_table}" WHERE "{child_col}" = :id'
            label = f"DEL {child_table}"

        # Cada cleanup en su propia conexión: si falla, no envenena la
        # sesión principal (evita PendingRollbackError - SQLAlchemy f405).
        try:
            with db.engine.begin() as conn:
                conn.execute(text(sql), {"id": lid_str})
            cleanups_done.append(label)
        except SQLAlchemyError as e:
            err_msg = str(getattr(e, "orig", e))[:120]
            current_app.logger.warning("[delete-lead] skip %s: %s", label, err_msg)
            cleanups_skipped.append(f"{label} ({err_msg[:50]})")

    # Expirar la session para que no intente walking de relationships
    # con tablas que tienen schema drift (ej. conversaciones sin lead_id/id).
    db.session.expunge_all()

    # Delete final del lead via RAW SQL en conexión fresca.
    # Evita que el ORM walke relaciones (Lead.conversaciones lazy=dynamic)
    # y dispare SELECT internos que rompen por columnas inexistentes
    # en tablas con schema drift (SQLAlchemy f405 / UndefinedColumn).
    try:
        with db.engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM leads WHERE id = :id"),
                {"id": lid_str},
            )
            if result.rowcount == 0:
                return jsonify({
                    "ok": True, "lead_nombre": lead_nombre,
                    "cleanups_done": cleanups_done,
                    "cleanups_skipped": cleanups_skipped,
                    "note": "Lead ya no existía al momento del delete final",
                })
    except IntegrityError as e:
        msg = str(e.orig)[:300] if e.orig else str(e)[:300]
        current_app.logger.error("[delete-lead] IntegrityError: %s\n%s", msg, traceback.format_exc())
        return jsonify({
            "error": f"FK constraint impide borrar: {msg}",
            "cleanups_done": cleanups_done,
            "cleanups_skipped": cleanups_skipped,
        }), 409
    except Exception as e:
        msg = str(getattr(e, "orig", e))[:300]
        current_app.logger.error("[delete-lead] error: %s\n%s", e, traceback.format_exc())
        return jsonify({
            "error": f"Error inesperado: {type(e).__name__}: {msg}",
            "cleanups_done": cleanups_done,
            "cleanups_skipped": cleanups_skipped,
        }), 500

    try:
        log_actividad("eliminar", "lead", None, f"Lead eliminado: {lead_nombre}")
    except Exception:
        pass
    return jsonify({
        "ok": True, "lead_nombre": lead_nombre,
        "cleanups_done": cleanups_done,
        "cleanups_skipped": cleanups_skipped,
    })


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
