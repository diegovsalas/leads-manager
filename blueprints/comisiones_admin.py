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
from models import (ComisionTasa, ComisionEtapa, ComisionDescuento,
                    ComisionPolitica, ComisionCorte,
                    Sale, SaleParticipacion, Usuario, MetaVendedor)
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


# ══════════════════════════════════════════════════════════════════
# CORTE MENSUAL — FEAT-2026-08-26
#
# "La meta habilita el pago": las comisiones se calculan y congelan al
# cerrar cada venta, como siempre. Lo que decide el corte es si al terminar
# el mes se autorizan o se retienen.
#
# El sistema NO define qué pasa con lo retenido. Que se pierda o se arrastre
# al mes siguiente es una decisión con implicaciones laborales, y se toma
# aquí, mes con mes: liberar o cancelar. Queda registrado quién lo decidió.
# ══════════════════════════════════════════════════════════════════

def _rango_mes(mes):
    """'2026-08' -> (inicio, fin) en datetime UTC, fin exclusivo."""
    from datetime import datetime, timezone as _tz
    y, m = (int(x) for x in mes.split("-"))
    ini = datetime(y, m, 1, tzinfo=_tz.utc)
    fin = datetime(y + 1, 1, 1, tzinfo=_tz.utc) if m == 12 \
        else datetime(y, m + 1, 1, tzinfo=_tz.utc)
    return ini, fin


def _comisiones_del_mes(mes):
    """Comisión que le corresponde a cada vendedor por las ventas CERRADAS
    en ese mes. Devuelve {usuario_id: {"comision": float, "vendido": float}}.

    Se mide por fecha de cierre de la venta, no por creación del lead: un
    lead de julio cerrado en agosto se paga en agosto.

    En una venta compartida cada quien cobra su tajada (sale_participaciones).
    Solo cuando la venta no tiene reparto se le abona completa a su dueño.
    """
    ini, fin = _rango_mes(mes)
    ventas = Sale.query.filter(
        Sale.closed_at >= ini, Sale.closed_at < fin,
        Sale.status == "activa",
    ).all()
    if not ventas:
        return {}

    repartos = {}
    for p in SaleParticipacion.query.filter(
            SaleParticipacion.sale_id.in_([v.id for v in ventas])).all():
        repartos.setdefault(str(p.sale_id), []).append(p)

    out = {}
    for v in ventas:
        partes = repartos.get(str(v.id))
        if partes:
            for p in partes:
                if not p.usuario_id:
                    continue
                acc = out.setdefault(str(p.usuario_id), {"comision": 0.0, "vendido": 0.0})
                acc["comision"] += float(p.monto or 0)
        elif v.user_id:
            acc = out.setdefault(str(v.user_id), {"comision": 0.0, "vendido": 0.0})
            acc["comision"] += float(v.commission_amount or 0)
        # El monto vendido siempre se le acredita al dueño de la venta.
        if v.user_id:
            acc = out.setdefault(str(v.user_id), {"comision": 0.0, "vendido": 0.0})
            acc["vendido"] += float(v.monthly_amount or v.total_amount or 0)
    return out


def _meta_del_mes(usuario_id, mes, base):
    m = MetaVendedor.query.filter_by(usuario_id=usuario_id, mes=mes).first()
    if not m:
        return 0.0
    rec = float(m.meta_recurrente_mxn or 0)
    ev = float(m.meta_eventual_mxn or 0)
    if base == "recurrente":
        return rec
    if base == "eventual":
        return ev
    return rec + ev or float(m.meta_mxn or 0)


@comisiones_bp.route("/corte")
def corte():
    """Pantalla del corte: qué vendió cada quien, contra su meta, y en qué
    quedaron sus comisiones."""
    err = _solo_admin()
    if err:
        return err

    from datetime import datetime, timezone as _tz
    mes = (request.args.get("mes") or "").strip() or \
        datetime.now(_tz.utc).strftime("%Y-%m")

    pol = ComisionPolitica.vigente()
    db.session.commit()

    datos = _comisiones_del_mes(mes)
    guardados = {str(c.usuario_id): c
                 for c in ComisionCorte.query.filter_by(mes=mes).all()}

    filas = []
    for u in Usuario.query.order_by(Usuario.nombre).all():
        uid = str(u.id)
        d = datos.get(uid)
        meta = _meta_del_mes(u.id, mes, pol.base)
        if not d and not meta:
            continue                      # ni vendió ni tenía meta: no sale
        vendido = d["vendido"] if d else 0.0
        comision = d["comision"] if d else 0.0
        cumpl = (vendido / meta) if meta > 0 else 0.0
        guardado = guardados.get(uid)
        filas.append({
            "usuario_id": uid, "vendedor": u.nombre,
            "meta": meta, "vendido": vendido,
            "cumplimiento": cumpl, "comision": comision,
            "alcanzo": (not pol.meta_habilita_pago) or (meta > 0 and cumpl >= float(pol.umbral)),
            "guardado": guardado.to_dict() if guardado else None,
        })

    return render_template("comisiones_corte.html", mes=mes, politica=pol,
                           filas=filas, **_ctx())


