"""Leads y oportunidades multi UN: quién cambia las unidades y qué exige el cierre.

Una venta cruzada paga una tasa distinta. Si agregar una unidad no costara
nada, sería la forma de inflar una comisión: por eso cada unidad sube su
propio respaldo, un trato cerrado ya no cambia de unidades y solo un
gerente quita unidades.
"""
import secrets

from extensions import db
from models import (CierreEvidencia, EtapaOportunidad as E, EtapaPipeline,
                    Oportunidad, Sale)
from tests import fabricas as f

import cierre_evidencia as CE


def _client(app, vendedor=None, role="super_admin"):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "pruebas"
        session["user_rol"] = role
        if vendedor:
            session["usuario_id"] = str(vendedor.id)
    return client


def _respaldo(unidad=None, lead=None, op=None, tipo="contrato"):
    db.session.add(CierreEvidencia(
        lead_id=lead.id if lead else None,
        oportunidad_id=op.id if op else None,
        tipo=tipo, unidad=unidad,
        archivo_url=f"https://pruebas.test/{secrets.token_hex(4)}.pdf"))
    db.session.commit()


def _cerrar(lead):
    from blueprints.leads import cerrar_lead_core
    return cerrar_lead_core(lead, {
        "unidad": lead.marca_interes, "sale_type": "servicio_unico",
        "sale_category": "eventual", "mensualidad_cerrada": 10000,
        "total_amount": 10000}, quien_cierra=str(lead.usuario_asignado_id))


def _lead_cruzado(**extra):
    l = f.lead(propietario=f.vendedor(marcas=("Aromatex", "Café Origen")), **extra)
    l.marcas_interes = ["Aromatex", "Café Origen"]
    db.session.commit()
    return l


def _op(marcas=("Aromatex",), etapa=E.NEGOCIACION, **extra):
    op = Oportunidad(nombre="Venta cruzada", marca_interes=marcas[0],
                     marcas_interes=list(marcas), valor=12000,
                     monthly_amount=1000, etapa=etapa, **extra)
    db.session.add(op)
    db.session.commit()
    return op


# ── Reglas de respaldo ────────────────────────────────────────────

def test_una_sola_unidad_sigue_la_regla_de_antes():
    assert CE.unidades_que_exigen("Aromatex") == []
    assert CE.unidades_que_exigen(["PESTEX RECURRENTE", "Pestex"]) == ["Pestex"]


def test_venta_cruzada_exige_a_todas_las_unidades():
    assert CE.unidades_que_exigen(["Aromatex", "Aromatex Home", "Café Origen"]) == ["Aromatex", "Café Origen"]


def test_respaldo_de_venta_cruzada_debe_decir_su_unidad():
    unidad, err = CE.unidad_para_guardar(None, ["Aromatex", "Café Origen"])
    assert unidad is None and err
    unidad, err = CE.unidad_para_guardar("Pestex", ["Aromatex", "Café Origen"])
    assert unidad is None and err
    assert CE.unidad_para_guardar(None, "Pestex") == ("Pestex", None)


def test_venta_cruzada_no_cierra_sin_respaldo(app):
    with app.app_context():
        l = _lead_cruzado()
        salida, estado = _cerrar(l)
        assert estado == 422
        assert Sale.query.filter_by(lead_id=l.id).count() == 0


def test_venta_cruzada_no_cierra_con_el_respaldo_de_una_sola_unidad(app):
    with app.app_context():
        l = _lead_cruzado()
        _respaldo("Aromatex", lead=l)
        salida, estado = _cerrar(l)
        assert estado == 422
        assert "Café Origen" in salida["error"]


def test_respaldo_sin_unidad_no_cuenta_en_venta_cruzada(app):
    with app.app_context():
        l = _lead_cruzado()
        _respaldo(None, lead=l)
        _respaldo(None, lead=l, tipo="whatsapp")
        assert _cerrar(l)[1] == 422


def test_venta_cruzada_cierra_con_respaldo_de_cada_unidad(app):
    with app.app_context():
        l = _lead_cruzado()
        _respaldo("Aromatex", lead=l)
        _respaldo("Café Origen", lead=l)
        assert _cerrar(l)[1] == 201


# ── Editar unidades de un lead ────────────────────────────────────

