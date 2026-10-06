"""Regresiones de expansión: acceso, clasificación, cierre y forecast."""
from decimal import Decimal

import pytest

from extensions import db
from models import Oportunidad, EtapaOportunidad as E, Sale
from tests import fabricas as f
from blueprints.oportunidades import _sale_type_por_defecto


def _client(app, vendedor=None, role="super_admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "pruebas"
        session["user_rol"] = role
        if vendedor:
            session["usuario_id"] = str(vendedor.id)
    return client


def _op(owner=None, account=None, **extra):
    op = Oportunidad(nombre="Expansión", marca_interes="Aromatex", valor=12000,
                     monthly_amount=1000, etapa=E.CALIFICACION,
                     propietario_id=owner.id if owner else None,
                     account_id=account.id if account else None, **extra)
    db.session.add(op)
    db.session.commit()
    return op


def test_cliente_de_lead_se_clasifica_como_upsell(app):
    with app.app_context():
        account = f.empresa()
        lead = f.lead(account_id=account.id)
        db.session.add(Sale(lead_id=lead.id, unit="aromatex", sale_type="suscripcion_nueva", status="activa"))
        db.session.commit()
        assert _sale_type_por_defecto(_op(account=account), 1000) == "upsell"


def test_cliente_cs_se_encuentra_por_nombre_comercial_y_espacios(app):
    with app.app_context():
        account = f.empresa("Razón social SA")
        account.nombre_comercial = "  Cliente   Comercial "
        db.session.commit()
        f.cuenta_cs(nombre="cliente comercial", mrr=5000)
        client = _client(app)
        rows = client.get("/api/oportunidades/cuentas-upsell?q=Razón").get_json()
        assert [row["id"] for row in rows] == [str(account.id)]
        assert _sale_type_por_defecto(_op(account=account), 1000) == "upsell"


def test_cliente_de_otra_unidad_es_expansion(app):
    with app.app_context():
        account = f.empresa()
        previous = _op(account=account)
        db.session.add(Sale(opportunity_id=previous.id, unit="pestex", sale_type="suscripcion_nueva", status="activa"))
        db.session.commit()
        assert _sale_type_por_defecto(_op(account=account), 1000) == "upsell"


def test_reabrir_cancela_venta_y_recerrar_reutiliza_la_misma(app):
    with app.app_context():
        op = _op(owner=f.vendedor(), sale_type="upsell")
        client = _client(app)
        url = f"/api/oportunidades/{op.id}/mover"
        assert client.patch(url, json={"etapa": E.CIERRE_GANADO.value}).status_code == 200
        sale = Sale.query.filter_by(opportunity_id=op.id).one()
        sale_id = sale.id
        assert float(sale.commission_amount) == 1000
        for stage in (E.NEGOCIACION, E.CIERRE_PERDIDO):
            assert client.patch(url, json={"etapa": stage.value}).status_code == 200
            db.session.refresh(sale)
            assert sale.status == "cancelada"
            assert sale.commission_status == "cancelada"
        assert client.patch(url, json={"etapa": E.CIERRE_GANADO.value}).status_code == 200
        db.session.refresh(sale)
        assert sale.id == sale_id
        assert sale.status == "activa"
        assert sale.commission_status == "pendiente"
        assert sale.canceled_at is None
        assert Sale.query.filter_by(opportunity_id=op.id).count() == 1


@pytest.mark.parametrize("path", ["", "/mover"])
def test_cierre_sin_mensualidad_se_rechaza(app, path):
    with app.app_context():
        op = _op(sale_type="upsell")
        op.monthly_amount = None
        db.session.commit()
        response = _client(app).patch(f"/api/oportunidades/{op.id}{path}", json={"etapa": E.CIERRE_GANADO.value})
        assert response.status_code == 400
        assert Sale.query.count() == 0


def test_cierre_unico_sin_valor_se_rechaza(app):
    with app.app_context():
        op = _op(sale_type="servicio_unico")
        op.valor = 0
        db.session.commit()
        assert _client(app).patch(f"/api/oportunidades/{op.id}/mover", json={"etapa": E.CIERRE_GANADO.value}).status_code == 400
        assert Sale.query.count() == 0


@pytest.mark.parametrize("method,suffix", [("get", ""), ("patch", ""), ("patch", "/mover"), ("delete", ""), ("get", "/evidencia-cierre"), ("post", "/evidencia-cierre")])
def test_vendedor_no_opera_oportunidades_ajenas(app, method, suffix):
    with app.app_context():
        owner = f.vendedor("Dueño")
        outsider = f.vendedor("Otro")
        op = _op(owner=owner)
        client = _client(app, outsider, "Vendedor")
        response = getattr(client, method)(f"/api/oportunidades/{op.id}{suffix}", json={"etapa": E.NEGOCIACION.value})
        assert response.status_code == 404


def test_admin_de_unidad_no_consulta_ni_mueve_otra_unidad(app):
    with app.app_context():
        op = _op()
        client = _client(app, role="Super Admin Pestex")
        assert client.get(f"/api/oportunidades/{op.id}").status_code == 404
        assert client.patch(f"/api/oportunidades/{op.id}/mover", json={"etapa": E.NEGOCIACION.value}).status_code == 404


def test_vendedor_no_transfiere_propietario(app):
    with app.app_context():
        owner = f.vendedor()
        other = f.vendedor("Otro")
        op = _op(owner=owner)
        response = _client(app, owner, "Vendedor").patch(f"/api/oportunidades/{op.id}", json={"propietario_id": str(other.id)})
        assert response.status_code == 403


def test_no_crea_upsell_sobre_prospecto(app):
    with app.app_context():
        account = f.empresa()
        response = _client(app).post("/api/oportunidades/", json={"nombre": "Expansión", "account_id": str(account.id), "is_upsell": True})
        assert response.status_code == 400
        assert Oportunidad.query.count() == 0


@pytest.mark.parametrize("amount", [-1, "NaN", "Infinity", "incorrecto"])
def test_montos_invalidos_no_se_guardan(app, amount):
    with app.app_context():
        op = _op()
        response = _client(app).patch(f"/api/oportunidades/{op.id}", json={"monthly_amount": amount})
        assert response.status_code == 400
        db.session.refresh(op)
        assert op.monthly_amount == Decimal("1000")


def test_probabilidad_sigue_etapa_y_respeta_ajuste_explicito(app):
    with app.app_context():
        op = _op()
        client = _client(app)
        url = f"/api/oportunidades/{op.id}"
        assert client.patch(url+"/mover", json={"etapa": E.ANALISIS.value}).get_json()["probabilidad"] == 25
        assert client.patch(url+"/mover", json={"etapa": E.NEGOCIACION.value}).get_json()["probabilidad"] == 75
        assert client.patch(url, json={"probabilidad": 65}).get_json()["probabilidad"] == 65
        closed = client.patch(url+"/mover", json={"etapa": E.CIERRE_GANADO.value}).get_json()
        assert closed["probabilidad"] == 100
        assert closed["fecha_cierre_real"]
        reopened = client.patch(url+"/mover", json={"etapa": E.PROPUESTA.value}).get_json()
        assert reopened["probabilidad"] == 50
        assert reopened["fecha_cierre_real"] is None


def test_oportunidad_con_venta_conserva_historial(app):
    with app.app_context():
        op = _op()
        client = _client(app)
        assert client.patch(f"/api/oportunidades/{op.id}/mover", json={"etapa": E.CIERRE_GANADO.value}).status_code == 200
        assert client.delete(f"/api/oportunidades/{op.id}").status_code == 409
        assert Sale.query.count() == 1


def test_cancelar_no_borra_el_registro_de_comision_pagada(app):
    with app.app_context():
        op = _op(sale_type="upsell")
        client = _client(app)
        url = f"/api/oportunidades/{op.id}/mover"
        client.patch(url, json={"etapa": E.CIERRE_GANADO.value})
        sale = Sale.query.one()
        sale.commission_status = "pagada"
        db.session.commit()
        assert client.patch(url, json={"etapa": E.NEGOCIACION.value}).status_code == 200
        db.session.refresh(sale)
        assert sale.status == "cancelada"
        assert sale.commission_status == "pagada"


def test_crear_cierre_recurrente_sin_monto_no_genera_registros(app):
    with app.app_context():
        response = _client(app).post("/api/oportunidades/", json={
            "nombre": "Expansión", "etapa": E.CIERRE_GANADO.value,
            "sale_type": "upsell", "valor": 1000})
        assert response.status_code == 400
        assert Sale.query.count() == 0
        assert Oportunidad.query.count() == 0


def test_pestex_sigue_exigiendo_evidencia(app):
    with app.app_context():
        op = _op(sale_type="upsell")
        op.marca_interes = "Pestex"
        db.session.commit()
        response = _client(app).patch(f"/api/oportunidades/{op.id}/mover", json={"etapa": E.CIERRE_GANADO.value})
        assert response.status_code == 422
        assert response.get_json()["requiere_evidencia"]
        assert Sale.query.count() == 0


def test_pipe_renderiza_javascript_valido(app):
    import shutil
    import subprocess
    from html.parser import HTMLParser

    if not shutil.which("node"):
        pytest.skip("Node no disponible")

    class Scripts(HTMLParser):
        def __init__(self):
            super().__init__()
            self.current = None
            self.scripts = []

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.current = []

        def handle_data(self, data):
            if self.current is not None:
                self.current.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.current is not None:
                self.scripts.append("".join(self.current))
                self.current = None

    with app.app_context():
        response = _client(app).get("/")
        assert response.status_code == 200
        parser = Scripts()
        parser.feed(response.get_data(as_text=True))
        assert parser.scripts
        for script in parser.scripts:
            check = subprocess.run(["node", "--check"], input=script, text=True, capture_output=True)
            assert check.returncode == 0, check.stderr


def test_editar_venta_ganada_no_permite_quitar_su_monto(app):
    with app.app_context():
        op = _op(sale_type="upsell")
        client = _client(app)
        url = f"/api/oportunidades/{op.id}"
        client.patch(url+"/mover", json={"etapa": E.CIERRE_GANADO.value})
        assert client.patch(url, json={"valor": 0}).status_code == 400
        db.session.refresh(op)
        assert op.valor == 12000
        assert Sale.query.one().total_amount == 12000


def test_editar_marca_de_venta_ganada_no_elude_evidencia(app):
    with app.app_context():
        op = _op(sale_type="upsell")
        client = _client(app)
        url = f"/api/oportunidades/{op.id}"
        client.patch(url+"/mover", json={"etapa": E.CIERRE_GANADO.value})
        assert client.patch(url, json={"marca_interes": "Pestex"}).status_code == 422
        db.session.refresh(op)
        assert op.marca_interes == "Aromatex"
        assert Sale.query.one().unit == "aromatex"


def test_cierre_por_api_clasifica_el_id_de_cliente_recibido_como_texto(app):
    with app.app_context():
        account = f.empresa("Cliente CS")
        f.cuenta_cs(nombre="Cliente CS", mrr=5000)
        response = _client(app).post("/api/oportunidades/", json={
            "nombre": "Más sucursales", "account_id": str(account.id),
            "marca_interes": "Aromatex", "valor": 12000, "monthly_amount": 1000,
            "etapa": E.CIERRE_GANADO.value})
        assert response.status_code == 201
        assert response.get_json()["sale_type"] == "upsell"
        assert Sale.query.one().sale_type == "upsell"
