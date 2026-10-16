"""Recomendaciones desde campo: de la visita al pipe del vendedor.

El caso de referencia: alguien de la empresa visita a Juan Pérez (cliente de
Aromatex) y ve que necesita Pestex. No tiene usuario de CRM; recomienda
desde el link general con su nombre y, si quiere, su celular.
"""
from decimal import Decimal

import pytest

from extensions import db
from models import (Account, ComisionEtapa, ComisionTasa, CSContacto, CSOpportunity,
                    Lead, LeadAssignmentNotice, LeadAtribucion, OrigenLead,
                    RolComercial, Sale, Usuario)
from tests import fabricas as f
import comisiones as C
import referidos as R

CEL_JAVIER = "81 1234 5678"


@pytest.fixture(autouse=True)
def _sin_limite_acumulado(app):
    """El límite por IP es real; entre pruebas todas vienen de la misma."""
    from extensions import limiter
    limiter.reset()


def _tecnico(nombre="Javier López", telefono=CEL_JAVIER):
    t = Usuario(nombre=nombre, rol_comercial=RolComercial.TECNICO, en_turno=False,
                especialidad_marca=[], telefono_whatsapp=telefono)
    db.session.add(t)
    db.session.commit()
    return t


def _juan_perez(**extra):
    c = f.cuenta_cs("Juan Pérez", mrr=3000, **extra)
    c.unidades_contratadas = "Aromatex"
    db.session.commit()
    return c


def _enviar(client, **form):
    datos = {"quien_nombre": "Javier López", "quien_telefono": CEL_JAVIER,
             "es_cliente": "si", "cliente_nombre": "Juan Pérez",
             "observacion": "Vi cucarachas en la cocina", "marcas": ["Pestex"]}
    datos.update(form)
    return client.post("/recomendar/", data=datos)


def _admin(client, rol="Super Admin"):
    with client.session_transaction() as s:
        s["user_id"] = "pruebas"
        s["user_rol"] = rol


# ── Formulario ───────────────────────────────────────────────────

def test_el_formulario_es_abierto_y_sin_nexo(app):
    with app.app_context():
        r = app.test_client().get("/recomendar/")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Nombre y apellido" in html and "Café Origen" in html
        assert 'value="Nexo"' not in html


@pytest.mark.parametrize("form, mensaje", [
    ({"quien_nombre": "Javier"}, "nombre y apellido"),
    ({"quien_telefono": "12345"}, "10 dígitos"),
    ({"es_cliente": ""}, "ya es cliente de Grupo Avantex"),
    ({"cliente_nombre": ""}, "nombre del negocio"),
    ({"marcas": []}, "al menos un servicio"),
    ({"marcas": ["Nexo"]}, "al menos un servicio"),
    ({"observacion": ""}, "qué viste"),
])
def test_valida_lo_minimo_sin_crear_nada(app, form, mensaje):
    with app.app_context():
        r = _enviar(app.test_client(), **form)
        assert r.status_code == 400
        assert mensaje in r.get_data(as_text=True)
        assert Lead.query.count() == 0


def test_el_celular_es_opcional(app):
    with app.app_context():
        r = _enviar(app.test_client(), quien_nombre="Ana Torres", quien_telefono="")
        assert r.status_code == 200
        lead = Lead.query.one()
        assert lead.referido_nombre == "Ana Torres" and lead.referido_telefono is None
        assert lead.referido_por_id is None


def test_la_trampa_de_bots_no_crea_nada(app):
    with app.app_context():
        r = _enviar(app.test_client(), sitio_web="http://spam.test")
        assert r.status_code == 200
        assert Lead.query.count() == 0


# ── Quién recomendó y su comisión ────────────────────────────────

def test_con_celular_registrado_queda_a_su_nombre(app):
    with app.app_context():
        t = _tecnico()
        _enviar(app.test_client(), quien_telefono="+52 1 81-1234-5678")
        lead = Lead.query.one()
        assert lead.referido_por_id == t.id
        a = LeadAtribucion.query.filter_by(etapa="prospectar").one()
        assert a.usuario_id == t.id and a.es_automatica is False


