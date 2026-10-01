"""Cierre de un lead como ganado: la venta y la comision.

Es el flujo que mueve dinero. Equivocarse aqui no da un error visible:
da una comision mal pagada que nadie nota hasta la nomina.
"""
from datetime import date

import pytest

from extensions import db
from models import EtapaPipeline, Sale
from tests import fabricas as f


def _cerrar(lead, **datos):
    from blueprints.leads import cerrar_lead_core
    base = {"unidad": "Aromatex", "sale_type": "servicio_unico",
            "sale_category": "eventual", "mensualidad_cerrada": 10000,
            "total_amount": 10000}
    base.update(datos)
    return cerrar_lead_core(lead, base, quien_cierra=str(lead.usuario_asignado_id))


def test_cerrar_registra_la_venta_con_el_monto(app):
    with app.app_context():
        v = f.vendedor()
        l = f.lead(propietario=v)
        salida, estado = _cerrar(l, mensualidad_cerrada=25000, total_amount=25000)

        assert estado == 201
        venta = Sale.query.filter_by(lead_id=l.id).one()
        assert float(venta.total_amount) == 25000
        assert venta.sale_type == "servicio_unico"
        assert venta.user_id == v.id


def test_cerrar_mueve_la_etapa_y_sella_la_fecha(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor())
        _cerrar(l)

        db.session.refresh(l)
        assert l.etapa_pipeline == EtapaPipeline.CIERRE_GANADO
        assert l.fecha_cierre is not None
        # La venta y el lead tienen que caer en el MISMO mes, o Metas y
        # Comisiones reportan cifras distintas para el mismo cierre.
        venta = Sale.query.filter_by(lead_id=l.id).one()
        assert l.fecha_cierre.date() == venta.closed_at.date()


def test_no_se_puede_cerrar_dos_veces(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor())
        _cerrar(l)
        salida, estado = _cerrar(l)

        assert estado == 409
        assert Sale.query.filter_by(lead_id=l.id).count() == 1


def test_sin_monto_la_venta_queda_pendiente_no_en_cero(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor(), valor=0)
        salida, estado = _cerrar(l, mensualidad_cerrada=0, total_amount=0)

        assert estado == 201
        venta = Sale.query.filter_by(lead_id=l.id).one()
        # Una venta sin monto NO es una comision de cero legitima: queda
        # esperando la factura. Confundirlas es pagar de menos.
        assert venta.commission_status == "pendiente_monto"


def test_pestex_exige_respaldo_para_cerrar(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor(marcas=("Pestex",)), marca="Pestex")
        salida, estado = _cerrar(l, unidad="Pestex")

        # Pestex vende a credito: al cerrar no hay factura todavia, asi que
        # el respaldo es la unica prueba de que la venta existe.
        assert estado == 422
        assert salida.get("requiere_evidencia") is True
        assert Sale.query.filter_by(lead_id=l.id).count() == 0


def test_aromatex_no_exige_respaldo(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor())
        salida, estado = _cerrar(l)
        assert estado == 201
