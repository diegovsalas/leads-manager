# referidos.py
"""
Recomendaciones desde campo.  FEAT-2026-10-16

Alguien de la empresa visita a un cliente (Juan Pérez, cliente de Aromatex)
y ve que necesita otro servicio (Pestex, Café Origen). No necesita usuario de
CRM: recomienda desde el link general /recomendar con su nombre y, si
quiere, su celular. Aquí se convierte en un lead normal del pipe con tres
cosas que un lead manual no trae:

  1. Quién lo recomendó (referido_nombre / referido_telefono), siempre.
  2. Si ese celular es de alguien registrado en /tecnicos, el lead queda
     ligado a esa persona (referido_por_id) y la etapa "prospectar" del
     tabulador es suya: cobra como en una venta multi-UN. Si no está
     registrado, se le acredita en cuanto dirección lo registre.
  3. El vendedor correcto: el responsable comercial de la cuenta si vende
     esa unidad, si no el reparto de siempre.

Si la cuenta es AAA, además se abre en el pipe de CS para que el KAM
intervenga. En el resto de las cuentas CS no se entera: es un trato de
ventas.
"""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_

from extensions import db
from models import (Account, CSAccount, CSContacto, CSOpportunity, EtapaPipeline,
                    Lead, LeadAtribucion, OrigenLead, RolComercial, Sale, Usuario, UserCRM)

ETAPAS_CERRADAS = (EtapaPipeline.CIERRE_GANADO, EtapaPipeline.CIERRE_PERDIDO)

# Lo que se puede recomendar desde una visita. Nexo (marketing digital) no
# se detecta en campo, así que no se ofrece.
UNIDADES_REFERIBLES = ("Aromatex", "Pestex", "Weldex", "Café Origen")


def _norm(col):
    return func.lower(func.regexp_replace(func.trim(col), r"\s+", " ", "g"))


def _clave(texto):
    return " ".join((texto or "").split()).lower()


def celular_clave(telefono):
    """Los últimos 10 dígitos: '+52 1 81 1234-5678' y '8112345678' son el
    mismo celular. None si no alcanza para identificar a nadie."""
    digitos = re.sub(r"\D", "", telefono or "")
    return digitos[-10:] if len(digitos) >= 10 else None


def tecnico_por_celular(telefono):
    clave = celular_clave(telefono)
    if not clave:
        return None
    for t in Usuario.query.filter(Usuario.rol_comercial == RolComercial.TECNICO).all():
        if celular_clave(t.telefono_whatsapp) == clave:
            return t
    return None


def cliente_cs_por_nombre(nombre):
    """La cuenta de CS que corresponde a lo que escribió quien recomienda.

    El link es abierto, así que el formulario no ofrece una lista de clientes:
    cualquiera con el link podría sacarla. Se empata aquí, sin mostrar nada:
    primero el nombre exacto (sin importar mayúsculas ni espacios) y, si no,
    un solo cliente cuyo nombre lo contenga. Con dos o más candidatos no se
    adivina; el vendedor lo resuelve con el nombre escrito.
    """
    clave = _clave(nombre)
    if len(clave) < 3:
        return None
    vigentes = CSAccount.query.filter(or_(CSAccount.en_due_diligence.is_(None),
                                          CSAccount.en_due_diligence.is_(False)))
    exacto = vigentes.filter(_norm(CSAccount.nombre) == clave).first()
    if exacto is not None or len(clave) < 4:
        return exacto
    parecidos = vigentes.filter(_norm(CSAccount.nombre).contains(clave)).limit(2).all()
    return parecidos[0] if len(parecidos) == 1 else None


def _cuenta_comercial(nombre, es_cliente):
    """La empresa del lado comercial (accounts) para ese nombre.

    cs_accounts y accounts no están ligadas; se empatan por nombre igual que
    en el buscador de upsell. Si es cliente y no existe en comercial se da de
    alta marcada como cliente: así el vendedor lo ve como cliente existente y
    no como prospecto. Un prospecto no crea empresa, para no llenar el
    catálogo con nombres sin validar.
    """
    if not nombre:
        return None
    clave = _clave(nombre)
    cuenta = Account.query.filter(or_(
        _norm(Account.nombre) == clave,
        _norm(Account.nombre_comercial) == clave,
    )).first()
    if cuenta is None and es_cliente:
        cuenta = Account(nombre=" ".join(nombre.split()), is_cliente=True)
        db.session.add(cuenta)
        db.session.flush()
    elif cuenta is not None and es_cliente and not cuenta.is_cliente:
        cuenta.is_cliente = True
    return cuenta


