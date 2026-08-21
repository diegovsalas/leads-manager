# comisiones.py
"""
Tabulador de comisiones multi-UN repartido por etapa.  FEAT-2026-08-21

Principio del tabulador: cada UN sigue pagando exactamente lo mismo que paga
hoy. Lo único nuevo es que esa bolsa se reparte entre 4 etapas y cada quien
cobra las que hizo. La suma de las participaciones SIEMPRE es la comisión
total; nunca la incrementa.

    comisión_total = mensualidad_cerrada × tasa_UN × factor_descuento
    participación  = comisión_total × Σ(pesos de las etapas que hizo)

Dos correcciones frente al Sheet original, ambas documentadas donde ocurren:

  1. El tramo de descuento del 85% aparece en el Sheet con piso "0.0%", lo que
     haría que una venta a precio de lista cobrara 85% en vez de 100%. Aquí el
     precio de lista se resuelve antes de consultar la tabla.

  2. El ejemplo al pie de "Tabla rápida" reparte 35/65 entre las dos primeras y
     las dos últimas etapas, pero los pesos definidos dan 30/70. Este módulo
     usa los pesos (30/70), que es lo que también hace la Calculadora del Sheet.
"""
from decimal import Decimal, ROUND_HALF_UP

from extensions import db
from models import (
    ComisionTasa, ComisionEtapa, ComisionDescuento,
    LeadAtribucion, SaleParticipacion, Usuario,
)

CENTAVO = Decimal("0.01")


def _d(v):
    """A Decimal, tolerando None, float, str y Decimal."""
    if v is None:
        return Decimal("0")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _redondear(v):
    return _d(v).quantize(CENTAVO, rounding=ROUND_HALF_UP)


# ── Configuración ────────────────────────────────────────────────

def etapas_config():
    """Las 4 etapas ordenadas. Devuelve [(clave, nombre, peso Decimal), ...]"""
    filas = ComisionEtapa.query.order_by(ComisionEtapa.orden).all()
    return [(e.clave, e.nombre, _d(e.peso)) for e in filas]


def peso_de(clave):
    e = ComisionEtapa.query.filter_by(clave=clave).first()
    return _d(e.peso) if e else Decimal("0")


def tasa_un(unidad, mensualidad):
    """Tasa aplicable a esa UN para ese nivel de mensualidad.

    Se modela con tramos porque Pestex es escalonado. Una UN de tasa fija es
    simplemente un tramo con monto_hasta = NULL.

    Gana el tramo MÁS ESPECÍFICO, no el primero por monto_desde. Sin esto, un
    renglón catch-all (0 → sin tope) ensombrece cualquier escalón que se
    capture después: la tasa nueva nunca aplicaría y nadie se enteraría.
    Un tramo acotado le gana al abierto, y entre acotados gana el más estrecho.

    Devuelve (tasa, registro) o (None, None) si la UN no está configurada.
    """
    m = _d(mensualidad)
    candidatos = [
        t for t in ComisionTasa.query.filter(
            ComisionTasa.unidad == unidad, ComisionTasa.activo.is_(True)).all()
        if m >= _d(t.monto_desde) and (t.monto_hasta is None or m <= _d(t.monto_hasta))
    ]
    if not candidatos:
        return None, None

    def amplitud(t):
        # None = sin tope: infinitamente amplio, siempre el último recurso
        return (1, 0) if t.monto_hasta is None else (0, _d(t.monto_hasta) - _d(t.monto_desde))

    mejor = min(candidatos, key=amplitud)
    return _d(mejor.tasa), mejor


def tramos_traslapados(unidad):
    """Pares de tramos que se pisan entre sí. La UI lo muestra como aviso:
    un traslape no rompe el cálculo (gana el más específico) pero casi
    siempre significa que la configuración quedó a medias."""
    filas = (ComisionTasa.query
             .filter(ComisionTasa.unidad == unidad, ComisionTasa.activo.is_(True))
             .order_by(ComisionTasa.monto_desde).all())
    choques = []
    for i, a in enumerate(filas):
        for b in filas[i + 1:]:
            a_hasta = _d(a.monto_hasta) if a.monto_hasta is not None else None
            b_hasta = _d(b.monto_hasta) if b.monto_hasta is not None else None
            solapan = (a_hasta is None or _d(b.monto_desde) <= a_hasta) and \
                      (b_hasta is None or _d(a.monto_desde) <= b_hasta)
            if solapan:
                choques.append((a, b))
    return choques


