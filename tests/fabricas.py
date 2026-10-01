"""Constructores de datos para las pruebas.

Existen para que cada prueba diga solo lo que le importa. Si una prueba
necesita "un vendedor de Aromatex", no deberia tener que saber que Usuario
pide especialidad_marca como lista ni que en_turno decide si entra al
reparto: eso es ruido que esconde lo que se esta probando.
"""
import secrets
from datetime import date, datetime, timezone

from extensions import db
from models import (Account, CSAccount, EtapaPipeline, Lead, OrigenLead,
                    RolCRM, Usuario, UserCRM)


def vendedor(nombre="Vendedora", marcas=("Aromatex",), en_turno=True):
    u = Usuario(nombre=nombre, en_turno=en_turno,
                especialidad_marca=list(marcas))
    db.session.add(u)
    db.session.commit()
    return u


def login(nombre="Admin", rol=RolCRM.SUPER_ADMIN, usuario=None):
    u = UserCRM(nombre=nombre, correo=f"{secrets.token_hex(4)}@pruebas.test",
                rol=rol, activo=True,
                usuario_id=usuario.id if usuario else None)
    u.set_password("x")          # password_hash es NOT NULL
    db.session.add(u)
    db.session.commit()
    return u


def lead(propietario=None, marca="Aromatex", valor=10000,
         etapa=EtapaPipeline.NEGOCIACION, **extra):
    l = Lead(nombre=extra.pop("nombre", "Cliente de prueba"),
             telefono=extra.pop("telefono", "+52" + secrets.token_hex(5)),
             usuario_asignado_id=propietario.id if propietario else None,
             marca_interes=marca, marcas_interes=[marca],
             origen=extra.pop("origen", OrigenLead.WEB),
             etapa_pipeline=etapa, valor_estimado=valor, **extra)
    db.session.add(l)
    db.session.commit()
    return l


def cuenta_cs(nombre="Cliente CS", mrr=0, kam=None, ejecutivo=None):
    c = CSAccount(nombre=nombre, client_id="T-" + secrets.token_hex(3),
                  mrr=mrr,
                  kam_id=kam.id if kam else None,
                  ejecutivo_id=ejecutivo.id if ejecutivo else None)
    db.session.add(c)
    db.session.commit()
    return c


def empresa(nombre="Empresa de prueba"):
    a = Account(nombre=nombre)
    db.session.add(a)
    db.session.commit()
    return a