def _vende(usuario, marca):
    marcas = usuario.especialidad_marca or []
    return marca in marcas or "Todas" in marcas


def _vendedor_para(cs_account, cuenta, marca):
    """Quién recibe el referido, en este orden:

      1. El ejecutivo comercial de la cuenta en CS, si vende esa unidad.
         Es quien lleva precio, upsell y renovación de las cuentas grandes.
      2. El dueño de la empresa en comercial, si está en turno y la vende:
         el cliente sigue con el vendedor que ya conoce.
      3. El reparto normal por marca, zona y carga.

    None si nadie vende esa unidad: el lead queda sin asignar y visible.
    """
    if cs_account is not None and cs_account.ejecutivo_id:
        login = db.session.get(UserCRM, cs_account.ejecutivo_id)
        perfil = db.session.get(Usuario, login.usuario_id) if login and login.usuario_id else None
        if perfil is not None and _vende(perfil, marca):
            return perfil

    if cuenta is not None and cuenta.owner_id:
        dueno = db.session.get(Usuario, cuenta.owner_id)
        if dueno is not None and dueno.en_turno and _vende(dueno, marca):
            return dueno

    from asignacion import elegir_vendedor
    try:
        return elegir_vendedor(marca)
    except ValueError:
        return None


def _contacto_de(cs_account):
    """El contacto principal de la cuenta, para cuando no se capturó uno."""
    if cs_account is None:
        return None
    return (CSContacto.query.filter_by(account_id=cs_account.id)
            .order_by(CSContacto.is_owner.desc(), CSContacto.created_at)
            .first())


def _abierto_previo(cuenta, empresa, marca):
    """Un referido abierto de la misma empresa y unidad. Evita que el mismo
    hallazgo, mandado dos veces, reparta el cliente entre dos vendedores."""
    q = Lead.query.filter(
        Lead.origen == OrigenLead.REFERIDO_OPERACIONES,
        Lead.marca_interes == marca,
        Lead.etapa_pipeline.notin_(ETAPAS_CERRADAS),
    )
    if cuenta is not None:
        q = q.filter(Lead.account_id == cuenta.id)
    else:
        q = q.filter(_norm(Lead.empresa_nombre) == _clave(empresa))
    return q.order_by(Lead.fecha_creacion.desc()).first()


def _acreditar(lead, tecnico):
    """Liga el lead a quien lo recomendó y le da la etapa de prospectar.

    Manual (es_automatica=False) para que el avance del vendedor no se la
    quite: registrar_avance no pisa lo que ya existe. Una corrección que ya
    hizo dirección a mano se respeta, y una venta cerrada no se toca: su
    reparto ya quedó congelado.
    """
    lead.referido_por_id = tecnico.id
    if Sale.query.filter_by(lead_id=lead.id).first() is not None:
        return
    a = LeadAtribucion.query.filter_by(lead_id=lead.id, etapa="prospectar").first()
    if a is None:
        a = LeadAtribucion(lead_id=lead.id, etapa="prospectar")
        db.session.add(a)
    elif not a.es_automatica:
        return
    a.usuario_id = tecnico.id
    a.es_automatica = False
    a.definida_por = "Recomendación desde visita"


def acreditar_pendientes(tecnico):
    """Al registrar a alguien, sus recomendaciones anteriores pasan a ser
    suyas. Así nadie pierde la comisión por haber recomendado antes de que
    dirección lo diera de alta. No hace commit. Devuelve cuántas acreditó."""
    clave = celular_clave(tecnico.telefono_whatsapp)
    if not clave:
        return 0
    n = 0
    for lead in Lead.query.filter(Lead.origen == OrigenLead.REFERIDO_OPERACIONES,
                                  Lead.referido_por_id.is_(None),
                                  Lead.referido_telefono.isnot(None)).all():
        if celular_clave(lead.referido_telefono) == clave:
            _acreditar(lead, tecnico)
            n += 1
    return n


