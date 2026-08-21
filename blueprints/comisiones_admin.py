# blueprints/comisiones_admin.py
"""
Configuración del tabulador de comisiones multi-UN.  FEAT-2026-08-21

Matriz editable de tasas por unidad de negocio (con tramos, para los
escalonados como Pestex), pesos de las 4 etapas y factores de castigo por
descuento. Todo lo que el motor de comisiones.py consume se captura aquí:
cambiar una política no requiere tocar código ni desplegar.

Solo roles administrativos. Un cambio de pesos afecta el cálculo de toda
venta que se cierre después, así que no es una pantalla para vendedores.
"""
from decimal import Decimal, InvalidOperation

from flask import Blueprint, render_template, request, redirect, jsonify, session, flash

from extensions import db
from models import ComisionTasa, ComisionEtapa, ComisionDescuento
from blueprints.auth import is_admin_role
from actividad import log_actividad

comisiones_bp = Blueprint("comisiones_admin", __name__, template_folder="../templates")


def _solo_admin():
    if not is_admin_role():
        return jsonify({"error": "Solo roles administrativos"}), 403
    return None


def _dec(valor, default=None):
    """Texto -> Decimal. Acepta '25.4', '25,4' y vacío."""
    txt = (valor or "").strip().replace(",", ".").replace("%", "")
    if not txt:
        return default
    try:
        return Decimal(txt)
    except (InvalidOperation, ValueError):
        return default


def _ctx():
    return {
        "user_nombre": session.get("user_nombre", ""),
        "user_rol": session.get("user_rol", ""),
        "is_kam": False,
    }


@comisiones_bp.route("/")
def index():
    """La matriz completa: tasas, pesos y descuentos."""
    err = _solo_admin()
    if err:
        return err

    tasas = (ComisionTasa.query
             .order_by(ComisionTasa.unidad, ComisionTasa.monto_desde).all())
    etapas = ComisionEtapa.query.order_by(ComisionEtapa.orden).all()
    descuentos = (ComisionDescuento.query
                  .order_by(ComisionDescuento.descuento_desde).all())

    suma_pesos = sum(Decimal(str(e.peso)) for e in etapas) if etapas else Decimal("0")

    # Una UN con más de un tramo es escalonada; con uno solo, tasa fija.
    por_unidad = {}
    for t in tasas:
        por_unidad.setdefault(t.unidad, []).append(t)

    # Un traslape no rompe el cálculo (gana el tramo más específico) pero casi
    # siempre significa que quedó un catch-all viejo ensombreciendo escalones
    # nuevos. Se avisa en pantalla.
    from comisiones import tramos_traslapados
    traslapes = {un: tramos_traslapados(un) for un in por_unidad}
    traslapes = {k: v for k, v in traslapes.items() if v}

    return render_template(
        "comisiones.html",
        por_unidad=por_unidad, etapas=etapas, descuentos=descuentos,
        traslapes=traslapes,
        suma_pesos=suma_pesos,
        pesos_cuadran=(suma_pesos == Decimal("1")),
        **_ctx(),
    )


# ── Tasas por unidad de negocio ──────────────────────────────────

@comisiones_bp.route("/tasa/<int:tasa_id>", methods=["POST"])
def guardar_tasa(tasa_id):
    err = _solo_admin()
    if err:
        return err
    t = db.session.get(ComisionTasa, tasa_id)
    if not t:
        return jsonify({"error": "Tramo no encontrado"}), 404

    d = request.form
    antes = f"{t.unidad} {float(t.tasa)*100:.2f}%"
    tasa = _dec(d.get("tasa"))
    if tasa is None or tasa < 0 or tasa > 100:
        flash("La tasa debe ser un número entre 0 y 100.", "error")
        return redirect("/comisiones/")

    t.tasa = tasa / 100                      # se captura en %, se guarda en fracción
    t.monto_desde = _dec(d.get("monto_desde"), Decimal("0"))
    t.monto_hasta = _dec(d.get("monto_hasta"))     # vacío = sin tope
    t.nota = (d.get("nota") or "").strip()
    t.activo = d.get("activo") == "on"
    db.session.commit()
    log_actividad("editar", "comision_tasa", None,
                  f"Tasa {antes} → {t.unidad} {float(t.tasa)*100:.2f}%")
    return redirect("/comisiones/")


@comisiones_bp.route("/tasa/nueva", methods=["POST"])
def nueva_tasa():
    """Agrega un tramo. Así se capturan los escalones de Pestex."""
    err = _solo_admin()
    if err:
        return err
    d = request.form
    unidad = (d.get("unidad") or "").strip()
    tasa = _dec(d.get("tasa"))
    if not unidad or tasa is None:
        flash("Falta la unidad de negocio o la tasa.", "error")
        return redirect("/comisiones/")

    db.session.add(ComisionTasa(
        unidad=unidad,
        monto_desde=_dec(d.get("monto_desde"), Decimal("0")),
        monto_hasta=_dec(d.get("monto_hasta")),
        tasa=tasa / 100,
        modalidad=(d.get("modalidad") or "Pago único, mes 1").strip(),
        regla_origen=(d.get("regla_origen") or "").strip(),
        nota=(d.get("nota") or "").strip(),
    ))
    db.session.commit()
    log_actividad("crear", "comision_tasa", None, f"Tramo nuevo · {unidad} {tasa}%")
    return redirect("/comisiones/")


