# blueprints/leads.py
import csv
import io
import re
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, session, current_app, Response
from sqlalchemy import or_, func
from extensions import db, socketio
from models import Lead, EtapaPipeline, OrigenLead, Usuario
from icp_scoring import calcular_icp, INDUSTRIAS, TAMANOS
from blueprints.auth import require_role
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
    Super Admin ve todo (puede pasar ?vendedor=<uuid> para filtrar a uno).
    FEAT-2026-06-29: filtro global ?un=Aromatex/Pestex/Weldex/Nexo.
    Leads sin marca_interes siguen visibles aunque haya filtro
    (forzar al equipo a clasificarlos)."""
    from blueprints.auth import get_vendedor_filter, effective_un_from_request
    from un_filter import filtrar_leads_por_un
    vendedor_id = get_vendedor_filter()
    q = Lead.query
    if vendedor_id:
        q = q.filter(Lead.usuario_asignado_id == vendedor_id)
    else:
        filtro = (request.args.get("vendedor") or "").strip()
        if filtro == "sin_asignar":
            q = q.filter(Lead.usuario_asignado_id.is_(None))
        elif filtro:
            try:
                uuid.UUID(filtro)
                q = q.filter(Lead.usuario_asignado_id == filtro)
            except (ValueError, TypeError):
                pass
    # Filtro global por UN
    q = filtrar_leads_por_un(q, Lead, effective_un_from_request(request.args.get("un")))
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

    # FEAT-2026-08-25: el teléfono dejó de ser obligatorio, pero un lead sin
    # ninguna forma de contacto no sirve para nada. Se exige al menos uno.
    telefono_in = (data.get("telefono") or "").strip() or None
    email_in    = (data.get("email") or data.get("correo") or "").strip().lower() or None
    if not telefono_in and not email_in:
        return jsonify({"error": "Hace falta un teléfono o un correo para contactar al lead"}), 400
    if email_in and ("@" not in email_in or "." not in email_in.split("@")[-1]):
        return jsonify({"error": f"El correo '{email_in}' no parece válido"}), 400

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
                    telefono=telefono_in,
                    email=email_in,
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
                "telefono":           telefono_in,
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

        # Auto-vincular Contact. Antes exigía teléfono; ahora un lead que solo
        # trae correo también genera (o encuentra) su contacto.
        contact_id = data.get("contact_id")
        nombre_contacto = data.get("nombre")
        if not contact_id and nombre_contacto and (telefono_in or email_in):
            step = "buscar_contact"
            if telefono_in:
                existing_c = Contact.query.filter(Contact.telefono == telefono_in).first()
            else:
                existing_c = Contact.query.filter(
                    db.func.lower(Contact.email) == email_in).first()
            if existing_c:
                contact_id = existing_c.id
            else:
                step = "crear_contact"
                new_c = Contact(
                    nombre=nombre_contacto, telefono=telefono_in,
                    whatsapp=telefono_in, email=email_in, account_id=account_id,
                )
                db.session.add(new_c)
                db.session.flush()
                contact_id = new_c.id

        step = "construir_lead"
        lead = Lead(
            nombre=data.get("nombre", "Sin nombre"),
            telefono=telefono_in,
            email=email_in,
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
        # FEAT-2026-06-29: ya no hay UNIQUE(telefono), pero por si el
        # auto-migrate todavía no corrió en una réplica, mantenemos el
        # mensaje claro.
        if step == "commit" and "leads_telefono_key" in str(getattr(e, "orig", e)):
            existing = Lead.query.filter_by(telefono=data.get("telefono")).first()
            if existing:
                return jsonify({
                    "error": "Aún no se aplicó la migración que permite múltiples leads por cliente — espera 1 min y reintenta.",
                    "lead": existing.to_dict(),
                }), 409
        msg = str(getattr(e, "orig", e))[:400]
        return jsonify({
            "error": f"Error en paso '{step}': {type(e).__name__}: {msg}",
            "step": step,
        }), 500


@leads_bp.route("/check-duplicate", methods=["GET"])
def check_duplicate():
    """GET /api/leads/check-duplicate?phone=52... — devuelve TODOS los leads
    con el mismo cliente (match por últimos 10 dígitos del teléfono).

    FEAT-2026-06-29: ya no bloqueamos al duplicado. Mostramos lo que ya
    existe para que el vendedor decida: 'es otra venta al mismo cliente'
    (crea Lead nuevo) vs 'me confundí' (cancela).
    """
    phone = request.args.get("phone", "").strip()
    digits = re.sub(r"\D", "", phone)[-10:]
    if len(digits) < 10:
        return jsonify({"duplicate": False, "leads": []})
    leads = (
        Lead.query
        .filter(Lead.telefono.like(f"%{digits}"))
        .order_by(Lead.fecha_creacion.desc())
        .all()
    )
    if not leads:
        return jsonify({"duplicate": False, "leads": []})
    out = []
    for l in leads:
        asignado_nombre = l.usuario_asignado.nombre if l.usuario_asignado else None
        out.append({
            "id": str(l.id),
            "nombre": l.nombre,
            "empresa_nombre": l.empresa_nombre,
            "telefono": l.telefono,
            "etapa": l.etapa_pipeline.value if l.etapa_pipeline else None,
            "tipo_venta": l.tipo_venta,
            "asignado_a": asignado_nombre,
            "fecha_creacion": l.fecha_creacion.isoformat() if l.fecha_creacion else None,
            "factura_monto": float(l.factura_monto) if l.factura_monto else None,
        })
    return jsonify({"duplicate": True, "leads": out})


@leads_bp.route("/sospechosos-ventas-mezcladas", methods=["GET"])
def leads_sospechosos():
    """FEAT-2026-06-29: reporte para super_admin.

    Antes de quitar el UNIQUE(telefono), si un vendedor quería registrar
    OTRA venta al mismo cliente, no podía crear un Lead nuevo. La salida
    de escape era editar el lead existente (cambiar monto, agregar nota
    'también vendí...', etc.) → quedaron leads con datos de 2 ventas
    mezcladas.

    Este endpoint detecta leads con señales de mezcla para revisión humana.
    Solo Super Admin.
    """
    from blueprints.auth import get_vendedor_filter
    if get_vendedor_filter():
        return jsonify({"error": "Solo Super Admin"}), 403

    señales_clave = ("también", "tambien", " y vend", " y rent", "+ ",
                     "más renta", "mas renta", "más equipo", "mas equipo",
                     "y aroma", "y equipo")

    leads_ganados = (
        Lead.query.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
        .all()
    )

    sospechosos = []
    for l in leads_ganados:
        razones = []
        # Señal 1: factura_monto >> valor_estimado (más del doble)
        try:
            est = float(l.valor_estimado or 0)
            fac = float(l.factura_monto or 0)
            if est > 0 and fac > est * 2:
                razones.append(f"Factura ${fac:,.0f} es {fac/est:.1f}× el estimado ${est:,.0f}")
        except (ValueError, TypeError):
            pass
        # Señal 2: factura_notas tiene palabras de mezcla
        notas = (l.factura_notas or "").lower()
        for s in señales_clave:
            if s in notas:
                razones.append(f"Notas mencionan '{s.strip()}'")
                break
        # Señal 3: notas generales del lead con esas palabras
        notas_gen = (l.notas or "").lower()
        for s in señales_clave:
            if s in notas_gen and not any("Notas mencionan" in r for r in razones):
                razones.append(f"Notas del lead mencionan '{s.strip()}'")
                break
        # Señal 4: tipo_venta vacío en Cerrado Ganado (registrado pre-feat)
        if not l.tipo_venta:
            razones.append("Sin tipo_venta — pre feature de meta split")

        if not razones:
            continue
        sospechosos.append({
            "id": str(l.id),
            "nombre": l.nombre,
            "empresa_nombre": l.empresa_nombre,
            "telefono": l.telefono,
            "vendedor": l.usuario_asignado.nombre if l.usuario_asignado else None,
            "fecha_creacion": l.fecha_creacion.isoformat() if l.fecha_creacion else None,
            "valor_estimado": float(l.valor_estimado) if l.valor_estimado else None,
            "factura_monto": float(l.factura_monto) if l.factura_monto else None,
            "tipo_venta": l.tipo_venta,
            "marca_interes": l.marca_interes,
            "factura_notas": l.factura_notas,
            "razones": razones,
        })

    # Ordenar por # de razones desc, luego por monto
    sospechosos.sort(key=lambda x: (-len(x["razones"]), -(x["factura_monto"] or 0)))
    return jsonify({
        "total": len(sospechosos),
        "leads": sospechosos,
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

    # FIX-2026-08-25: antes era una comparación literal contra "super_admin",
    # así que Developer y los Super Admin segmentados no contaban como admin
    # y el modal les escondía opciones que sí les tocan.
    from blueprints.auth import is_admin_role
    is_admin = is_admin_role()

    # Un admin da de alta leads de cualquier unidad, no solo de las suyas.
    if is_admin:
        from un_filter import UN_CANONICAS
        marcas = list(UN_CANONICAS)

    # Vendedores a los que se les puede asignar el lead. Solo los admin
    # pueden asignar a otra persona; el resto crea a su propio nombre.
    vendedores = []
    if is_admin:
        vendedores = [
            {"id": str(u.id), "nombre": u.nombre,
             "marcas": list(u.especialidad_marca or []),
             "multi_un": bool(u.perfil_multi_un)}
            for u in Usuario.query.filter_by(en_turno=True).order_by(Usuario.nombre).all()
        ]

    return jsonify({
        "user_id": user_id, "usuario_id": usuario_id,
        "nombre": nombre, "rol": rol, "is_admin": is_admin,
        "marcas": marcas,          # ej. ["Aromatex", "Pestex"]
        "vendedores": vendedores,  # vacío si no es admin
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

    # FEAT-2026-08-21: propone quién hizo cada etapa del tabulador de
    # comisiones. Solo propone: nunca pisa una atribución ya corregida a
    # mano, y no falla el movimiento del lead si algo sale mal.
    #
    # FIX-2026-08-24: se atribuye a QUIEN MUEVE el lead, no a su dueño.
    # Antes se pasaba lead.usuario_asignado_id, así que las cuatro etapas
    # quedaban siempre a nombre de la misma persona; como aplica_tabulador()
    # exige dos participantes distintos, el tabulador no se activaba nunca.
    # Si no hay sesión (movimientos automáticos: bots, webhooks, ETL) se
    # conserva el dueño como responsable.
    try:
        from comisiones import registrar_avance
        quien_mueve = session.get("usuario_id") or lead.usuario_asignado_id
        registrar_avance(lead, nueva_etapa.value, quien_mueve)
    except Exception as e:
        current_app.logger.warning(f"[comisiones] atribución automática falló: {e}")

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

    for campo in ["nombre", "telefono", "email", "marca_interes", "cantidad_productos",
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
            if campo == "email" and value:
                value = str(value).strip().lower() or None
            setattr(lead, campo, value)

    # FEAT-2026-08-25: editar un lead no puede dejarlo sin ninguna forma de
    # contacto. Se valida sobre el resultado, no sobre lo que vino en el body:
    # borrar el teléfono está bien si ya tenía correo, y al revés.
    if not (lead.telefono or "").strip() and not (lead.email or "").strip():
        db.session.rollback()
        return jsonify({"error": "El lead se quedaría sin teléfono ni correo"}), 400

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


# ══════════════════════════════════════════════
# COMISIONES POR ETAPA — FEAT-2026-08-21
# Tabulador multi-UN: la bolsa que cada UN ya paga se reparte entre
# 4 etapas y cada quien cobra las que hizo.
# ══════════════════════════════════════════════
@leads_bp.route("/<uuid:lead_id>/comision", methods=["GET"])
def comision_lead(lead_id):
    """Devuelve la atribución del lead y el reparto que resultaría al
    cerrarlo. Sirve para revisar antes de que la venta se congele."""
    import comisiones as C
    from models import ComisionEtapa

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    unidad = (request.args.get("unidad") or lead.marca_interes or "").strip()
    try:
        lista = float(request.args.get("lista") or lead.valor_estimado or 0)
        cerrada = float(request.args.get("cerrada") or lista)
    except (TypeError, ValueError):
        return jsonify({"error": "Montos inválidos"}), 400

    atrib = C.atribucion_de(lead.id)
    etapas = [{"clave": e.clave, "nombre": e.nombre, "peso": float(e.peso),
               "descripcion": e.descripcion,
               "usuario_id": str(atrib[e.clave]) if atrib.get(e.clave) else None}
              for e in ComisionEtapa.query.order_by(ComisionEtapa.orden).all()]

    # El tabulador NO aplica a todos los leads: solo a ventas cruzadas de un
    # vendedor multi-UN en las que participó más de una persona. El resto
    # comisiona con el esquema de siempre.
    aplica, motivo = C.aplica_tabulador(lead)

    r = C.calcular(unidad, lista, cerrada, atrib) if unidad and lista else {
        "error": "Falta unidad de negocio o mensualidad de lista."}

    if r.get("error"):
        return jsonify({"lead_id": str(lead.id), "unidad": unidad,
                        "etapas": etapas, "aplica_tabulador": aplica,
                        "motivo": motivo, "error": r["error"]})

    return jsonify({
        "lead_id": str(lead.id), "unidad": unidad, "etapas": etapas,
        "aplica_tabulador": aplica, "motivo": motivo,
        "mensualidad_lista": float(r["mensualidad_lista"]),
        "mensualidad_cerrada": float(r["mensualidad_cerrada"]),
        "descuento": float(r["descuento"]),
        "tasa_base": float(r["tasa_base"]),
        "factor_descuento": float(r["factor_descuento"]),
        "etiqueta_descuento": r["etiqueta_descuento"],
        "requiere_autorizacion": r["requiere_autorizacion"],
        "comision_total": float(r["comision_total"]),
        "comision_sin_descuento": float(r["comision_sin_descuento"]),
        "detalle_etapas": [{"clave": d["clave"], "nombre": d["nombre"],
                            "peso": float(d["peso"]), "monto": float(d["monto"]),
                            "usuario_id": str(d["usuario_id"]) if d["usuario_id"] else None}
                           for d in r["detalle_etapas"]],
        "participaciones": [{"usuario_id": str(p["usuario_id"]), "nombre": p["nombre"],
                             "etapas": p["etapas"], "pct": float(p["pct"]),
                             "monto": float(p["monto"])}
                            for p in r["participaciones"]],
        "peso_sin_asignar": float(r["peso_sin_asignar"]),
        "monto_sin_asignar": float(r["monto_sin_asignar"]),
        "repartido": float(r["repartido"]),
        "cuadra": r["cuadra"],
        "nota_un": r["nota_un"],
    })


@leads_bp.route("/<uuid:lead_id>/comision/atribucion", methods=["POST"])
@require_role(["super_admin"])
def fijar_atribucion_lead(lead_id):
    """Corrección manual de quién hizo cada etapa. Body: {etapa, usuario_id}.
    usuario_id vacío deja la etapa sin responsable (no se paga).

    FIX-2026-08-24: esta ruta decide a quién se le paga y estaba abierta a
    cualquiera con sesión. La atribución automática sigue siendo de todos
    (la genera quien mueve el lead); corregirla a mano es de dirección.
    """
    import comisiones as C

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}
    clave = (data.get("etapa") or "").strip()
    if not clave:
        return jsonify({"error": "Falta la etapa"}), 400

    uid = (data.get("usuario_id") or "").strip() or None
    if uid:
        try:
            uuid.UUID(uid)
        except (ValueError, TypeError):
            return jsonify({"error": "usuario_id inválido"}), 400
        if not db.session.get(Usuario, uid):
            return jsonify({"error": "Vendedor no encontrado"}), 404

    C.fijar_atribucion(lead.id, clave, uid, session.get("user_nombre", ""))
    db.session.commit()
    log_actividad("editar", "lead", lead.id,
                  f"Atribución de comisión · {clave} → {uid or 'sin responsable'}")
    return jsonify({"ok": True, "etapa": clave, "usuario_id": uid})


# ══════════════════════════════════════════════
# CIERRE DE VENTA — FEAT-2026-08-24
#
# Hasta hoy, mover un lead a "Cerrado Ganado" no generaba nada: la tabla
# sales quedaba vacía y la comisión nunca se congelaba. Este es el único
# camino que crea la venta.
# ══════════════════════════════════════════════

# De dónde salió el lead decide si el vendedor lo generó (cobra completo) o
# se lo dieron (cobra la mitad). Es una propuesta: la pantalla de cierre lo
# muestra y el gerente puede cambiarlo antes de confirmar.
ORIGEN_A_COMISION = {
    "Prospeccion":   "autogenerado",
    "Upselling":     "autogenerado",
    "Cross-selling": "autogenerado",
    "Meta Ads":          "lead_otorgado",
    "Web":               "lead_otorgado",
    "WhatsApp Organico": "lead_otorgado",
}


@leads_bp.route("/<uuid:lead_id>/cierre/preview", methods=["POST"])
def preview_cierre(lead_id):
    """Cómo quedaría la venta con los montos que se están capturando, antes
    de confirmar. No escribe nada."""
    import comisiones as C
    from blueprints.sales import _calc_commission
    from models import Sale

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    data = request.get_json() or {}
    unidad = (data.get("unidad") or lead.marca_interes or "").strip()
    try:
        lista   = float(data.get("mensualidad_lista") or lead.valor_estimado or 0)
        cerrada = float(data.get("mensualidad_cerrada") or lista)
        total   = float(data.get("total_amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Montos inválidos"}), 400

    sale_type  = data.get("sale_type") or "suscripcion_nueva"
    com_type   = data.get("commission_type") or ORIGEN_A_COMISION.get(
        lead.origen.value if lead.origen else "", "lead_otorgado")

    aplica, motivo = C.aplica_tabulador(lead)
    out = {"aplica_tabulador": aplica, "motivo": motivo,
           "unidad": unidad, "commission_type": com_type,
           "venta_existente": None}

    ya = Sale.query.filter_by(lead_id=lead.id).first()
    if ya:
        out["venta_existente"] = {"id": str(ya.id),
                                  "comision": float(ya.commission_amount or 0),
                                  "cerrada_el": ya.closed_at.isoformat() if ya.closed_at else None}

    if aplica:
        r = C.calcular(unidad, lista, cerrada, C.atribucion_de(lead.id))
        if r.get("error"):
            out["error"] = r["error"]
            return jsonify(out)
        out.update({
            "esquema": "tabulador",
            "comision_total": float(r["comision_total"]),
            "descuento": float(r["descuento"]),
            "etiqueta_descuento": r["etiqueta_descuento"],
            "requiere_autorizacion": r["requiere_autorizacion"],
            "participaciones": [{"usuario_id": str(p["usuario_id"]), "nombre": p["nombre"],
                                 "etapas": p["etapas"], "pct": float(p["pct"]),
                                 "monto": float(p["monto"])}
                                for p in r["participaciones"]],
            "monto_sin_asignar": float(r["monto_sin_asignar"]),
            "nota_un": r["nota_un"],
        })
    else:
        rate, amount = _calc_commission(sale_type, com_type, cerrada, total)
        out.update({
            "esquema": "normal",
            "comision_total": round(amount, 2),
            "tasa": rate,
            "beneficiario": lead.usuario_asignado.nombre if lead.usuario_asignado else None,
        })
    return jsonify(out)


@leads_bp.route("/<uuid:lead_id>/cerrar", methods=["POST"])
def cerrar_lead(lead_id):
    """Cierra el lead como ganado: registra la venta, congela el reparto de
    comisión y mueve la etapa. Todo en una sola transacción.

    El reparto se congela a propósito: si mañana cambian los pesos del
    tabulador, lo ya cerrado no se mueve.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    import comisiones as C
    from blueprints.auth import get_vendedor_filter
    from blueprints.sales import _calc_commission, _parse_dt
    from models import Sale

    lead = db.session.get(Lead, lead_id)
    if not lead:
        return jsonify({"error": "Lead no encontrado"}), 404

    # Un vendedor solo cierra sus propios leads.
    vendedor_id = get_vendedor_filter()
    if vendedor_id and str(lead.usuario_asignado_id) != str(vendedor_id):
        return jsonify({"error": "No tienes permisos sobre este lead"}), 403

    # Una venta por lead. Si ya existe se devuelve, no se duplica.
    ya = Sale.query.filter_by(lead_id=lead.id).first()
    if ya:
        return jsonify({"error": "Este lead ya tiene una venta registrada",
                        "sale_id": str(ya.id)}), 409

    data = request.get_json() or {}
    unidad = (data.get("unidad") or lead.marca_interes or "").strip()
    if not unidad:
        return jsonify({"error": "Falta la unidad de negocio"}), 400

    sale_type = data.get("sale_type") or "suscripcion_nueva"
    if sale_type not in ("suscripcion_nueva", "servicio_unico", "upsell"):
        return jsonify({"error": "Tipo de venta inválido"}), 400

    try:
        lista   = float(data.get("mensualidad_lista") or lead.valor_estimado or 0)
        cerrada = float(data.get("mensualidad_cerrada") or lista)
        total   = float(data.get("total_amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Montos inválidos"}), 400

    if sale_type == "servicio_unico":
        if total <= 0:
            return jsonify({"error": "Un servicio único necesita monto total"}), 400
    elif cerrada <= 0:
        return jsonify({"error": "Falta la mensualidad cerrada"}), 400

    com_type = data.get("commission_type") or ORIGEN_A_COMISION.get(
        lead.origen.value if lead.origen else "", "lead_otorgado")
    if com_type not in ("autogenerado", "lead_otorgado"):
        return jsonify({"error": "Tipo de comisión inválido"}), 400

    # Quien cierra hizo la etapa de cierre. Se registra ANTES de calcular,
    # porque cambia quiénes participan y por lo tanto el reparto.
    quien_cierra = session.get("usuario_id") or lead.usuario_asignado_id
    try:
        C.registrar_avance(lead, EtapaPipeline.CIERRE_GANADO.value, quien_cierra)
        db.session.flush()
    except Exception as e:
        current_app.logger.warning(f"[comisiones] atribución de cierre falló: {e}")

    aplica, motivo = C.aplica_tabulador(lead)
    resultado = None
    if aplica:
        resultado = C.calcular(unidad, lista, cerrada, C.atribucion_de(lead.id))
        if resultado.get("error"):
            db.session.rollback()
            return jsonify({"error": resultado["error"]}), 400
        rate = None
        amount = float(resultado["comision_total"])
    else:
        rate, amount = _calc_commission(sale_type, com_type, cerrada, total)

    sale = Sale(
        lead_id=lead.id,
        user_id=lead.usuario_asignado_id,
        unit=unidad,
        sale_type=sale_type,
        sale_category=data.get("sale_category") or "recurrente",
        uen=data.get("uen"),
        lead_source=lead.origen.value if lead.origen else None,
        monthly_amount=Decimal(str(cerrada)),
        total_amount=Decimal(str(total or cerrada)),
        commission_type=com_type,
        commission_rate=Decimal(str(rate)) if rate is not None else None,
        commission_amount=Decimal(str(round(amount, 2))),
        closed_at=_parse_dt(data.get("closed_at")) or datetime.now(timezone.utc),
        contract_signed_at=_parse_dt(data.get("contract_signed_at")),
        first_payment_at=_parse_dt(data.get("first_payment_at")),
        service_start_at=_parse_dt(data.get("service_start_at")),
    )
    db.session.add(sale)
    db.session.flush()          # necesitamos sale.id para congelar el reparto

    if aplica:
        C.congelar_en_venta(sale, resultado)

    etapa_anterior = lead.etapa_pipeline.value
    lead.etapa_pipeline = EtapaPipeline.CIERRE_GANADO

    # FIX-2026-08-24: la respuesta se arma ANTES del commit.
    #
    # SQLAlchemy expira los objetos al confirmar, así que leer sale.id o
    # lead.nombre después dispara un SELECT nuevo. Si esa consulta falla —una
    # conexión caída del pooler basta— el endpoint revienta con 500 aunque la
    # venta YA quedó guardada. El vendedor ve un error, reintenta, y se topa
    # con el 409 de "este lead ya tiene una venta". Peor que un error: un
    # error mentiroso.
    out = {"ok": True, "sale_id": str(sale.id),
           "esquema": "tabulador" if aplica else "normal",
           "motivo": motivo, "comision_total": round(amount, 2)}
    if aplica:
        out["participaciones"] = [{"nombre": p["nombre"], "etapas": p["etapas"],
                                   "pct": float(p["pct"]), "monto": float(p["monto"])}
                                  for p in resultado["participaciones"]]
        out["monto_sin_asignar"] = float(resultado["monto_sin_asignar"])
    detalle_log = (f"{lead.nombre}: venta registrada · {unidad} · "
                   f"comisión ${amount:,.2f} · esquema "
                   f"{'tabulador por etapas' if aplica else 'normal'}")
    lead_id_str = str(lead.id)

    db.session.commit()

    # Nada de lo que sigue vuelve a tocar los objetos de la sesión, y ninguno
    # de estos pasos debe tumbar un cierre que ya está guardado.
    try:
        log_actividad("cerrar", "lead", lead_id_str, detalle_log)
    except Exception as e:
        current_app.logger.warning(f"[cierre] no se pudo registrar la actividad: {e}")
    try:
        socketio.emit("lead_movido", {"lead_id": lead_id_str,
                                      "etapa_anterior": etapa_anterior,
                                      "etapa_nueva": EtapaPipeline.CIERRE_GANADO.value})
    except Exception:
        pass

    return jsonify(out), 201


