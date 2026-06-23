"""
Tools que Claude puede invocar para responder preguntas del usuario.

Cada tool tiene su definición JSON Schema (para Claude tool_use) y una
función Python que ejecuta la query con RBAC aplicado.

Convención: las funciones reciben siempre `ctx` con info del usuario
(user_id, usuario_id, rol, nombre), validan permisos y devuelven dict
listo para serializar como tool_result.
"""
from datetime import datetime, timedelta, timezone
from sqlalchemy import func, or_

from extensions import db
from models import (
    Lead, EtapaPipeline, OrigenLead, Usuario, UserCRM,
    Oportunidad, EtapaOportunidad,
)


# ── Helpers RBAC ────────────────────────────────────────────────────


def _es_admin(ctx: dict) -> bool:
    return (ctx.get("rol", "") or "").lower().replace(" ", "_") == "super_admin"


def _mi_usuario_id(ctx: dict):
    """Perfil comercial del usuario actual (usuarios.id), o None si no tiene."""
    return ctx.get("usuario_id")


def _normalize_user_lookup(nombre_o_id: str) -> Usuario:
    """Acepta nombre parcial o UUID y devuelve la Usuario más probable, o None."""
    if not nombre_o_id:
        return None
    s = nombre_o_id.strip()
    if len(s) == 36 and "-" in s:
        return Usuario.query.filter_by(id=s).first()
    return Usuario.query.filter(
        func.lower(Usuario.nombre).like(f"%{s.lower()}%")
    ).first()


# ── Tools ───────────────────────────────────────────────────────────


def tool_mi_resumen_pendientes(ctx: dict) -> dict:
    """Resume qué necesito atender HOY: sin contactar, por vencer, respondieron, estancados."""
    uid = _mi_usuario_id(ctx)
    if not uid:
        return {"error": "No tienes perfil comercial vinculado. Pide a tu admin que lo configure."}

    base = Lead.query.filter(
        Lead.usuario_asignado_id == uid,
        Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
    )

    sin_contactar = base.filter(
        Lead.etapa_pipeline == EtapaPipeline.NUEVO_LEAD
    ).count()

    respondieron = base.filter(Lead.respondio_ultimo_contacto.is_(True)).count()

    now = datetime.now(timezone.utc)
    siete_dias = now - timedelta(days=7)
    catorce_dias = now - timedelta(days=14)
    estancados_7d = base.filter(
        or_(Lead.fecha_ultimo_contacto.is_(None), Lead.fecha_ultimo_contacto < siete_dias)
    ).count()
    estancados_14d = base.filter(
        or_(Lead.fecha_ultimo_contacto.is_(None), Lead.fecha_ultimo_contacto < catorce_dias)
    ).count()

    en_negociacion = base.filter(
        Lead.etapa_pipeline.in_([EtapaPipeline.COTIZACION, EtapaPipeline.DEMO, EtapaPipeline.NEGOCIACION])
    ).count()

    pipe_total = float(base.with_entities(
        func.coalesce(func.sum(func.coalesce(
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0)
    ).scalar() or 0)

    total_activos = base.count()

    return {
        "vendedor":          ctx.get("nombre"),
        "total_leads_activos": total_activos,
        "sin_contactar":     sin_contactar,
        "respondieron":      respondieron,
        "estancados_7d":     estancados_7d,
        "estancados_14d":    estancados_14d,
        "en_negociacion":    en_negociacion,
        "pipe_total_mxn":    pipe_total,
    }


def tool_mis_leads_por_etapa(ctx: dict, etapa: str = None, limit: int = 10) -> dict:
    """Lista de leads del usuario, opcionalmente filtrados por etapa."""
    uid = _mi_usuario_id(ctx)
    if not uid:
        return {"error": "No tienes perfil comercial vinculado."}

    q = Lead.query.filter(Lead.usuario_asignado_id == uid)
    if etapa:
        try:
            q = q.filter(Lead.etapa_pipeline == EtapaPipeline(etapa))
        except ValueError:
            return {"error": f"Etapa '{etapa}' no válida."}

    leads = q.order_by(Lead.fecha_ultimo_contacto.asc().nullsfirst()).limit(limit).all()
    now = datetime.now(timezone.utc)
    out = []
    for l in leads:
        ref = l.fecha_ultimo_contacto or l.fecha_creacion
        if ref and ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        dias = (now - ref).days if ref else 0
        out.append({
            "id":      str(l.id),
            "nombre":  l.nombre,
            "empresa": l.empresa_nombre,
            "etapa":   l.etapa_pipeline.value,
            "marca":   l.marca_interes,
            "valor":   float(l.valor_calculado or 0),
            "origen":  l.origen.value if l.origen else None,
            "estado":  l.estado_cliente,
            "dias_sin_contacto": dias,
            "respondio": l.respondio_ultimo_contacto,
        })
    return {"leads": out, "count": len(out)}


def tool_equipo_resumen(ctx: dict) -> dict:
    """Resumen del equipo: leads por vendedor + alertas. Solo Super Admin."""
    if not _es_admin(ctx):
        return {"error": "Esta info es solo para Super Admin."}

    vendedores = Usuario.query.filter(Usuario.en_turno.is_(True)).order_by(Usuario.nombre).all()
    now = datetime.now(timezone.utc)
    siete_dias = now - timedelta(days=7)

    out = []
    for v in vendedores:
        base = Lead.query.filter(
            Lead.usuario_asignado_id == v.id,
            Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]),
        )
        activos = base.count()
        if activos == 0:
            continue
        sin_contactar = base.filter(Lead.etapa_pipeline == EtapaPipeline.NUEVO_LEAD).count()
        estancados = base.filter(
            or_(Lead.fecha_ultimo_contacto.is_(None), Lead.fecha_ultimo_contacto < siete_dias)
        ).count()
        pipe = float(base.with_entities(
            func.coalesce(func.sum(func.coalesce(
                Lead.cantidad_productos * Lead.precio_unitario,
                Lead.valor_estimado, 0,
            )), 0)
        ).scalar() or 0)
        out.append({
            "vendedor":      v.nombre,
            "vendedor_id":   str(v.id),
            "marcas":        list(v.especialidad_marca or []),
            "activos":       activos,
            "sin_contactar": sin_contactar,
            "estancados_7d": estancados,
            "pipe_mxn":      pipe,
            "alerta_critica": estancados > 5 or sin_contactar > 3,
        })
    out.sort(key=lambda x: -x["pipe_mxn"])
    return {"equipo": out, "total_vendedores": len(out)}


