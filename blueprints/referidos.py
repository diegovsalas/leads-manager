# blueprints/referidos.py
"""
Recomendaciones desde campo — FEAT-2026-10-16. Ver referidos.py.

/recomendar/   → formulario abierto para cualquiera de la empresa, sin login
/tecnicos/     → link para compartir y registro de quién cobra (dirección)
"""
from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func

from blueprints.auth import is_admin_role
from extensions import db, limiter
from models import EtapaPipeline, Lead, OrigenLead, RolComercial, Usuario
from un_filter import normalizar_marcas
import referidos as R

recomendar_bp = Blueprint("recomendar", __name__)
tecnicos_bp = Blueprint("tecnicos", __name__)

# Límites de columna de leads; se validan aquí para responder con un mensaje
# en vez de un 500 cuando Postgres rechace el INSERT.
_MAXLEN = {
    "quien_nombre": 150, "quien_telefono": 30,
    "cliente_nombre": 200, "sucursal": 200, "contacto_nombre": 200,
    "contacto_telefono": 30, "contacto_email": 200, "observacion": 2000,
}
_ETIQUETA = {
    "quien_nombre": "tu nombre", "quien_telefono": "tu celular",
    "cliente_nombre": "nombre del negocio", "sucursal": "sucursal",
    "contacto_nombre": "nombre del contacto", "contacto_telefono": "teléfono del contacto",
    "contacto_email": "correo del contacto", "observacion": "qué viste",
}


def _form(datos=None, marcas_sel=None, error=None, status=200):
    return render_template("referidos/form.html", unidades=R.UNIDADES_REFERIBLES,
                           datos=datos or {}, marcas_sel=marcas_sel or [],
                           error=error), status


@recomendar_bp.route("/")
def formulario():
    return _form()


@recomendar_bp.route("/", methods=["POST"])
@limiter.limit("20 per hour")
def enviar():
    datos = {k: (request.form.get(k) or "").strip() for k in _MAXLEN}
    datos["es_cliente"] = request.form.get("es_cliente") or ""
    marcas = [m for m in normalizar_marcas(request.form.getlist("marcas"))
              if m in R.UNIDADES_REFERIBLES]

    # Trampa para bots, igual que el formulario web: un humano no ve el campo.
    if request.form.get("sitio_web"):
        return render_template("referidos/gracias.html", es_nuevo=True, quien="",
                               empresa=datos["cliente_nombre"], marcas=marcas or ["—"],
                               vendedor=None)

    def _error(mensaje):
        return _form(datos, marcas, mensaje, 400)

    if len(datos["quien_nombre"].split()) < 2:
        return _error("Escribe tu nombre y apellido.")
    if datos["quien_telefono"] and not R.celular_clave(datos["quien_telefono"]):
        return _error("Tu celular debe tener 10 dígitos.")
    if datos["es_cliente"] not in ("si", "no"):
        return _error("Indica si el negocio ya es cliente de Grupo Avantex.")
    if not datos["cliente_nombre"]:
        return _error("Escribe el nombre del negocio que visitaste.")
    if not marcas:
        return _error("Elige al menos un servicio que necesita.")
    if not datos["observacion"]:
        return _error("Cuéntanos qué viste en la visita.")
    for campo, maximo in _MAXLEN.items():
        if len(datos[campo]) > maximo:
            return _error(f"El campo «{_ETIQUETA[campo]}» es demasiado largo (máx. {maximo}).")
    if datos["contacto_email"] and "@" not in datos["contacto_email"]:
        return _error("El correo del contacto no parece válido.")

    lead, es_nuevo = R.registrar_referido(
        quien_nombre=" ".join(datos["quien_nombre"].split()),
        quien_telefono=datos["quien_telefono"],
        marcas=marcas, observacion=datos["observacion"],
        es_cliente=datos["es_cliente"] == "si", cliente_nombre=datos["cliente_nombre"],
        sucursal=datos["sucursal"], contacto_nombre=datos["contacto_nombre"],
        contacto_telefono=datos["contacto_telefono"],
        contacto_email=datos["contacto_email"])

    # Al confirmar se repite el nombre que escribió, no el de CS: el link es
    # abierto y no debe revelar cómo está registrado un cliente.
    return render_template(
        "referidos/gracias.html", es_nuevo=es_nuevo,
        quien=datos["quien_nombre"].split()[0],
        empresa=" ".join(datos["cliente_nombre"].split()), marcas=marcas,
        vendedor=lead.usuario_asignado.nombre if lead.usuario_asignado else None)


# ── Administración ──────────────────────────────────────────────────

@tecnicos_bp.before_request
def _solo_admin():
    if not is_admin_role():
        return jsonify({"error": "No autorizado"}), 403


@tecnicos_bp.route("/")
def lista():
    ganado = func.count(Lead.id).filter(Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO)
    referidos = Lead.query.filter(Lead.origen == OrigenLead.REFERIDO_OPERACIONES)

    tecnicos = (Usuario.query.filter(Usuario.rol_comercial == RolComercial.TECNICO)
                .order_by(Usuario.nombre).all())
    conteo = {uid: (total, ganados) for uid, total, ganados in
              referidos.filter(Lead.referido_por_id.isnot(None))
              .with_entities(Lead.referido_por_id, func.count(Lead.id), ganado)
              .group_by(Lead.referido_por_id)}

    # Quien recomendó sin estar registrado, agrupado por celular (o por
    # nombre si no lo dio), para registrarlo con un clic.
    sin_registro = {}
    for nombre, tel, total, ganados in (
            referidos.filter(Lead.referido_por_id.is_(None), Lead.referido_nombre.isnot(None))
            .with_entities(Lead.referido_nombre, Lead.referido_telefono,
                           func.count(Lead.id), ganado)
            .group_by(Lead.referido_nombre, Lead.referido_telefono)):
        clave = R.celular_clave(tel) or nombre.lower()
        fila = sin_registro.setdefault(clave, {"nombre": nombre, "telefono": tel or "",
                                               "total": 0, "ganados": 0})
        fila["total"] += total
        fila["ganados"] += ganados

    return render_template(
        "referidos/tecnicos.html", tecnicos=tecnicos, conteo=conteo,
        sin_registro=sorted(sin_registro.values(), key=lambda f: -f["total"]),
        link=request.host_url.rstrip("/") + url_for("recomendar.formulario"),
        error=request.args.get("error"), acreditadas=request.args.get("acreditadas"))


@tecnicos_bp.route("/", methods=["POST"])
def crear():
    nombre = " ".join((request.form.get("nombre") or "").split())[:150]
    telefono = (request.form.get("telefono") or "").strip()[:30]
    if not nombre or not R.celular_clave(telefono):
        return redirect(url_for("tecnicos.lista",
                                error="Nombre y celular de 10 dígitos son obligatorios."))
    if R.tecnico_por_celular(telefono):
        return redirect(url_for("tecnicos.lista", error="Ese celular ya está registrado."))

    # en_turno=False y sin marcas: nunca entra al reparto de leads.
    tecnico = Usuario(nombre=nombre, telefono_whatsapp=telefono,
                      rol_comercial=RolComercial.TECNICO, en_turno=False,
                      especialidad_marca=[])
    db.session.add(tecnico)
    db.session.flush()
    n = R.acreditar_pendientes(tecnico)
    db.session.commit()
    _log(f"{nombre} registrado para recomendaciones ({n} anteriores acreditadas)")
    return redirect(url_for("tecnicos.lista", acreditadas=n))


def _log(detalle):
    try:
        from actividad import log_actividad
        log_actividad("crear", "tecnico", None, detalle)
    except Exception:
        pass