def test_sin_registro_guarda_quien_fue_pero_no_acredita(app):
    with app.app_context():
        _enviar(app.test_client(), quien_nombre="Pedro Ruiz", quien_telefono="8199998888")
        lead = Lead.query.one()
        assert lead.referido_nombre == "Pedro Ruiz" and lead.referido_por_id is None
        assert LeadAtribucion.query.count() == 0
        assert "no está registrado" in lead.notas


def test_registrarlo_despues_le_acredita_lo_anterior(app):
    with app.app_context():
        vendedor = f.vendedor("Vende Pestex", marcas=("Pestex",))
        client = app.test_client()
        _enviar(client, quien_nombre="Pedro Ruiz", quien_telefono="8199998888")
        lead = Lead.query.one()
        # El vendedor ya lo movió: la etapa automática era suya.
        C.registrar_avance(lead, "1er Contacto", vendedor.id)
        db.session.commit()

        _admin(client)
        r = client.post("/tecnicos/", data={"nombre": "Pedro Ruiz", "telefono": "81 9999 8888"})
        assert "acreditadas=1" in r.headers["Location"]
        pedro = Usuario.query.filter_by(rol_comercial=RolComercial.TECNICO).one()
        db.session.refresh(lead)
        assert lead.referido_por_id == pedro.id
        assert C.atribucion_de(lead.id)["prospectar"] == pedro.id


def test_registrar_no_toca_una_venta_ya_cerrada(app):
    with app.app_context():
        vendedor = f.vendedor("Vende Pestex", marcas=("Pestex",))
        client = app.test_client()
        _enviar(client, quien_nombre="Pedro Ruiz", quien_telefono="8199998888")
        lead = Lead.query.one()
        C.registrar_avance(lead, "1er Contacto", vendedor.id)
        db.session.add(Sale(lead_id=lead.id, user_id=vendedor.id, unit="Pestex",
                            sale_type="suscripcion_nueva", monthly_amount=1,
                            total_amount=1, commission_amount=0))
        db.session.commit()

        _admin(client)
        client.post("/tecnicos/", data={"nombre": "Pedro Ruiz", "telefono": "8199998888"})
        db.session.refresh(lead)
        assert lead.referido_por_id is not None
        assert C.atribucion_de(lead.id)["prospectar"] == vendedor.id


def test_cobra_prospectar_aunque_el_vendedor_no_sea_multi_un(app):
    with app.app_context():
        for orden, (clave, peso) in enumerate((("prospectar", "0.15"), ("cita", "0.15"),
                                               ("cotizacion", "0.30"), ("cierre", "0.40"))):
            db.session.add(ComisionEtapa(clave=clave, nombre=clave.title(),
                                         peso=Decimal(peso), orden=orden))
        db.session.add(ComisionTasa(unidad="Pestex", monto_desde=0, tasa=Decimal("0.5")))
        db.session.commit()
        t = _tecnico()
        vendedor = f.vendedor("Vende Pestex", marcas=("Pestex",))
        assert not vendedor.perfil_multi_un
        _juan_perez()
        _enviar(app.test_client())
        lead = Lead.query.one()

        for etapa in ("1er Contacto", "Presentación", "Cotización", "Cerrado Ganado"):
            C.registrar_avance(lead, etapa, vendedor.id)
        db.session.commit()
        atrib = C.atribucion_de(lead.id)
        assert atrib["prospectar"] == t.id and atrib["cierre"] == vendedor.id

        aplica, motivo = C.aplica_tabulador(lead)
        assert aplica and "técnico" in motivo
        r = C.calcular("Pestex", 10000, 10000, atrib)
        pagos = {p["usuario_id"]: p["monto"] for p in r["participaciones"]}
        assert pagos[t.id] == Decimal("750.00")        # 10,000 × 50% × 15%
        assert pagos[vendedor.id] == Decimal("4250.00")
        assert r["cuadra"]


def test_comision_propuesta_es_lead_otorgado_y_editable():
    from blueprints.leads import ORIGEN_A_COMISION
    assert ORIGEN_A_COMISION["Referido operaciones"] == "lead_otorgado"


# ── Cliente y vendedor ───────────────────────────────────────────