def registrar_referido(*, quien_nombre, quien_telefono="", marcas, observacion,
                       es_cliente, cliente_nombre, sucursal="", contacto_nombre="",
                       contacto_telefono="", contacto_email=""):
    """Da de alta el referido y hace commit. Devuelve (lead, es_nuevo).

    marcas: unidades canónicas, la primera es la dueña del trato.
    es_cliente: lo que contestó quien recomienda. Si dice que sí pero el
    nombre no empata con CS, se registra igual como cliente.
    """
    marca = marcas[0]
    cs_account = cliente_cs_por_nombre(cliente_nombre) if es_cliente else None
    empresa = cs_account.nombre if cs_account is not None else " ".join(cliente_nombre.split())
    cuenta = _cuenta_comercial(empresa, es_cliente=es_cliente)
    tecnico = tecnico_por_celular(quien_telefono)

    hoy = datetime.now(ZoneInfo("America/Monterrey")).strftime("%d/%m/%Y")
    firma = quien_nombre + (f", cel. {quien_telefono}" if quien_telefono else "")
    hallazgo = f"[{hoy}] {firma}: {observacion}"
    if sucursal:
        hallazgo += f"\nSucursal: {sucursal}"

    previo = _abierto_previo(cuenta, empresa, marca)
    if previo is not None:
        previo.notas = ((previo.notas or "") + "\n\n" + hallazgo).strip()
        db.session.commit()
        return previo, False

    contacto = _contacto_de(cs_account)
    telefono = contacto_telefono or (contacto.telefono if contacto else "") or None
    email = contacto_email or (contacto.correo if contacto else "") or None
    nombre = contacto_nombre or (contacto.nombre if contacto else "") or empresa

    notas = [f"Recomendado por {firma}.", hallazgo]
    if tecnico is None:
        notas.append("Quien lo recomendó no está registrado en Técnicos: "
                     "la etapa de prospectar se le acredita al registrarlo.")
    if cs_account is not None:
        actual = cs_account.unidades_contratadas or "sin unidades registradas"
        notas.append(f"Ya es cliente de Grupo Avantex: {actual}.")
    elif es_cliente:
        notas.append("Ya es cliente de Grupo Avantex, según quien lo recomendó "
                     "(no se encontró en CS).")
    else:
        notas.append("Prospecto: todavía no es cliente de Grupo Avantex.")

    vendedor = _vendedor_para(cs_account, cuenta, marca)
    lead = Lead(
        nombre=nombre, telefono=telefono, email=email,
        empresa_nombre=empresa,
        account_id=cuenta.id if cuenta else None,
        origen=OrigenLead.REFERIDO_OPERACIONES,
        marca_interes=marca, marcas_interes=list(marcas),
        etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        notas="\n".join(notas),
        referido_nombre=quien_nombre,
        referido_telefono=quien_telefono or None,
        usuario_asignado_id=vendedor.id if vendedor else None,
    )
    if tecnico is not None:
        lead.referido_por_id = tecnico.id
    db.session.add(lead)
    db.session.flush()
    if tecnico is not None:
        _acreditar(lead, tecnico)

    if vendedor is not None:
        vendedor.ultimo_lead_asignado = datetime.now(timezone.utc)

    # Solo las cuentas AAA entran al pipe de CS: ahí el KAM tiene que
    # intervenir. En las demás el trato es solo de ventas.
    if cs_account is not None and cs_account.es_aaa:
        quien = vendedor.nombre if vendedor else "sin vendedor asignado"
        db.session.add(CSOpportunity(
            account_id=cs_account.id, tipo="upsell_un", unidad_negocio=marca,
            descripcion=f"{hallazgo}\nLead de ventas: {quien}.",
            contacto=nombre if contacto_nombre or contacto else "",
            contacto_telefono=telefono or "", contacto_email=email or "",
            etapa="prospeccion", kam_id=cs_account.kam_id))

    db.session.commit()
    return lead, True
