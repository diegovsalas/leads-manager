"""Avance contra la meta mensual.

El criterio de "cuando cuenta una venta" es el nudo de este flujo: Metas,
Comisiones y Revision Comercial tienen que medir por el MISMO dia o
reportan cifras distintas del mismo cierre.
"""
from datetime import datetime, timezone

from blueprints.metas import _calc_pct, _calcular_ventas
from extensions import db
from models import EtapaPipeline
from tests import fabricas as f


def _dia(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


def test_cuenta_por_fecha_de_cierre_no_de_creacion(app):
    """FIX-2026-09-09: antes contaba por cuando ENTRO el lead.

    Un trato que entra en julio y cierra en septiembre es venta de
    septiembre. Medirlo por la entrada dejaba el mes del cierre casi
    vacio y no cuadraba con Comisiones, que si usa la fecha de cierre.
    """
    with app.app_context():
        v = f.vendedor()
        f.lead(propietario=v, valor=50000, etapa=EtapaPipeline.CIERRE_GANADO,
               fecha_creacion=_dia(2026, 7, 10), fecha_cierre=_dia(2026, 9, 15))

        assert _calcular_ventas(str(v.id), "2026-09") == 50000
        assert _calcular_ventas(str(v.id), "2026-07") == 0


def test_separa_recurrente_de_eventual(app):
    with app.app_context():
        v = f.vendedor()
        for tipo, monto in [("Recurrente", 30000), ("Eventual", 12000)]:
            f.lead(propietario=v, valor=monto, tipo_venta=tipo,
                   etapa=EtapaPipeline.CIERRE_GANADO,
                   fecha_creacion=_dia(2026, 9, 1), fecha_cierre=_dia(2026, 9, 20))

        assert _calcular_ventas(str(v.id), "2026-09", tipo_venta="Recurrente") == 30000
        assert _calcular_ventas(str(v.id), "2026-09", tipo_venta="Eventual") == 12000
        # El total NO es la suma de los dos tipos: hay leads sin clasificar
        # que tambien cuentan. Por eso se consulta aparte.
        assert _calcular_ventas(str(v.id), "2026-09") == 42000


def test_un_lead_sin_tipo_cuenta_en_el_total(app):
    with app.app_context():
        v = f.vendedor()
        f.lead(propietario=v, valor=8000, etapa=EtapaPipeline.CIERRE_GANADO,
               fecha_creacion=_dia(2026, 9, 2), fecha_cierre=_dia(2026, 9, 9))

        assert _calcular_ventas(str(v.id), "2026-09") == 8000
        assert _calcular_ventas(str(v.id), "2026-09", tipo_venta="Recurrente") == 0


def test_un_lead_perdido_no_cuenta(app):
    with app.app_context():
        v = f.vendedor()
        f.lead(propietario=v, valor=99000, etapa=EtapaPipeline.CIERRE_PERDIDO,
               fecha_creacion=_dia(2026, 9, 1), fecha_cierre=_dia(2026, 9, 5))

        assert _calcular_ventas(str(v.id), "2026-09") == 0


def test_sin_meta_el_porcentaje_es_cero_no_una_division_por_cero():
    assert _calc_pct(50000, 0) == 0
    assert _calc_pct(0, 0) == 0
    assert _calc_pct(50000, 100000) == 50.0
    # Pasarse de la meta se reporta tal cual, no se recorta a 100.
    assert _calc_pct(150000, 100000) == 150.0