def test_lead_cerrado_no_cambia_de_unidades(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor(), etapa=EtapaPipeline.CIERRE_GANADO)
        r = _client(app).put(f"/api/leads/{l.id}",
                             json={"marcas_interes": ["Aromatex", "Café Origen"]})
        assert r.status_code == 409
        db.session.refresh(l)
        assert l.marcas_interes == ["Aromatex"]


def test_vendedor_agrega_pero_no_quita_unidades(app):
    with app.app_context():
        v = f.vendedor()
        l = f.lead(propietario=v)
        client = _client(app, vendedor=v, role="vendedor")
        r = client.put(f"/api/leads/{l.id}", json={"marcas_interes": ["Aromatex", "Café Origen"]})
        assert r.status_code == 200
        r = client.put(f"/api/leads/{l.id}", json={"marcas_interes": ["Aromatex"]})
        assert r.status_code == 403
        assert "gerente" in r.get_json()["error"]
        db.session.refresh(l)
        assert l.marcas_interes == ["Aromatex", "Café Origen"]


def test_gerente_puede_quitar_una_unidad(app):
    with app.app_context():
        l = _lead_cruzado()
        r = _client(app).put(f"/api/leads/{l.id}", json={"marcas_interes": ["Aromatex"]})
        assert r.status_code == 200
        db.session.refresh(l)
        assert l.marcas_interes == ["Aromatex"]


def test_la_unidad_duena_no_se_puede_quitar(app):
    with app.app_context():
        l = _lead_cruzado()
        _client(app).put(f"/api/leads/{l.id}", json={"marcas_interes": ["Café Origen"]})
        db.session.refresh(l)
        assert l.marca_interes == "Aromatex"
        assert "Aromatex" in l.marcas_interes


# ── Oportunidades ─────────────────────────────────────────────────

def test_oportunidad_cruzada_no_cierra_sin_respaldo_de_cada_unidad(app):
    with app.app_context():
        op = _op(marcas=("Aromatex", "Pestex"))
        _respaldo("Pestex", op=op)
        url = f"/api/oportunidades/{op.id}/mover"
        client = _client(app)
        assert client.patch(url, json={"etapa": E.CIERRE_GANADO.value}).status_code == 422
        _respaldo("Aromatex", op=op)
        assert client.patch(url, json={"etapa": E.CIERRE_GANADO.value}).status_code == 200


def test_oportunidad_cruzada_no_nace_cerrada(app):
    with app.app_context():
        account = f.empresa()
        r = _client(app).post("/api/oportunidades/", json={
            "nombre": "Cruzada", "account_id": str(account.id),
            "marca_interes": "Aromatex", "marcas_interes": ["Aromatex", "Café Origen"],
            "valor": 12000, "monthly_amount": 1000, "etapa": E.CIERRE_GANADO.value})
        assert r.status_code == 422


def test_oportunidad_cerrada_no_cambia_de_unidades(app):
    with app.app_context():
        op = _op(etapa=E.CIERRE_PERDIDO)
        r = _client(app).patch(f"/api/oportunidades/{op.id}",
                               json={"marcas_interes": ["Aromatex", "Café Origen"]})
        assert r.status_code == 409


def test_vendedor_no_quita_unidades_de_una_oportunidad(app):
    with app.app_context():
        v = f.vendedor(marcas=("Aromatex", "Café Origen"))
        op = _op(marcas=("Aromatex", "Café Origen"), propietario_id=v.id)
        r = _client(app, vendedor=v, role="vendedor").patch(
            f"/api/oportunidades/{op.id}", json={"marcas_interes": ["Aromatex"]})
        assert r.status_code == 403
        assert "gerente" in r.get_json()["error"]


def test_oportunidad_de_otra_unidad_no_cuelga_del_lead(app):
    """Ganar la oportunidad de Pestex daba por ganado el lead de Aromatex
    que seguía estancado: otra unidad al mismo cliente va en un lead nuevo."""
    with app.app_context():
        l = f.lead(propietario=f.vendedor(), empresa_nombre="Pastelería Leti")
        client = _client(app)
        r = client.post(f"/api/oportunidades/from-lead/{l.id}",
                        json={"marca_interes": "Pestex", "marcas_interes": ["Pestex"]})
        assert r.status_code == 400
        assert "lead nuevo" in r.get_json()["error"]

        op = _op(lead_id=l.id)
        r = client.patch(f"/api/oportunidades/{op.id}",
                         json={"marcas_interes": ["Aromatex", "Pestex"]})
        assert r.status_code == 400
        db.session.refresh(l)
        assert l.marcas_interes == ["Aromatex"]