def test_cliente_empata_con_cs_sin_buscador(app):
    with app.app_context():
        ejecutivo_perfil = f.vendedor("Ejecutiva", marcas=("Pestex",), en_turno=False)
        ejecutivo = f.login("Ejecutiva", usuario=ejecutivo_perfil)
        f.vendedor("Otro Pestex", marcas=("Pestex",))
        _juan_perez(ejecutivo=ejecutivo)

        r = _enviar(app.test_client(), cliente_nombre="  juan   PÉREZ ")
        assert r.status_code == 200
        lead = Lead.query.one()
        assert lead.origen == OrigenLead.REFERIDO_OPERACIONES
        assert lead.empresa_nombre == "Juan Pérez"
        assert lead.usuario_asignado_id == ejecutivo_perfil.id
        assert "Ya es cliente de Grupo Avantex: Aromatex" in lead.notas
        empresa = Account.query.one()
        assert empresa.is_cliente and lead.account_id == empresa.id


def test_un_parecido_unico_empata_y_dos_no_se_adivinan(app):
    with app.app_context():
        _juan_perez()
        assert R.cliente_cs_por_nombre("Pérez").nombre == "Juan Pérez"
        f.cuenta_cs("Pérez Hermanos")
        assert R.cliente_cs_por_nombre("Pérez") is None


def test_la_confirmacion_no_revela_el_nombre_en_cs(app):
    with app.app_context():
        _juan_perez()
        r = _enviar(app.test_client(), cliente_nombre="perez")
        assert "Juan Pérez" not in r.get_data(as_text=True)


def test_sin_ejecutivo_va_al_reparto_de_esa_unidad(app):
    with app.app_context():
        f.vendedor("Solo Aromatex", marcas=("Aromatex",))
        pestex = f.vendedor("Vende Pestex", marcas=("Pestex",))
        _juan_perez()
        _enviar(app.test_client())
        assert Lead.query.one().usuario_asignado_id == pestex.id


def test_sin_contacto_usa_el_principal_de_la_cuenta(app):
    with app.app_context():
        cuenta = _juan_perez()
        db.session.add(CSContacto(account_id=cuenta.id, nombre="Juan Pérez",
                                  telefono="8112345678", is_owner=True))
        db.session.commit()
        _enviar(app.test_client())
        assert Lead.query.one().telefono == "8112345678"


def test_si_nadie_vende_la_unidad_el_lead_se_crea_sin_asignar(app):
    with app.app_context():
        f.vendedor("Solo Aromatex", marcas=("Aromatex",))
        r = _enviar(app.test_client(), es_cliente="no", cliente_nombre="Oficinas Norte",
                    marcas=["Café Origen"])
        lead = Lead.query.one()
        assert lead.marca_interes == "Café Origen" and lead.usuario_asignado_id is None
        assert "Lo asigna dirección comercial" in r.get_data(as_text=True)


def test_prospecto_no_inventa_empresa(app):
    with app.app_context():
        _enviar(app.test_client(), es_cliente="no", cliente_nombre="Taller Sin Registro")
        assert Account.query.count() == 0
        assert "Prospecto" in Lead.query.one().notas


def test_cliente_que_no_aparece_en_cs_queda_como_cliente(app):
    with app.app_context():
        _enviar(app.test_client(), cliente_nombre="Cliente Viejo")
        empresa = Account.query.one()
        assert empresa.nombre == "Cliente Viejo" and empresa.is_cliente
        assert "no se encontró en CS" in Lead.query.one().notas


def test_si_dice_que_no_es_cliente_no_se_liga_a_cs(app):
    with app.app_context():
        cuenta = _juan_perez(kam=f.login("KAM", rol=f.RolCRM.KAM))
        cuenta.es_aaa = True
        db.session.commit()
        _enviar(app.test_client(), es_cliente="no")
        assert Account.query.count() == 0
        assert CSOpportunity.query.count() == 0


def test_mandarlo_dos_veces_no_duplica_el_lead(app):
    with app.app_context():
        f.vendedor("Vende Pestex", marcas=("Pestex",))
        _juan_perez()
        client = app.test_client()
        _enviar(client)
        r = _enviar(client, quien_nombre="Ana Torres", observacion="También en la bodega")

        assert "Ya estaba registrada" in r.get_data(as_text=True)
        lead = Lead.query.one()
        assert "cucarachas" in lead.notas and "Ana Torres" in lead.notas