def factor_descuento(pct_descuento):
    """Factor de castigo sobre la tasa. pct_descuento en fracción (0.07 = 7%).

    El precio de lista se resuelve AQUÍ y no en la tabla: en el Sheet el tramo
    del 85% tiene piso 0.0%, así que una búsqueda por piso le aplicaría 85% a
    una venta sin descuento. Sin descuento => factor 1.0, sin excepción.
    """
    d = _d(pct_descuento)
    if d <= 0:
        return Decimal("1"), "Sin descuento — precio de lista", False

    mejor = None
    for tr in ComisionDescuento.query.order_by(ComisionDescuento.descuento_desde).all():
        if d > _d(tr.descuento_desde):
            mejor = tr
    if mejor is None:
        return Decimal("1"), "Sin descuento — precio de lista", False
    return _d(mejor.factor), mejor.etiqueta, bool(mejor.requiere_autorizacion)


# ── Cálculo ──────────────────────────────────────────────────────

def calcular(unidad, mensualidad_lista, mensualidad_cerrada, atribucion):
    """Calcula la comisión total y su reparto.

    atribucion: {clave_etapa: usuario_id or None}
    Devuelve un dict con el desglose completo, listo para mostrar o guardar.
    Una etapa sin responsable no se paga: su peso queda en `sin_asignar`, y el
    total repartido es menor que el total. Es deliberado — así se ve el hueco
    en vez de repartirlo entre los demás.
    """
    lista = _d(mensualidad_lista)
    cerrada = _d(mensualidad_cerrada)

    desc = (lista - cerrada) / lista if lista > 0 else Decimal("0")
    if desc < 0:
        desc = Decimal("0")          # se cerró por encima de lista: sin castigo

    tasa, reg = tasa_un(unidad, cerrada)
    if tasa is None:
        return {"error": f"La unidad «{unidad}» no tiene tasa configurada.",
                "unidad": unidad}

    factor, etiqueta, requiere_aut = factor_descuento(desc)
    tasa_aplicada = tasa * factor
    total = _redondear(cerrada * tasa_aplicada)
    total_sin_desc = _redondear(cerrada * tasa)

    # Reparto por etapa
    detalle, por_persona, sin_asignar = [], {}, Decimal("0")
    for clave, nombre, peso in etapas_config():
        uid = atribucion.get(clave)
        monto = _redondear(total * peso)
        detalle.append({"clave": clave, "nombre": nombre, "peso": peso,
                        "usuario_id": uid, "monto": monto})
        if uid:
            acc = por_persona.setdefault(uid, {"pct": Decimal("0"),
                                               "monto": Decimal("0"), "etapas": []})
            acc["pct"] += peso
            acc["monto"] += monto
            acc["etapas"].append(nombre)
        else:
            sin_asignar += peso

    # El redondeo por etapa puede dejar centavos sueltos; se ajusta en la
    # participación mayor para que la suma cuadre exacto con el total.
    repartido = sum(p["monto"] for p in por_persona.values())
    if por_persona and sin_asignar == 0 and repartido != total:
        mayor = max(por_persona.values(), key=lambda p: p["monto"])
        mayor["monto"] += total - repartido
        repartido = total

    nombres = {str(u.id): u.nombre for u in Usuario.query.all()} if por_persona else {}
    participaciones = [
        {"usuario_id": uid, "nombre": nombres.get(str(uid), "—"),
         "etapas": ", ".join(p["etapas"]), "pct": p["pct"], "monto": p["monto"]}
        for uid, p in sorted(por_persona.items(), key=lambda kv: -kv[1]["monto"])
    ]

    return {
        "unidad": unidad,
        "mensualidad_lista": lista,
        "mensualidad_cerrada": cerrada,
        "descuento": desc,
        "tasa_base": tasa,
        "factor_descuento": factor,
        "etiqueta_descuento": etiqueta,
        "requiere_autorizacion": requiere_aut,
        "tasa_aplicada": tasa_aplicada,
        "comision_total": total,
        "comision_sin_descuento": total_sin_desc,
        "detalle_etapas": detalle,
        "participaciones": participaciones,
        "peso_sin_asignar": sin_asignar,
        "monto_sin_asignar": _redondear(total * sin_asignar),
        "repartido": _redondear(repartido),
        "cuadra": sin_asignar == 0 and _redondear(repartido) == total,
        "nota_un": (reg.nota or "") if reg else "",
    }


# ── ¿Esta venta va por el tabulador? ─────────────────────────────

# "Aromatex Home" es una línea de producto de Aromatex, no otra unidad de
# negocio. Sin esta equivalencia, 7 de los 9 vendedores con más de una marca
# contarían como multi-UN solo por tener ambas, y sus ventas normales
# entrarían al tabulador sin deberlo.
# Si el criterio cambia, es la única línea que hay que tocar.
UN_EQUIVALENTES = {"Aromatex Home": "Aromatex"}


