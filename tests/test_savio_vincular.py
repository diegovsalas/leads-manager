"""Vincular una cuenta de CS con sus clientes de Savio.

La sincronización ya deja una fila de customer_rfcs por cada cliente de
Savio, agrupadas por RFC. El RFC no es único (el genérico XAXX010101000 lo
comparten muchos clientes), así que vincular tiene que ubicar la fila por
cliente de Savio, nunca por RFC.
"""
from extensions import db
from models import CustomerMaster, CustomerRfc, SavioCustomer
from tests import fabricas as f
import savio_sync

GENERICO = "XAXX010101000"


def _savio(cid, rfc, nombre=None):
    db.session.add(SavioCustomer(customer_id=cid, name=nombre or f"Cliente {cid}",
                                 legal_name=nombre or f"Cliente {cid}", tax_id=rfc))
    db.session.commit()


def _post(app, cuenta, ids):
    c = app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = "pruebas"
        s["user_rol"] = "Super Admin"
    return c.post("/api/savio/customers/master/link",
                  json={"cs_account_id": str(cuenta.id), "savio_customer_ids": ids})


def _ligados(cuenta):
    m = CustomerMaster.query.filter_by(cs_account_id=cuenta.id).first()
    return sorted(r.savio_customer_id for r in m.rfcs) if m else []


def test_rfc_generico_vincula_al_cliente_elegido_y_no_a_otro(app):
    with app.app_context():
        _savio("A", GENERICO)
        _savio("B", GENERICO)
        savio_sync.auto_populate_masters()      # como la sincronización real
        cuenta = f.cuenta_cs("Cuenta B")

        r = _post(app, cuenta, ["B"])
        assert r.status_code == 200, r.get_data(as_text=True)
        assert _ligados(cuenta) == ["B"]
        # A sigue siendo de su propio grupo, no se movió con B.
        a = CustomerRfc.query.filter_by(savio_customer_id="A").one()
        assert a.master.cs_account_id is None


def test_segundo_cliente_con_el_mismo_rfc_si_se_agrega(app):
    with app.app_context():
        _savio("A", "ABC010101AB1")
        _savio("B", "ABC010101AB1")
        savio_sync.auto_populate_masters()
        cuenta = f.cuenta_cs("Empresa con dos razones")

        _post(app, cuenta, ["A"])
        r = _post(app, cuenta, ["B"])
        assert r.status_code == 200
        assert _ligados(cuenta) == ["A", "B"]


def test_cliente_sin_rfc_tambien_se_vincula(app):
    with app.app_context():
        _savio("SINRFC", None)
        cuenta = f.cuenta_cs("Sin RFC")
        r = _post(app, cuenta, ["SINRFC"])
        assert r.status_code == 200
        assert _ligados(cuenta) == ["SINRFC"]
        assert r.get_json()["not_found"] == []


def test_cliente_inexistente_se_reporta(app):
    with app.app_context():
        cuenta = f.cuenta_cs("Cuenta")
        r = _post(app, cuenta, ["NOEXISTE"])
        assert r.get_json()["not_found"] == ["NOEXISTE"]


def test_volver_a_vincular_lo_mismo_no_duplica(app):
    with app.app_context():
        _savio("A", "ABC010101AB1")
        savio_sync.auto_populate_masters()
        cuenta = f.cuenta_cs("Cuenta")
        _post(app, cuenta, ["A"])
        r = _post(app, cuenta, ["A"])
        assert r.get_json()["already_linked"] == 1
        assert CustomerRfc.query.filter_by(savio_customer_id="A").count() == 1