def test_aviso_al_vendedor_dice_quien_lo_recomendo(app):
    with app.app_context():
        f.vendedor("Vende Pestex", marcas=("Pestex",))
        _juan_perez()
        _enviar(app.test_client())

        aviso = LeadAssignmentNotice.query.one()
        assert aviso.snapshot["referido_por"] == "Javier López"
        import lead_notifications as N
        payload = N._payload({"snapshot": aviso.snapshot, "created_at": aviso.created_at,
                              "lead_id": aviso.lead_id}, "v@pruebas.test")
        assert "Recomendado por:</strong> Javier López" in payload["html"]
        # Lo que vio y su celular se quedan en el CRM, igual que el contacto.
        assert "cucarachas" not in str(payload) and "5678" not in str(payload)


# ── Pipe de CS: solo AAA ─────────────────────────────────────────

def test_cuenta_aaa_abre_oportunidad_para_su_kam(app):
    with app.app_context():
        kam = f.login("KAM", rol=f.RolCRM.KAM)
        cuenta = _juan_perez(kam=kam)
        cuenta.es_aaa = True
        db.session.commit()
        _enviar(app.test_client())

        opp = CSOpportunity.query.one()
        assert opp.kam_id == kam.id and opp.account_id == cuenta.id
        assert opp.unidad_negocio == "Pestex" and "Javier López" in opp.descripcion


def test_cuenta_normal_no_toca_el_pipe_de_cs(app):
    with app.app_context():
        _juan_perez(kam=f.login("KAM", rol=f.RolCRM.KAM))
        _enviar(app.test_client())
        assert CSOpportunity.query.count() == 0


# ── Administración ───────────────────────────────────────────────

def test_solo_direccion_administra(app):
    with app.app_context():
        client = app.test_client()
        assert client.get("/tecnicos/").status_code in (302, 401)
        _admin(client, "Vendedor")
        assert client.get("/tecnicos/").status_code == 403
        assert client.post("/tecnicos/", data={"nombre": "X Y", "telefono": "8111111111"}).status_code == 403
        assert Usuario.query.count() == 0


def test_la_pagina_muestra_el_link_y_quien_falta_registrar(app):
    with app.app_context():
        client = app.test_client()
        _enviar(client, quien_nombre="Pedro Ruiz", quien_telefono="8199998888")
        _admin(client)
        html = client.get("/tecnicos/").get_data(as_text=True)
        assert "/recomendar/" in html
        assert "Recomendaron sin estar registrados" in html and "Pedro Ruiz" in html


def test_registro_exige_celular_y_no_lo_duplica(app):
    with app.app_context():
        client = app.test_client()
        _admin(client)
        r = client.post("/tecnicos/", data={"nombre": "Javier López", "telefono": ""})
        assert "error=" in r.headers["Location"] and Usuario.query.count() == 0

        client.post("/tecnicos/", data={"nombre": "Javier López", "telefono": CEL_JAVIER})
        r = client.post("/tecnicos/", data={"nombre": "Otro", "telefono": "+52 8112345678"})
        assert "error=" in r.headers["Location"]
        t = Usuario.query.one()
        assert t.rol_comercial == RolComercial.TECNICO and not t.en_turno
        assert t.especialidad_marca == []

        from asignacion import elegir_vendedor
        with pytest.raises(ValueError):
            elegir_vendedor("Pestex")


def test_tecnicos_no_salen_en_la_lista_de_vendedores(app):
    with app.app_context():
        _tecnico()
        f.vendedor("Vendedora")
        client = app.test_client()
        _admin(client)
        nombres = [v["nombre"] for v in client.get("/api/vendedores/").get_json()]
        assert nombres == ["Vendedora"]


# ── Café Origen ──────────────────────────────────────────────────

def test_cafe_origen_es_una_unidad_valida():
    from un_filter import es_un_valida, normalizar_marcas, normalizar_un
    assert normalizar_un("cafe origen") == "Café Origen"
    assert es_un_valida("Café Origen")
    assert normalizar_marcas(["Pestex", "CAFÉ ORIGEN"]) == ["Pestex", "Café Origen"]