def tool_vendedor_pendientes(ctx: dict, vendedor: str) -> dict:
    """Pendientes de UN vendedor específico. Solo Super Admin.
    `vendedor` puede ser nombre parcial o UUID."""
    if not _es_admin(ctx):
        return {"error": "Esta info es solo para Super Admin."}
    v = _normalize_user_lookup(vendedor)
    if not v:
        return {"error": f"No encontré vendedor que coincida con '{vendedor}'."}

    # Reutilizamos lógica de mi_resumen pero con uid del otro vendedor
    fake_ctx = {**ctx, "usuario_id": str(v.id), "nombre": v.nombre}
    resumen = tool_mi_resumen_pendientes(fake_ctx)
    resumen["vendedor"] = v.nombre
    return resumen


def tool_kpis_periodo(ctx: dict, mes: str = None) -> dict:
    """KPIs del periodo: leads creados, ganados, revenue. Default = mes actual.
    `mes` formato YYYY-MM."""
    uid = _mi_usuario_id(ctx)
    if not uid and not _es_admin(ctx):
        return {"error": "Sin perfil comercial."}

    # Rango
    if mes and len(mes) == 7:
        try:
            y, m = int(mes[:4]), int(mes[5:7])
            inicio = datetime(y, m, 1, tzinfo=timezone.utc)
            fin = (datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12
                   else datetime(y, m + 1, 1, tzinfo=timezone.utc))
        except ValueError:
            inicio = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            fin = inicio + timedelta(days=32)
    else:
        inicio = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        fin = inicio + timedelta(days=32)

    q = Lead.query.filter(Lead.fecha_creacion >= inicio, Lead.fecha_creacion < fin)
    if not _es_admin(ctx):
        q = q.filter(Lead.usuario_asignado_id == uid)

    total = q.count()
    ganados = q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).count()
    perdidos = q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_PERDIDO).count()
    revenue = float(q.filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO).with_entities(
        func.coalesce(func.sum(func.coalesce(
            Lead.cantidad_productos * Lead.precio_unitario,
            Lead.valor_estimado, 0,
        )), 0)
    ).scalar() or 0)

    return {
        "mes":          inicio.strftime("%Y-%m"),
        "leads_creados": total,
        "ganados":      ganados,
        "perdidos":     perdidos,
        "revenue_mxn":  revenue,
        "tasa_cierre":  round(ganados / total * 100, 1) if total > 0 else 0,
        "scope":        "equipo completo" if _es_admin(ctx) else f"mis leads ({ctx.get('nombre')})",
    }