# ══════════════════════════════════════════════
# EXPORT CSV DEL PIPE — FEAT-2026-08-21
# ══════════════════════════════════════════════
EXPORT_PIPE_COLUMNAS = [
    "Etapa del pipeline", "Empresa", "Contacto", "Teléfono", "Unidad de negocio",
    "Origen", "Vendedor asignado", "Valor estimado",
    "Tipo de venta",                       # Recurrente / Eventual
    "ICP", "Nivel ICP",
    "Estado", "Tipo de cliente", "En nurturing", "Creado", "Último contacto",
    "Próximo contacto", "Días en el pipe",
    # Datos del cierre. Solo se llenan en los leads ganados.
    "Fecha de cierre", "Monto de cierre",
    "Factura", "Fecha de factura", "Monto facturado",
    "Forma de pago", "Condiciones y notas de pago",
    # Etapas del tabulador de comisiones: quién hizo cada una
    "Prospectar", "Cita", "Cotización y seguimiento", "Cierre",
    "Aplica tabulador", "Notas",
]


@leads_bp.route("/export.csv", methods=["GET"])
def exportar_pipe_csv():
    """Descarga el pipe en CSV, con la etapa de cada lead y quién hizo cada
    etapa del tabulador de comisiones.

    Respeta EXACTAMENTE los mismos filtros que listar_leads(): un vendedor
    solo exporta sus leads, y el filtro global por UN aplica igual. Sin esto
    el botón sería una fuga de datos.

    Acepta ?etapa= para exportar una sola columna del tablero.
    """
    from blueprints.auth import get_vendedor_filter, effective_un_from_request
    from un_filter import filtrar_leads_por_un
    import comisiones as C

    vendedor_id = get_vendedor_filter()
    q = Lead.query
    if vendedor_id:
        q = q.filter(Lead.usuario_asignado_id == vendedor_id)
    else:
        filtro = (request.args.get("vendedor") or "").strip()
        if filtro == "sin_asignar":
            q = q.filter(Lead.usuario_asignado_id.is_(None))
        elif filtro:
            try:
                uuid.UUID(filtro)
                q = q.filter(Lead.usuario_asignado_id == filtro)
            except (ValueError, TypeError):
                pass
    q = filtrar_leads_por_un(q, Lead, effective_un_from_request(request.args.get("un")))

    etapa_filtro = (request.args.get("etapa") or "").strip()
    if etapa_filtro:
        try:
            q = q.filter(Lead.etapa_pipeline == EtapaPipeline(etapa_filtro))
        except ValueError:
            return jsonify({"error": f"Etapa desconocida: {etapa_filtro}"}), 400

    leads = q.order_by(Lead.etapa_pipeline, Lead.fecha_actualizacion.desc()).all()

    # Atribuciones y vendedores en dos consultas, no una por lead.
    from models import LeadAtribucion
    ids = [l.id for l in leads]
    atrib = {}
    if ids:
        for a in LeadAtribucion.query.filter(LeadAtribucion.lead_id.in_(ids)).all():
            atrib.setdefault(str(a.lead_id), {})[a.etapa] = a.usuario_id
    nombres = {str(u.id): u.nombre for u in Usuario.query.all()}
    perfiles = {str(u.id): bool(u.perfil_multi_un) for u in Usuario.query.all()}

    # Datos del cierre, también en dos consultas y no una por lead.
    from models import Sale, Cotizacion
    ventas = {}
    pagos = {}
    if ids:
        for s in Sale.query.filter(Sale.lead_id.in_(ids)).all():
            ventas[str(s.lead_id)] = s
        # La forma de pago vive en la cotización, no en el lead. Nos quedamos
        # con la más reciente de cada uno, que es la que se terminó cerrando.
        for c in (Cotizacion.query
                  .filter(Cotizacion.lead_id.in_(ids))
                  .order_by(Cotizacion.lead_id, Cotizacion.fecha.asc()).all()):
            if c.condiciones_pago:
                pagos[str(c.lead_id)] = c.condiciones_pago

    def _fecha(v):
        return v.strftime("%Y-%m-%d") if v else ""

    hoy = datetime.utcnow()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(EXPORT_PIPE_COLUMNAS)

    for l in leads:
        a = atrib.get(str(l.id), {})
        dueno = str(l.usuario_asignado_id) if l.usuario_asignado_id else None

        # Mismo criterio que aplica_tabulador(), sin volver a consultar la BD
        participantes = {u for u in a.values() if u}
        aplica = bool(dueno and perfiles.get(dueno) and len(participantes) >= 2)

        dias = (hoy - l.fecha_creacion.replace(tzinfo=None)).days if l.fecha_creacion else ""

        # Fecha de cierre: la de la venta es la buena. Los leads cerrados antes
        # de que existieran las ventas no la tienen, así que se cae a la fecha
        # de factura. No se usa fecha_actualizacion: cambia con cualquier edición
        # y daría una fecha de cierre falsa.
        venta = ventas.get(str(l.id))
        f_cierre = _fecha(venta.closed_at) if venta else (
            _fecha(l.factura_fecha) or _fecha(l.factura_registrada_at))
        monto_cierre = (float(venta.monthly_amount or venta.total_amount or 0)
                        if venta else "")

        w.writerow([
            l.etapa_pipeline.value if l.etapa_pipeline else "",
            l.empresa_nombre or "",
            l.nombre or "",
            l.telefono or "",
            l.marca_interes or "",
            l.origen.value if l.origen else "",
            nombres.get(dueno, "") if dueno else "Sin asignar",
            float(l.valor_estimado) if l.valor_estimado is not None else "",
            l.tipo_venta or "",
            l.icp_score if l.icp_score is not None else "",
            l.icp_nivel or "",
            l.estado_cliente or "",
            l.tipo_cliente or "",
            "sí" if l.en_nurturing else "no",
            l.fecha_creacion.strftime("%Y-%m-%d") if l.fecha_creacion else "",
            l.fecha_ultimo_contacto.strftime("%Y-%m-%d") if l.fecha_ultimo_contacto else "",
            l.proximo_contacto.strftime("%Y-%m-%d") if l.proximo_contacto else "",
            dias,
            f_cierre,
            monto_cierre,
            l.factura_numero or "",
            _fecha(l.factura_fecha),
            float(l.factura_monto) if l.factura_monto is not None else "",
            pagos.get(str(l.id), ""),
            (l.factura_notas or "").replace("\n", " ").replace("\r", " "),
            nombres.get(str(a.get("prospectar")), "") if a.get("prospectar") else "",
            nombres.get(str(a.get("cita")), "") if a.get("cita") else "",
            nombres.get(str(a.get("cotizacion")), "") if a.get("cotizacion") else "",
            nombres.get(str(a.get("cierre")), "") if a.get("cierre") else "",
            "sí" if aplica else "no",
            (l.notas or "").replace("\n", " ").replace("\r", " "),
        ])

    sufijo = f"_{etapa_filtro.replace(' ', '-')}" if etapa_filtro else ""
    nombre = f"pipe{sufijo}_{hoy.strftime('%Y%m%d_%H%M')}.csv"
    # BOM para que Excel en Windows abra los acentos correctamente
    return Response(
        "﻿" + out.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