def unidades_de(usuario):
    """Unidades de negocio distintas de un vendedor, ya normalizadas."""
    marcas = getattr(usuario, "especialidad_marca", None) or []
    return {UN_EQUIVALENTES.get(m, m) for m in marcas if m}


def es_multi_un(usuario):
    """True si el vendedor atiende más de una unidad de negocio."""
    return len(unidades_de(usuario)) > 1


def aplica_tabulador(lead):
    """Decide si esta venta se reparte por etapas o va por el esquema normal.

    Se cumplen DOS condiciones, no una:

      1. El lead pertenece a un vendedor multi-UN. El tabulador nació para
         resolver las ventas cruzadas entre unidades; un vendedor de una sola
         UN no está en ese supuesto.
      2. Participó más de una persona en las cuatro etapas. Si alguien hizo
         todo solo, no hay nada que repartir y cobra con el esquema de
         siempre, aunque sea multi-UN.

    Devuelve (aplica: bool, motivo: str) — el motivo se muestra en pantalla
    para que nadie tenga que adivinar por qué una venta comisionó de un modo
    o de otro.
    """
    dueno = getattr(lead, "usuario_asignado", None)
    if dueno is None and getattr(lead, "usuario_asignado_id", None):
        from models import Usuario
        dueno = db.session.get(Usuario, lead.usuario_asignado_id)

    if dueno is None:
        return False, "El lead no tiene vendedor asignado."

    if not es_multi_un(dueno):
        uns = ", ".join(sorted(unidades_de(dueno))) or "ninguna"
        return False, (f"{dueno.nombre} atiende una sola unidad ({uns}). "
                       f"El tabulador es para ventas cruzadas entre unidades.")

    participantes = {uid for uid in atribucion_de(lead.id).values() if uid}
    if len(participantes) < 2:
        return False, ("Participó una sola persona en las cuatro etapas. "
                       "No hay nada que repartir.")

    return True, (f"{dueno.nombre} es multi-UN y participaron "
                  f"{len(participantes)} personas.")


# ── Atribución ───────────────────────────────────────────────────

# Al mover un lead en el pipeline se propone al responsable de las etapas del
# tabulador que ese movimiento implica. El gerente puede corregirlo después.
PIPELINE_A_ETAPA = {
    "1er Contacto":  ["prospectar"],
    "2do Contacto":  ["prospectar"],
    "3er Contacto":  ["prospectar"],
    "4to Contacto":  ["prospectar"],
    "Presentación":  ["cita"],
    "Demo":          ["cita"],
    "Cotización":    ["cotizacion"],
    "Negociación":   ["cotizacion"],
    "Cerrado Ganado": ["cierre"],
}


def registrar_avance(lead, etapa_pipeline, usuario_id):
    """Propone atribución automática al mover el lead. No pisa lo que un
    humano ya corrigió a mano."""
    claves = PIPELINE_A_ETAPA.get(etapa_pipeline, [])
    if not claves or not usuario_id:
        return []
    creadas = []
    for clave in claves:
        existente = LeadAtribucion.query.filter_by(lead_id=lead.id, etapa=clave).first()
        if existente:
            continue                      # ya hay responsable, automático o manual
        db.session.add(LeadAtribucion(
            lead_id=lead.id, etapa=clave,
            usuario_id=usuario_id, es_automatica=True))
        creadas.append(clave)
    return creadas


def atribucion_de(lead_id):
    """{clave_etapa: usuario_id} de un lead."""
    return {a.etapa: a.usuario_id
            for a in LeadAtribucion.query.filter_by(lead_id=lead_id).all()}


def fijar_atribucion(lead_id, clave, usuario_id, definida_por=""):
    """Corrección manual. Marca la fila como no-automática para que un
    movimiento posterior del pipeline no la sobrescriba."""
    a = LeadAtribucion.query.filter_by(lead_id=lead_id, etapa=clave).first()
    if not a:
        a = LeadAtribucion(lead_id=lead_id, etapa=clave)
        db.session.add(a)
    a.usuario_id = usuario_id
    a.es_automatica = False
    a.definida_por = definida_por
    return a


def congelar_en_venta(sale, resultado):
    """Guarda el reparto al cerrar. Se congela: si mañana cambian los pesos,
    lo ya pagado no se mueve."""
    SaleParticipacion.query.filter_by(sale_id=sale.id).delete()
    for p in resultado.get("participaciones", []):
        db.session.add(SaleParticipacion(
            sale_id=sale.id, usuario_id=p["usuario_id"],
            etapas=p["etapas"], porcentaje=p["pct"], monto=p["monto"]))
    sale.commission_amount = resultado["comision_total"]
    return sale