def test_oportunidad_con_las_unidades_del_lead_si_se_crea(app):
    with app.app_context():
        l = f.lead(propietario=f.vendedor())
        r = _client(app).post(f"/api/oportunidades/from-lead/{l.id}", json={})
        assert r.status_code == 201
        assert r.get_json()["marcas_interes"] == ["Aromatex"]


# ── Venta cruzada por cliente ─────────────────────────────────────

def _cliente_que_compra(unidad="aromatex", status="activa"):
    """Una cuenta con una venta ya cerrada de `unidad`."""
    cuenta = f.empresa("Pastelería Leti")
    previo = f.lead(propietario=f.vendedor(), account_id=cuenta.id,
                    etapa=EtapaPipeline.CIERRE_GANADO)
    db.session.add(Sale(lead_id=previo.id, unit=unidad, sale_type="servicio_unico",
                        status=status))
    db.session.commit()
    return cuenta


def test_otra_unidad_al_mismo_cliente_es_venta_cruzada(app):
    import comisiones as C
    with app.app_context():
        cuenta = _cliente_que_compra("aromatex")
        nuevo = f.lead(propietario=f.vendedor(marcas=("Café Origen",)),
                       marca="Café Origen", account_id=cuenta.id)
        assert C.unidades_cruzadas_del_cliente(nuevo) == ["Aromatex"]
        assert C.lead_es_multi_un(nuevo)


def test_la_misma_unidad_al_mismo_cliente_no_es_cruzada(app):
    import comisiones as C
    with app.app_context():
        cuenta = _cliente_que_compra("aromatex")
        nuevo = f.lead(propietario=f.vendedor(), account_id=cuenta.id)
        assert not C.lead_es_multi_un(nuevo)


def test_venta_cancelada_no_vuelve_cruzada_a_la_siguiente(app):
    import comisiones as C
    with app.app_context():
        cuenta = _cliente_que_compra("aromatex", status="cancelada")
        nuevo = f.lead(propietario=f.vendedor(marcas=("Café Origen",)),
                       marca="Café Origen", account_id=cuenta.id)
        assert not C.lead_es_multi_un(nuevo)


def test_venta_cruzada_por_cliente_exige_su_respaldo(app):
    """Paga tasa cruzada, así que sube su respaldo aunque no sea Pestex."""
    with app.app_context():
        cuenta = _cliente_que_compra("aromatex")
        nuevo = f.lead(propietario=f.vendedor(marcas=("Café Origen",)),
                       marca="Café Origen", account_id=cuenta.id)
        salida, estado = _cerrar(nuevo)
        assert estado == 422
        assert "ya compra Aromatex" in salida["error"]

        _respaldo("Café Origen", lead=nuevo)
        assert _cerrar(nuevo)[1] == 201


def test_sin_compra_previa_del_cliente_no_pide_respaldo(app):
    with app.app_context():
        nuevo = f.lead(propietario=f.vendedor(marcas=("Café Origen",)),
                       marca="Café Origen", account_id=f.empresa().id)
        assert _cerrar(nuevo)[1] == 201


def test_oportunidad_cruzada_aparece_en_la_vista_de_cada_unidad(app):
    with app.app_context():
        op = _op(marcas=("Aromatex", "Café Origen"))
        ids = [o["id"] for o in _client(app).get("/api/oportunidades/?marca=Café Origen").get_json()]
        assert str(op.id) in ids


def test_nexo_ya_no_se_ofrece_pero_no_se_pierde(app):
    """Se quitó de las pantallas, no del catálogo: un lead que ya la tiene
    la conserva al editarse."""
    from un_filter import normalizar_marcas
    with app.app_context():
        me = _client(app).get("/api/leads/me").get_json()
        assert "Nexo" not in me["marcas"]
        assert "Weldex" in me["marcas"]
        assert normalizar_marcas(["Nexo"]) == ["Nexo"]
