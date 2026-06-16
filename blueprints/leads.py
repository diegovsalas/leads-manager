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

    # Asignación: si manual y no viene usuario_asignado_id, usar el usuario en sesión.
    # Solo usuario_id (FK a usuarios). NO caer a user_id (es FK a users, FK violation).
    asignado = data.get("usuario_asignado_id") or session.get("usuario_id")

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
    step = "init"
    try:
        if not account_id and empresa_str:
            step = "buscar_account"
            existing = Account.query.filter(
                db.func.lower(Account.nombre) == empresa_str.lower()
            ).first()
            if existing:
                account_id = existing.id
            else:
                step = "crear_account"
                new_acc = Account(
                    nombre=empresa_str,
                    estado=data.get("estado_cliente") or data.get("estado"),
                    num_sucursales=data.get("num_sucursales"),
                    industria=data.get("tipo_industria"),
                    tamano=data.get("tamano_empresa"),
                    owner_id=asignado,
                )
                db.session.add(new_acc)
                db.session.flush()
                account_id = new_acc.id

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
        try:
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
        try:
            socketio.emit("nuevo_lead", lead.to_dict())
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
                   "account_id", "contact_id"]:
        if campo in data:
            # Normalizar empty string a None para UUIDs
            value = data[campo]
            if campo in ("account_id", "contact_id") and value == "":
                value = None
            setattr(lead, campo, value)

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