def tool_exportar_leads(ctx: dict, formato: str = "csv", etapa: str = None,
                        incluir_cerrados: bool = False) -> dict:
    """Exporta leads del usuario a CSV o XLS. Devuelve URL de descarga temporal.
    formato: 'csv' o 'xls'.
    """
    from blueprints.chat_ai import _save_export_file

    uid = _mi_usuario_id(ctx)
    if not uid and not _es_admin(ctx):
        return {"error": "Sin perfil comercial."}

    q = Lead.query
    if not _es_admin(ctx):
        q = q.filter(Lead.usuario_asignado_id == uid)
    if etapa:
        try:
            q = q.filter(Lead.etapa_pipeline == EtapaPipeline(etapa))
        except ValueError:
            return {"error": f"Etapa '{etapa}' no válida."}
    if not incluir_cerrados:
        q = q.filter(Lead.etapa_pipeline.notin_([EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO]))

    leads = q.order_by(Lead.fecha_creacion.desc()).limit(5000).all()
    if not leads:
        return {"error": "No hay leads que exportar con esos filtros."}

    headers = [
        "id", "nombre", "telefono", "empresa", "marca",
        "etapa", "valor_mxn", "origen", "estado",
        "fecha_creacion", "fecha_ultimo_contacto", "icp_nivel",
    ]
    rows = []
    for l in leads:
        rows.append([
            str(l.id), l.nombre or "", l.telefono or "", l.empresa_nombre or "",
            l.marca_interes or "", l.etapa_pipeline.value,
            float(l.valor_calculado or 0),
            l.origen.value if l.origen else "",
            l.estado_cliente or "",
            l.fecha_creacion.strftime("%Y-%m-%d") if l.fecha_creacion else "",
            l.fecha_ultimo_contacto.strftime("%Y-%m-%d") if l.fecha_ultimo_contacto else "",
            l.icp_nivel or "",
        ])

    if formato == "xls":
        url, filename = _save_export_file(headers, rows, "xls",
                                          ctx.get("nombre", "user"))
    else:
        url, filename = _save_export_file(headers, rows, "csv",
                                          ctx.get("nombre", "user"))

    return {
        "ok":         True,
        "filename":   filename,
        "download_url": url,
        "count":      len(rows),
        "formato":    formato,
    }


# ── Tool definitions para Anthropic Tool Use ───────────────────────


TOOLS_SCHEMA = [
    {
        "name": "mi_resumen_pendientes",
        "description": "Resume mis pendientes del día: sin contactar, respondieron, estancados, en negociación. Úsalo cuando el usuario pregunte por su día, sus pendientes, qué hacer.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "mis_leads_por_etapa",
        "description": "Devuelve la lista detallada de mis leads, opcionalmente filtrada por etapa del pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "etapa": {
                    "type": "string",
                    "description": "Etapa exacta del pipeline. Opcional.",
                    "enum": ["Nuevo Lead", "1er Contacto", "2do Contacto", "3er Contacto",
                             "4to Contacto", "Presentación", "Cotización", "Demo",
                             "Negociación", "Cerrado Ganado", "Cerrado Perdido"],
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "equipo_resumen",
        "description": "Vista panorámica del equipo de vendedores con sus KPIs y alertas. SOLO Super Admin.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "vendedor_pendientes",
        "description": "Pendientes de un vendedor específico (por nombre). SOLO Super Admin.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vendedor": {"type": "string", "description": "Nombre parcial o completo del vendedor"},
            },
            "required": ["vendedor"],
        },
    },
    {
        "name": "kpis_periodo",
        "description": "KPIs del periodo: leads creados, ganados, revenue, tasa de cierre.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "Formato YYYY-MM. Default = mes actual."},
            },
        },
    },
    {
        "name": "exportar_leads",
        "description": "Genera un archivo CSV o XLS descargable con mis leads (o todos si soy admin).",
        "input_schema": {
            "type": "object",
            "properties": {
                "formato": {"type": "string", "enum": ["csv", "xls"], "default": "csv"},
                "etapa": {"type": "string", "description": "Filtrar por etapa específica. Opcional."},
                "incluir_cerrados": {"type": "boolean", "default": False,
                                      "description": "Incluir leads cerrados (ganados o perdidos)."},
            },
        },
    },
]


TOOL_FUNCTIONS = {
    "mi_resumen_pendientes": tool_mi_resumen_pendientes,
    "mis_leads_por_etapa":   tool_mis_leads_por_etapa,
    "equipo_resumen":        tool_equipo_resumen,
    "vendedor_pendientes":   tool_vendedor_pendientes,
    "kpis_periodo":          tool_kpis_periodo,
    "exportar_leads":        tool_exportar_leads,
}


def run_tool(name: str, args: dict, ctx: dict) -> dict:
    """Despacha una tool por nombre. Retorna dict serializable."""
    fn = TOOL_FUNCTIONS.get(name)
    if not fn:
        return {"error": f"Tool '{name}' no existe."}
    try:
        return fn(ctx, **(args or {}))
    except TypeError as e:
        return {"error": f"Argumentos inválidos para {name}: {e}"}
    except Exception as e:
        return {"error": f"Error ejecutando {name}: {type(e).__name__}: {e}"}