@comisiones_bp.route("/politica", methods=["POST"])
def guardar_politica():
    err = _solo_admin()
    if err:
        return err
    pol = ComisionPolitica.vigente()
    pol.meta_habilita_pago = request.form.get("meta_habilita_pago") == "on"
    umbral = _dec(request.form.get("umbral"), None)
    if umbral is not None:
        # se captura en porcentaje (100) y se guarda en fracción (1.0)
        pol.umbral = umbral / 100 if umbral > 1 else umbral
    base = (request.form.get("base") or "total").strip()
    if base in ("total", "recurrente", "eventual"):
        pol.base = base
    db.session.commit()
    log_actividad("editar", "comision", None,
                  f"Política de pago: meta {'habilita' if pol.meta_habilita_pago else 'NO condiciona'} "
                  f"· umbral {float(pol.umbral)*100:.0f}% · base {pol.base}")
    flash("Política de pago actualizada", "ok")
    return redirect(request.referrer or "/comisiones/corte")


@comisiones_bp.route("/corte/aplicar", methods=["POST"])
def aplicar_corte():
    """Congela el corte del mes: guarda para cada vendedor qué vendió, qué
    meta tenía y si su comisión queda autorizada o retenida."""
    err = _solo_admin()
    if err:
        return err

    mes = (request.form.get("mes") or "").strip()
    if not mes:
        return jsonify({"error": "Falta el mes"}), 400

    pol = ComisionPolitica.vigente()
    datos = _comisiones_del_mes(mes)
    quien = session.get("user_nombre", "")
    n_aut = n_ret = 0

    for u in Usuario.query.all():
        uid = str(u.id)
        d = datos.get(uid)
        meta = _meta_del_mes(u.id, mes, pol.base)
        if not d and not meta:
            continue
        vendido = d["vendido"] if d else 0.0
        comision = d["comision"] if d else 0.0
        cumpl = (vendido / meta) if meta > 0 else 0.0

        if not pol.meta_habilita_pago:
            estado, motivo = "autorizada", "La meta no condiciona el pago."
        elif meta <= 0:
            estado, motivo = "retenida", "Sin meta capturada para el mes: no se puede evaluar."
        elif cumpl >= float(pol.umbral):
            estado = "autorizada"
            motivo = f"Alcanzó {cumpl*100:.0f}% de su meta."
        else:
            estado = "retenida"
            motivo = f"Alcanzó {cumpl*100:.0f}%, por debajo del {float(pol.umbral)*100:.0f}% requerido."

        c = ComisionCorte.query.filter_by(usuario_id=u.id, mes=mes).first()
        if not c:
            c = ComisionCorte(usuario_id=u.id, mes=mes)
            db.session.add(c)
        # Una decisión ya tomada a mano no se pisa al recalcular.
        if c.decidido_por and c.decidido_por != "sistema":
            c.meta_mxn, c.vendido_mxn = meta, vendido
            c.cumplimiento, c.comision_mxn = cumpl, comision
            continue
        c.meta_mxn, c.vendido_mxn = meta, vendido
        c.cumplimiento, c.comision_mxn = cumpl, comision
        c.estado, c.motivo = estado, motivo
        c.decidido_por = "sistema"
        n_aut += estado == "autorizada"
        n_ret += estado == "retenida"

    db.session.commit()
    log_actividad("editar", "comision", None,
                  f"Corte de {mes}: {n_aut} autorizadas, {n_ret} retenidas · por {quien}")
    flash(f"Corte de {mes} aplicado: {n_aut} autorizadas, {n_ret} retenidas", "ok")
    return redirect(f"/comisiones/corte?mes={mes}")


@comisiones_bp.route("/corte/<uuid:corte_id>/estado", methods=["POST"])
def cambiar_estado_corte(corte_id):
    """Liberar o cancelar una comisión retenida. Es la decisión que el
    sistema no toma solo."""
    err = _solo_admin()
    if err:
        return err

    c = db.session.get(ComisionCorte, corte_id)
    if not c:
        return jsonify({"error": "Corte no encontrado"}), 404

    nuevo = (request.form.get("estado") or "").strip()
    if nuevo not in ("autorizada", "retenida", "cancelada"):
        return jsonify({"error": "Estado inválido"}), 400

    anterior = c.estado
    c.estado = nuevo
    c.motivo = (request.form.get("motivo") or "").strip() or c.motivo
    c.decidido_por = session.get("user_nombre", "") or "dirección"
    db.session.commit()
    log_actividad("editar", "comision", None,
                  f"Comisión de {c.usuario.nombre if c.usuario else '?'} en {c.mes}: "
                  f"{anterior} → {nuevo} · por {c.decidido_por}")
    flash(f"Comisión {nuevo}", "ok")
    return redirect(f"/comisiones/corte?mes={c.mes}")
