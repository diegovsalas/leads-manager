"""Reparto de leads entre vendedores.

Un lead mal asignado no falla: lo recibe quien no debe, cuenta en la meta
de otro y nadie se entera. Varias de estas pruebas fijan bugs reales
encontrados el 2026-09-29 al conectar Shopify.
"""
import pytest

from asignacion import asignar_lead_comercial
from extensions import db
from models import EtapaPipeline, Lead, OrigenLead
from tests import fabricas as f


def _datos(**extra):
    base = {"telefono": "+528110000001", "nombre": "Prospecto",
            "origen": OrigenLead.WEB.value, "marca_interes": "Aromatex"}
    base.update(extra)
    return base


def test_asigna_a_un_vendedor_de_la_marca(app):
    with app.app_context():
        aromatex = f.vendedor("Ana", marcas=("Aromatex",))
        f.vendedor("Beto", marcas=("Pestex",))

        lead = asignar_lead_comercial(_datos())

        assert lead.usuario_asignado_id == aromatex.id


def test_sin_vendedores_de_esa_marca_levanta_error(app):
    with app.app_context():
        f.vendedor("Ana", marcas=("Aromatex",))

        # Falla explicito en vez de asignar a cualquiera: un lead de Nexo
        # en manos de alguien de Aromatex se atiende mal y cuenta mal.
        with pytest.raises(ValueError):
            asignar_lead_comercial(_datos(marca_interes="Nexo"))


def test_no_reparte_a_quien_no_esta_en_turno(app):
    with app.app_context():
        f.vendedor("Fuera", marcas=("Aromatex",), en_turno=False)

        with pytest.raises(ValueError):
            asignar_lead_comercial(_datos())


def test_conserva_el_email(app):
    """BUG 2026-09-29: la lista de campos era fija y no incluia email.

    Cualquier origen que lo trajera creaba el lead sin correo, y de paso
    rompia la deduplicacion, que busca por email antes de dar de alta.
    """
    with app.app_context():
        f.vendedor(marcas=("Aromatex",))

        lead = asignar_lead_comercial(_datos(email="cliente@ejemplo.com"))

        assert lead.email == "cliente@ejemplo.com"


def test_conserva_industria_y_los_ids_de_shopify(app):
    """Mismo bug que el email, con los campos que llegaron despues."""
    with app.app_context():
        f.vendedor(marcas=("Aromatex",))

        lead = asignar_lead_comercial(_datos(
            tipo_industria="Cafetería",
            shopify_customer_id="706405506930370084",
            shopify_checkout_id="9001",
            shopify_order_id="7001",
        ))

        assert lead.tipo_industria == "Cafetería"
        assert lead.shopify_customer_id == "706405506930370084"
        assert lead.shopify_checkout_id == "9001"
        assert lead.shopify_order_id == "7001"


def test_prefiere_al_vendedor_con_menos_carga(app):
    with app.app_context():
        cargada = f.vendedor("Cargada", marcas=("Aromatex",))
        libre = f.vendedor("Libre", marcas=("Aromatex",))
        for _ in range(3):
            f.lead(propietario=cargada, etapa=EtapaPipeline.NUEVO_LEAD)

        lead = asignar_lead_comercial(_datos())

        assert lead.usuario_asignado_id == libre.id
