"""Contrato de Savio en las pantallas de trabajo de CS.

Lo que importa: que el monto sea el de Savio con el mismo criterio que el
puente, que una cuenta sin vínculo no se confunda con una de $0, y que la
meta propuesta esté en la misma base (con IVA) que lo facturado.
"""
import secrets
from datetime import date, timedelta
from decimal import Decimal

from extensions import db
from models import CSContrato, CustomerMaster, CustomerRfc, RolCRM, SavioSubscription
from tests import fabricas as f
import cs_savio

HOY = date.today()


def _vincular(cuenta, *clientes):
    m = CustomerMaster(master_name=cuenta.nombre, cs_account_id=cuenta.id)
    db.session.add(m)
    db.session.flush()
    for cid in clientes:
        db.session.add(CustomerRfc(master_id=m.id, rfc="XAXX010101000", savio_customer_id=cid))
    db.session.commit()


def _sub(cliente, mrr, unit="pestex", sum_mrr=True, inicio=None, fin=None):
    db.session.add(SavioSubscription(
        id=secrets.token_hex(6), customer_id=cliente, mrr=Decimal(str(mrr)),
        sum_mrr=sum_mrr, unit=unit, start_date=inicio or HOY - timedelta(days=90),
        contract_end_date=fin))
    db.session.commit()


def _cliente(app, user):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = str(user.id)
        s["user_rol"] = user.rol.value
        s["user_nombre"] = user.nombre
    return c


# ── Cálculo ──────────────────────────────────────────────────────

def test_sin_vinculo_no_es_cero(app):
    with app.app_context():
        cuenta = f.cuenta_cs("Sin vínculo")
        c = cs_savio.contrato(cuenta.id)
        assert c["vinculada"] is False and c["mrr"] is None and c["meta_sugerida"] is None


def test_suma_solo_lo_vigente_y_recurrente_como_el_puente(app):
    with app.app_context():
        cuenta = f.cuenta_cs("Grupo Prueba")
        _vincular(cuenta, "S1", "S2")
        _sub("S1", 10000, "pestex")
        _sub("S2", 5000.50, "aromatex", fin=HOY + timedelta(days=30))
        _sub("S1", 999, sum_mrr=False)                              # eventual
        _sub("S1", 888, fin=HOY - timedelta(days=1))                # terminó
        _sub("S2", 777, inicio=HOY + timedelta(days=5))             # aún no empieza
        _sub("OTRO", 666)                                           # de otro cliente

        c = cs_savio.contrato(cuenta.id)
        assert c["vinculada"] and c["suscripciones"] == 2
        assert c["mrr"] == Decimal("15000.50")
        assert c["arr"] == Decimal("180006.00")
        assert c["meta_sugerida"] == Decimal("17400.58")             # con IVA
        assert c["unidades"] == ["Aromatex", "Pestex"]


def test_vinculada_sin_suscripciones_es_cero_sin_meta(app):
    with app.app_context():
        cuenta = f.cuenta_cs("Vinculada vacía")
        _vincular(cuenta, "S9")
        c = cs_savio.contrato(cuenta.id)
        assert c["vinculada"] and c["mrr"] == 0 and c["meta_sugerida"] is None


# ── Conciliación con el contrato cargado ─────────────────────────

def _savio(mrr):
    return {"vinculada": True, "mrr": Decimal(mrr)}


def test_concilia_en_monto_mensual():
    r = cs_savio.conciliar(_savio("21000"), {"importe_activos": "40000", "cada_meses": 2})
    assert r["mensual_contrato"] == Decimal("20000.00") and r["cuadra"]


def test_avisa_cuando_el_contrato_supera_a_savio():
    r = cs_savio.conciliar(_savio("10000"), {"importe_activos": "40000", "cada_meses": 2})
    assert not r["cuadra"] and r["diferencia"] == Decimal("10000.00")
    assert "mayor que lo registrado en Savio" in r["mensaje"]


def test_contrato_sin_suscripcion_en_savio():
    r = cs_savio.conciliar(_savio("0"), {"importe_activos": "1000", "cada_meses": 1})
    assert not r["cuadra"] and r["porcentaje"] is None


def test_sin_ciclo_comun_o_sin_vinculo_no_se_compara():
    assert cs_savio.conciliar(_savio("100"), {"importe_activos": "1000", "cada_meses": None}) is None
    assert cs_savio.conciliar({"vinculada": False, "mrr": None},
                              {"importe_activos": "1000", "cada_meses": 1}) is None


# ── Pantallas ────────────────────────────────────────────────────

def test_seguimiento_semanal_muestra_contrato_y_cuentas_sin_vincular(app):
    with app.app_context():
        admin = f.login("Dirección", RolCRM.SUPER_ADMIN)
        con = f.cuenta_cs("Con Savio")
        _vincular(con, "S1")
        _sub("S1", 12000)
        f.cuenta_cs("Sin Savio")
        html = _cliente(app, admin).get("/cs/semanal?alcance=todas").get_data(as_text=True)
        assert "Contrato Savio <strong>$12,000/mes</strong>" in html
        assert "1 cuentas sin vincular a Savio" in html
        assert "/?vincular=" in html


def test_kam_ve_el_aviso_pero_no_el_boton_de_vincular(app):
    with app.app_context():
        kam = f.login("KAM", RolCRM.KAM)
        f.cuenta_cs("Sin Savio", kam=kam)
        c = _cliente(app, kam)
        html = c.get("/cs/semanal?alcance=todas").get_data(as_text=True)
        assert "Sin vincular a Savio" in html and "/?vincular=" not in html
        hoy = c.get("/cs/").get_data(as_text=True)
        assert "Cuentas sin vincular a Savio · 1" in hoy and "Pide a Administración" in hoy


def test_trabajo_de_cuenta_muestra_contrato_y_conciliacion(app):
    with app.app_context():
        admin = f.login("Dirección", RolCRM.SUPER_ADMIN)
        cuenta = f.cuenta_cs("Cliente bimestral")
        _vincular(cuenta, "S1")
        _sub("S1", 10000)
        db.session.add(CSContrato(account_id=cuenta.id, nombre="c.csv", mimetype="text/csv",
                                  contenido=b"x", sha256="0" * 64, bytes=1, activo=True,
                                  resumen={"importe_activos": "40000", "cada_meses": 2,
                                           "activos": 10, "cancelados": 1, "sucursales": 10,
                                           "pendiente": ""},
                                  created_by="Prueba", actor_id="x"))
        db.session.commit()
        html = _cliente(app, admin).get(f"/cs/cuentas/{cuenta.id}").get_data(as_text=True)
        assert "Contrato en Savio" in html and "$10,000" in html and "$11,600" in html
        assert "Contrato cargado: <strong>$20,000 al mes</strong>" in html
        assert "+100%" in html


def test_reporte_propone_la_meta_con_iva(app):
    with app.app_context():
        admin = f.login("Dirección", RolCRM.SUPER_ADMIN)
        cuenta = f.cuenta_cs("Con Savio")
        _vincular(cuenta, "S1")
        _sub("S1", 10000)
        html = _cliente(app, admin).get(f"/cs/account/{cuenta.id}/semanal").get_data(as_text=True)
        assert 'data-meta-savio="11600.00"' in html
