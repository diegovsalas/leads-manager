"""
Clasificador de UEN de Savio.

Port a Python del SAVIO_UEN_MAP que vivía en vendedores.cloud (Node.js,
en deprecación). Recibe el UEN crudo que devuelve la API de Savio y, si
hace falta, la description y el amount de la suscripción/factura, y
devuelve la clasificación normalizada: unidad de negocio, tipo de
movimiento (recurrente/eventual/poliza/refacturacion) y si suma al MRR
del grupo.

Puro: no toca DB, no importa modelos. Solo strings y números → dicts.
"""

# Mapa base. La key está SIEMPRE en uppercase y sin espacios al borde.
# El lookup hace strip+upper antes de buscar.
SAVIO_UEN_MAP = {
    "AROMATEX RECURRENTE":       {"unit": "aromatex",         "type": "recurrente",     "sum_mrr": True},
    "AROMATEX RECURRENTE NUEVO": {"unit": "aromatex",         "type": "recurrente",     "sum_mrr": True},
    "PLAN RESCATE":              {"unit": "aromatex",         "type": "recurrente",     "sum_mrr": True},
    "PESTEX RECURRENTE":         {"unit": "pestex",           "type": "recurrente",     "sum_mrr": True},
    "PESTEX RECURRENTE NUEVO":   {"unit": "pestex",           "type": "recurrente",     "sum_mrr": True},
    "QTB":                       {"unit": "weldex",           "type": "recurrente",     "sum_mrr": True},
    "AROMATEX EVENTUAL":         {"unit": "aromatex",         "type": "eventual",       "sum_mrr": False},
    "AROMATEX POLIZAS":          {"unit": "aromatex",         "type": "poliza",         "sum_mrr": False},
    "ECOMMERCE":                 {"unit": "aromatex",         "type": "eventual",       "sum_mrr": False},
    "PESTEX POLIZAS":            {"unit": "pestex",           "type": "poliza",         "sum_mrr": False},
    "WELDEX":                    {"unit": "weldex",           "type": "eventual",       "sum_mrr": False},
    "WELDU":                     {"unit": "weldu",            "type": "recurrente",     "sum_mrr": True},
    "COMERCIALIZADORA":          {"unit": "comercializadora", "type": "referencia",     "sum_mrr": False},
    "AROMATEX AA/REF":           {"unit": "aromatex",         "type": "refacturacion",  "sum_mrr": False},
    "-":                         {"unit": None,               "type": None,             "sum_mrr": False},
}

UNKNOWN = {"unit": None, "type": None, "sum_mrr": False}

# Keywords que marcan a una subscription Weldex como recurrente
# (override del default eventual). Match case-insensitive.
WELDEX_RECURRENTE_KEYWORDS = (
    "mensual",
    "renta",
    "contrato",
    "plan",
    "mantenimiento",
    "cuota",
    "suscripcion",
    "suscripción",
    "servicio mensual",
    "trimestral",
    "semestral",
    "anual",
)


def _normalize(uen):
    """Strip + uppercase. None y no-strings devuelven ''."""
    if uen is None:
        return ""
    return str(uen).strip().upper()


def classify_uen(uen):
    """Devuelve la clasificación base de un UEN crudo. Copia el dict para
    que el caller pueda mutar sin afectar el mapa global."""
    key = _normalize(uen)
    return dict(SAVIO_UEN_MAP.get(key, UNKNOWN))


def is_weldex_recurrente(description):
    """True si el texto de description sugiere que un movimiento WELDEX es
    en realidad recurrente (override del default eventual)."""
    if not description:
        return False
    text = str(description).lower()
    return any(kw in text for kw in WELDEX_RECURRENTE_KEYWORDS)


def should_subtract_aa_ref(uen, amount):
    """True solo si el UEN normalizado es AROMATEX AA/REF y el monto es
    negativo. Las refacturaciones positivas no restan del MRR."""
    if amount is None:
        return False
    if _normalize(uen) != "AROMATEX AA/REF":
        return False
    try:
        return float(amount) < 0
    except (TypeError, ValueError):
        return False


def classify_subscription(uen, description=None, amount=None):
    """Clasificación completa aplicando todas las reglas en orden.

    Devuelve dict con: unit, type, sum_mrr, effective_amount_sign, uen_normalized.
    effective_amount_sign por default es 1; el sync lo usa para decidir si
    invertir el signo del monto al sumar al MRR.
    """
    uen_normalized = _normalize(uen)
    base = classify_uen(uen)

    result = {
        "unit": base["unit"],
        "type": base["type"],
        "sum_mrr": base["sum_mrr"],
        "effective_amount_sign": 1,
        "uen_normalized": uen_normalized,
    }

    # Override Weldex eventual → recurrente cuando description lo indica.
    if uen_normalized == "WELDEX" and is_weldex_recurrente(description):
        result["unit"] = "weldex"
        result["type"] = "recurrente"
        result["sum_mrr"] = True

    # AA/REF con monto negativo: marcar como resta sin tocar sum_mrr.
    if should_subtract_aa_ref(uen, amount):
        result["effective_amount_sign"] = -1

    return result


if __name__ == "__main__":
    cases = [
        ("AROMATEX RECURRENTE", None, None),
        ("PESTEX RECURRENTE", None, None),
        ("QTB", None, None),
        ("WELDU", None, None),
        ("PESTEX POLIZAS ", None, None),                                  # espacio extra
        ("WELDEX", "Mantenimiento mensual de planta industrial", None),   # weldex → recurrente
        ("WELDEX", "Reparación puntual de equipo dañado", None),          # weldex → eventual
        ("AROMATEX AA/REF", None, 1500.0),                                # refac positiva
        ("AROMATEX AA/REF", None, -1500.0),                               # refac negativa (resta)
        ("FOOBAR", None, None),                                           # desconocido
        ("", None, None),                                                 # vacío
        (None, None, None),                                               # None
    ]

    for uen, desc, amount in cases:
        out = classify_subscription(uen, desc, amount)
        print(f"  uen={uen!r}  desc={desc!r}  amount={amount}")
        print(f"    -> {out}")
        print()
