# blueprints/metas.py
"""
Metas mensuales por vendedor.
FEAT-2026-06-25: dos metas separadas por tipo_venta del lead (Recurrente / Eventual).
- Super Admin: CRUD + resumen del equipo
- Vendedor: solo lectura de su propio progreso
"""
from datetime import date
from flask import Blueprint, request, jsonify, session
from sqlalchemy import func
from extensions import db
from models import MetaVendedor, Usuario, Lead, EtapaPipeline
from blueprints.auth import require_role, get_vendedor_filter

metas_bp = Blueprint("metas", __name__)


def _mes_actual():
    return date.today().strftime("%Y-%m")


def _calcular_ventas(usuario_id, mes, tipo_venta=None):
    """Revenue cerrado ganado de un vendedor en un mes, opcionalmente
    filtrado por tipo_venta del lead ('Recurrente' / 'Eventual' / None=todos).
    Prioriza factura_monto (monto real cobrado) sobre el cálculo estimado."""
    year, month = mes.split("-")
    inicio = date(int(year), int(month), 1)
    if inicio.month == 12:
        fin = inicio.replace(year=inicio.year + 1, month=1)
    else:
        fin = inicio.replace(month=inicio.month + 1)

    q = db.session.query(
        func.coalesce(func.sum(func.coalesce(
            Lead.factura_monto,
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0)
    ).filter(
        Lead.usuario_asignado_id == usuario_id,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
        Lead.fecha_creacion >= inicio,
        Lead.fecha_creacion < fin,
    )
    if tipo_venta:
        q = q.filter(Lead.tipo_venta == tipo_venta)

    return float(q.scalar() or 0)


def _calc_pct(ventas, meta):
    return round((ventas / meta) * 100, 1) if meta and meta > 0 else 0


@metas_bp.route("/", methods=["GET"])
@require_role(["super_admin"])
def listar_metas():
    """Super Admin: todas las metas del mes (o mes especificado)."""
    mes = request.args.get("mes", _mes_actual())
    metas = MetaVendedor.query.filter_by(mes=mes).all()
    return jsonify([m.to_dict() for m in metas])


@metas_bp.route("/", methods=["POST"])
@require_role(["super_admin"])
def crear_o_actualizar_meta():
    """
    Super Admin: crear o actualizar meta. Acepta cualquier subset de:
      { usuario_id, mes, meta_mxn, meta_recurrente_mxn, meta_eventual_mxn }
    Solo aplica los campos enviados (deja los demás como estaban).
    HOTFIX-2026-06-25: tracking detallado del paso que falla y validación
    de FKs antes del commit para evitar errores opacos en el toast.
    """
    import logging
    log = logging.getLogger("metas")

    step = "parse_body"
    try:
        data = request.get_json() or {}
        usuario_id = data.get("usuario_id")
        mes = data.get("mes", _mes_actual())

        if not usuario_id:
            return jsonify({"error": "usuario_id requerido"}), 400

        # Validar que el usuario_id sea un UUID existente en `usuarios`
        step = "validate_usuario_fk"
        from models import Usuario, UserCRM
        usuario = db.session.get(Usuario, usuario_id)
        if not usuario:
            return jsonify({"error": f"Vendedor {usuario_id[:8]}... no existe en usuarios. ¿Está su perfil comercial creado?"}), 400

        montos_enviados = {
            k: data.get(k) for k in
            ("meta_mxn", "meta_recurrente_mxn", "meta_eventual_mxn")
            if k in data
        }
        if not montos_enviados:
            return jsonify({"error": "Envía al menos un monto (meta_mxn, meta_recurrente_mxn o meta_eventual_mxn)"}), 400

        step = "upsert_lookup"
        meta = MetaVendedor.query.filter_by(usuario_id=usuario_id, mes=mes).first()
        if not meta:
            # Validar created_by FK antes de incluirlo en el INSERT.
            # Si el user_id de la sesión no existe en users_crm (corner case),
            # dejamos created_by como NULL para no romper el INSERT.
            step = "validate_created_by_fk"
            sess_uid = session.get("user_id")
            created_by_valid = None
            if sess_uid:
                creator = db.session.get(UserCRM, sess_uid)
                if creator:
                    created_by_valid = sess_uid
                else:
                    log.warning(f"metas POST: session.user_id {sess_uid} no existe en users_crm; created_by=NULL")

            step = "insert_meta_row"
            meta = MetaVendedor(
                usuario_id=usuario_id,
                mes=mes,
                created_by=created_by_valid,
            )
            db.session.add(meta)

        step = "apply_montos"
        for k, v in montos_enviados.items():
            # Permitir limpiar la meta enviando 0/null
            if v in (None, "", 0, "0"):
                setattr(meta, k, None)
            else:
                try:
                    setattr(meta, k, float(v))
                except (ValueError, TypeError):
                    return jsonify({"error": f"{k} debe ser numérico"}), 400

        step = "commit"
        db.session.commit()
        return jsonify(meta.to_dict()), 201

    except Exception as e:
        db.session.rollback()
        log.exception(f"metas POST falló en paso '{step}' (usuario_id={data.get('usuario_id') if 'data' in locals() else '?'})")
        return jsonify({
            "error": f"Error en paso '{step}': {type(e).__name__}: {str(e)[:200]}",
            "step": step,
        }), 500


@metas_bp.route("/mi-progreso", methods=["GET"])
def mi_progreso():
    """Vendedor: sus DOS metas (Recurrente / Eventual) + ventas + % del mes actual."""
    usuario_id = session.get("usuario_id")
    if not usuario_id:
        return jsonify({"error": "Sin vendedor vinculado"}), 400

    mes = request.args.get("mes", _mes_actual())
    meta = MetaVendedor.query.filter_by(usuario_id=usuario_id, mes=mes).first()

    meta_rec = float(meta.meta_recurrente_mxn) if meta and meta.meta_recurrente_mxn else 0
    meta_ev  = float(meta.meta_eventual_mxn)   if meta and meta.meta_eventual_mxn   else 0
    meta_legacy = float(meta.meta_mxn) if meta and meta.meta_mxn else 0

    ventas_rec = _calcular_ventas(usuario_id, mes, tipo_venta="Recurrente")
    ventas_ev  = _calcular_ventas(usuario_id, mes, tipo_venta="Eventual")
    ventas_total = _calcular_ventas(usuario_id, mes, tipo_venta=None)

    return jsonify({
        "usuario_id": usuario_id,
        "mes": mes,
        # Nuevo formato (FEAT-2026-06-25)
        "meta_recurrente_mxn": meta_rec,
        "ventas_recurrente":   ventas_rec,
        "pct_recurrente":      _calc_pct(ventas_rec, meta_rec),
        "meta_eventual_mxn":   meta_ev,
        "ventas_eventual":     ventas_ev,
        "pct_eventual":        _calc_pct(ventas_ev, meta_ev),
        # Legacy (backward-compat con UI viejo)
        "meta_mxn":            meta_legacy or (meta_rec + meta_ev),
        "ventas_actual":       ventas_total,
        "porcentaje":          _calc_pct(ventas_total, meta_legacy or (meta_rec + meta_ev)),
        "tiene_meta":          bool(meta and (meta_rec or meta_ev or meta_legacy)),
        "tiene_meta_recurrente": bool(meta_rec),
        "tiene_meta_eventual":   bool(meta_ev),
    })


@metas_bp.route("/resumen-equipo", methods=["GET"])
@require_role(["super_admin"])
def resumen_equipo():
    """Super Admin: tabla comparativa de todos los vendedores con DOS metas y DOS avances."""
    mes = request.args.get("mes", _mes_actual())

    vendedores = Usuario.query.filter(Usuario.en_turno.is_(True)).order_by(Usuario.nombre).all()
    resultado = []

    for v in vendedores:
        meta = MetaVendedor.query.filter_by(usuario_id=v.id, mes=mes).first()

        meta_rec    = float(meta.meta_recurrente_mxn) if meta and meta.meta_recurrente_mxn else 0
        meta_ev     = float(meta.meta_eventual_mxn)   if meta and meta.meta_eventual_mxn   else 0
        meta_legacy = float(meta.meta_mxn) if meta and meta.meta_mxn else 0

        ventas_rec = _calcular_ventas(v.id, mes, tipo_venta="Recurrente")
        ventas_ev  = _calcular_ventas(v.id, mes, tipo_venta="Eventual")
        ventas_total = _calcular_ventas(v.id, mes, tipo_venta=None)

        leads_activos = Lead.query.filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
        ).count()

        resultado.append({
            "usuario_id": str(v.id),
            "nombre": v.nombre,
            "meta_recurrente_mxn": meta_rec,
            "ventas_recurrente":   ventas_rec,
            "pct_recurrente":      _calc_pct(ventas_rec, meta_rec),
            "meta_eventual_mxn":   meta_ev,
            "ventas_eventual":     ventas_ev,
            "pct_eventual":        _calc_pct(ventas_ev, meta_ev),
            # Legacy
            "meta_mxn":      meta_legacy or (meta_rec + meta_ev),
            "ventas_actual": ventas_total,
            "porcentaje":    _calc_pct(ventas_total, meta_legacy or (meta_rec + meta_ev)),
            "leads_activos": leads_activos,
            "tiene_meta":    bool(meta and (meta_rec or meta_ev or meta_legacy)),
        })

    return jsonify(resultado)