@comisiones_bp.route("/tasa/<int:tasa_id>/eliminar", methods=["POST"])
def eliminar_tasa(tasa_id):
    err = _solo_admin()
    if err:
        return err
    t = db.session.get(ComisionTasa, tasa_id)
    if t:
        # No dejar una UN sin ningún tramo: quedaría sin poder comisionar.
        hermanos = ComisionTasa.query.filter_by(unidad=t.unidad).count()
        if hermanos <= 1:
            flash(f"«{t.unidad}» quedaría sin ninguna tasa. "
                  f"Desactívala en vez de borrarla.", "error")
            return redirect("/comisiones/")
        log_actividad("eliminar", "comision_tasa", None,
                      f"Tramo de {t.unidad} desde {t.monto_desde}")
        db.session.delete(t)
        db.session.commit()
    return redirect("/comisiones/")


# ── Pesos de las etapas ──────────────────────────────────────────

@comisiones_bp.route("/pesos", methods=["POST"])
def guardar_pesos():
    """Los 4 pesos de una vez, porque deben sumar 100% entre ellos."""
    err = _solo_admin()
    if err:
        return err

    etapas = ComisionEtapa.query.order_by(ComisionEtapa.orden).all()
    nuevos = {}
    for e in etapas:
        v = _dec(request.form.get(f"peso_{e.clave}"))
        if v is None or v < 0:
            flash(f"El peso de «{e.nombre}» no es un número válido.", "error")
            return redirect("/comisiones/")
        nuevos[e.clave] = v

    total = sum(nuevos.values())
    if total != Decimal("100"):
        flash(f"Los pesos suman {total}%. Deben sumar exactamente 100% "
              f"para que la comisión no se duplique ni se pierda.", "error")
        return redirect("/comisiones/")

    antes = ", ".join(f"{e.nombre} {float(e.peso)*100:.0f}%" for e in etapas)
    for e in etapas:
        e.peso = nuevos[e.clave] / 100
    db.session.commit()
    log_actividad("editar", "comision_etapa", None, f"Pesos: {antes} → {total}% redistribuido")
    flash("Pesos actualizados.", "ok")
    return redirect("/comisiones/")


# ── Castigo por descuento ────────────────────────────────────────

@comisiones_bp.route("/descuento/<int:desc_id>", methods=["POST"])
def guardar_descuento(desc_id):
    err = _solo_admin()
    if err:
        return err
    tr = db.session.get(ComisionDescuento, desc_id)
    if not tr:
        return jsonify({"error": "Tramo no encontrado"}), 404

    desde = _dec(request.form.get("descuento_desde"))
    factor = _dec(request.form.get("factor"))
    if desde is None or factor is None:
        flash("Piso y factor deben ser números.", "error")
        return redirect("/comisiones/")

    tr.descuento_desde = desde / 100
    tr.factor = factor / 100
    tr.etiqueta = (request.form.get("etiqueta") or "").strip()
    tr.requiere_autorizacion = request.form.get("requiere_autorizacion") == "on"
    db.session.commit()
    log_actividad("editar", "comision_descuento", None,
                  f"Tramo desde {desde}% → factor {factor}%")
    return redirect("/comisiones/")


# ── Calculadora de prueba ────────────────────────────────────────

@comisiones_bp.route("/simular", methods=["GET"])
def simular():
    """Prueba la configuración sin cerrar una venta real. Equivale a la
    hoja 'Calculadora' del tabulador."""
    err = _solo_admin()
    if err:
        return err
    import comisiones as C

    unidad = (request.args.get("unidad") or "").strip()
    lista = _dec(request.args.get("lista"), Decimal("0"))
    cerrada = _dec(request.args.get("cerrada"), lista)
    if not unidad or lista <= 0:
        return jsonify({"error": "Indica unidad y mensualidad de lista."}), 400

    # Se simula con las 4 etapas repartidas entre dos personas ficticias
    # para ver el efecto de los pesos, no de la atribución.
    r = C.calcular(unidad, lista, cerrada, {})
    if r.get("error"):
        return jsonify(r), 400

    return jsonify({
        "unidad": unidad,
        "mensualidad_lista": float(r["mensualidad_lista"]),
        "mensualidad_cerrada": float(r["mensualidad_cerrada"]),
        "descuento": float(r["descuento"]),
        "tasa_base": float(r["tasa_base"]),
        "factor_descuento": float(r["factor_descuento"]),
        "etiqueta_descuento": r["etiqueta_descuento"],
        "requiere_autorizacion": r["requiere_autorizacion"],
        "tasa_aplicada": float(r["tasa_aplicada"]),
        "comision_total": float(r["comision_total"]),
        "comision_sin_descuento": float(r["comision_sin_descuento"]),
        "etapas": [{"nombre": d["nombre"], "peso": float(d["peso"]),
                    "monto": float(d["monto"])} for d in r["detalle_etapas"]],
        "nota_un": r["nota_un"],
    })
