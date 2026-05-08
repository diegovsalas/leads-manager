# models.py
import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from extensions import db


# ──────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────
class RolComercial(enum.Enum):
    GERENTE_VENTAS   = "Gerente de Ventas"
    LIDER_COMERCIAL  = "Líder Comercial"
    ASESOR_COMERCIAL = "Asesor Comercial"
    SDR              = "SDR"


class OrigenLead(enum.Enum):
    META_ADS          = "Meta Ads"
    WHATSAPP_ORGANICO = "WhatsApp Organico"
    WEB               = "Web"
    PROSPECCION       = "Prospeccion"


class EtapaPipeline(enum.Enum):
    NUEVO_LEAD     = "Nuevo Lead"
    CONTACTO_1     = "1er Contacto"
    CONTACTO_2     = "2do Contacto"
    CONTACTO_3     = "3er Contacto"
    CONTACTO_4     = "4to Contacto"
    COTIZACION     = "Cotización"
    DEMO           = "Demo"
    NEGOCIACION    = "Negociación"
    CIERRE_GANADO  = "Cerrado Ganado"
    CIERRE_PERDIDO = "Cerrado Perdido"


class DireccionMensaje(enum.Enum):
    ENTRANTE         = "Entrante"
    SALIENTE_VENDEDOR = "Saliente_Vendedor"
    SALIENTE_BOT     = "Saliente_Bot"


class PlataformaAds(enum.Enum):
    FACEBOOK  = "Facebook"
    INSTAGRAM = "Instagram"
    GOOGLE    = "Google"
    TIKTOK    = "TikTok"
    OTRO      = "Otro"


class RolCRM(enum.Enum):
    SUPER_ADMIN = "Super Admin"
    VENDEDOR    = "Vendedor"
    KAM         = "KAM"


class TipoProyecto(enum.Enum):
    AVANCE = "avance"
    IDEA   = "idea"
    NOTA   = "nota"


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def _utcnow():
    return datetime.now(timezone.utc)


def _genuuid():
    return uuid.uuid4()


# ──────────────────────────────────────────────
# Tabla: usuarios (perfil comercial de vendedores)
# ──────────────────────────────────────────────
class Usuario(db.Model):
    __tablename__ = "usuarios"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    nombre = db.Column(db.String(150), nullable=False)
    telefono_whatsapp = db.Column(db.String(30), nullable=True)
    rol_comercial = db.Column(
        db.Enum(RolComercial, name="rol_comercial_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=False, default=RolComercial.ASESOR_COMERCIAL,
    )
    especialidad_marca = db.Column(
        ARRAY(db.String(80)), nullable=False, default=list, server_default="{}",
    )
    ultimo_lead_asignado = db.Column(db.DateTime(timezone=True), nullable=True)
    en_turno = db.Column(db.Boolean, default=True, nullable=False)
    baileys_session = db.Column(db.String(50), nullable=True)  # ej: "janeth", "azael" — para /scan/{session}
    zona_cobertura = db.Column(ARRAY(db.String(80)), nullable=False, default=list, server_default="{}")  # ej: {"Nuevo León", "Tamaulipas"}

    # Relaciones
    leads = db.relationship("Lead", back_populates="usuario_asignado", lazy="dynamic")
    estados_bot = db.relationship("EstadoBotInterno", back_populates="usuario", lazy="dynamic")
    metas = db.relationship("MetaVendedor", back_populates="usuario", lazy="dynamic")

    def __repr__(self):
        return f"<Usuario {self.nombre}>"

    def to_dict(self):
        return {
            "id": str(self.id),
            "nombre": self.nombre,
            "telefono_whatsapp": self.telefono_whatsapp,
            "rol_comercial": self.rol_comercial.value if self.rol_comercial else None,
            "especialidad_marca": self.especialidad_marca or [],
            "ultimo_lead_asignado": self.ultimo_lead_asignado.isoformat() if self.ultimo_lead_asignado else None,
            "en_turno": self.en_turno,
            "baileys_session": self.baileys_session,
            "zona_cobertura": self.zona_cobertura or [],
        }


# ──────────────────────────────────────────────
# Tabla: leads
# ──────────────────────────────────────────────
class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    telefono = db.Column(db.String(30), unique=True, nullable=False)
    nombre = db.Column(db.String(200), nullable=True)
    origen = db.Column(
        db.Enum(OrigenLead, name="origen_lead_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=True,
    )
    marca_interes = db.Column(db.String(80), nullable=True)
    etapa_pipeline = db.Column(
        db.Enum(EtapaPipeline, name="etapa_pipeline_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=False, default=EtapaPipeline.NUEVO_LEAD,
    )

    # Cotizacion
    cantidad_productos = db.Column(db.Integer, nullable=True)
    precio_unitario = db.Column(db.Numeric(14, 2), nullable=True)
    valor_estimado = db.Column(db.Numeric(14, 2), nullable=True)
    motivo_perdida = db.Column(db.Text, nullable=True)

    # Seguimiento y clasificacion
    tipo_cliente = db.Column(db.Text, nullable=True)
    fecha_ultimo_contacto = db.Column(db.DateTime(timezone=True), default=_utcnow)
    proximo_contacto = db.Column(db.DateTime(timezone=True), nullable=True)
    en_nurturing = db.Column(db.Boolean, default=False, nullable=False)
    respondio_ultimo_contacto = db.Column(db.Boolean, default=False, nullable=False)

    # ICP (Ideal Customer Profile)
    icp_score = db.Column(db.Integer, nullable=True)
    icp_nivel = db.Column(db.Text, nullable=True)
    num_sucursales = db.Column(db.Integer, nullable=True)
    tipo_industria = db.Column(db.Text, nullable=True)
    tamano_empresa = db.Column(db.Text, nullable=True)
    estado_cliente = db.Column(db.String(100), nullable=True)  # Estado normalizado (ej: "Nuevo León")
    empresa_nombre = db.Column(db.String(200), nullable=True)  # Nombre de la empresa (del bot)

    # FK → usuarios
    usuario_asignado_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=True
    )
    usuario_asignado = db.relationship("Usuario", back_populates="leads")

    # Metadatos Meta Ads
    meta_lead_id = db.Column(db.String(100), unique=True, nullable=True)
    meta_form_id = db.Column(db.String(100), nullable=True)
    meta_ad_id = db.Column(db.String(100), nullable=True)
    meta_campaign = db.Column(db.String(200), nullable=True)

    # Bot presales
    bot_step = db.Column(db.String(30), nullable=True)  # waiting_name, waiting_empresa, waiting_sucursales, waiting_servicio, transferred, None

    # Timestamps
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    fecha_actualizacion = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    # Relaciones
    mensajes = db.relationship("MensajeWhatsapp", back_populates="lead", order_by="MensajeWhatsapp.timestamp", lazy="dynamic")
    conversaciones = db.relationship("Conversacion", back_populates="lead", lazy="dynamic")
    cotizaciones_rel = db.relationship("Cotizacion", back_populates="lead", lazy="dynamic")

    def __repr__(self):
        return f"<Lead {self.nombre} | {self.telefono}>"

    @property
    def valor_calculado(self):
        if self.cantidad_productos and self.precio_unitario:
            return self.cantidad_productos * self.precio_unitario
        return self.valor_estimado

    def to_dict(self):
        vendedor = self.usuario_asignado.to_dict() if self.usuario_asignado else None
        ultimo_msg = self.mensajes.order_by(MensajeWhatsapp.timestamp.desc()).first()
        valor = self.valor_calculado
        return {
            "id": str(self.id),
            "telefono": self.telefono,
            "nombre": self.nombre,
            "origen": self.origen.value if self.origen else None,
            "marca_interes": self.marca_interes,
            "etapa_pipeline": self.etapa_pipeline.value,
            "cantidad_productos": self.cantidad_productos,
            "precio_unitario": float(self.precio_unitario) if self.precio_unitario else None,
            "valor_estimado": float(valor) if valor else None,
            "motivo_perdida": self.motivo_perdida,
            "tipo_cliente": self.tipo_cliente,
            "fecha_ultimo_contacto": self.fecha_ultimo_contacto.isoformat() if self.fecha_ultimo_contacto else None,
            "proximo_contacto": self.proximo_contacto.isoformat() if self.proximo_contacto else None,
            "en_nurturing": self.en_nurturing,
            "respondio_ultimo_contacto": self.respondio_ultimo_contacto,
            "icp_score": self.icp_score,
            "icp_nivel": self.icp_nivel,
            "num_sucursales": self.num_sucursales,
            "tipo_industria": self.tipo_industria,
            "tamano_empresa": self.tamano_empresa,
            "usuario_asignado": vendedor,
            "fecha_creacion": self.fecha_creacion.isoformat(),
            "fecha_actualizacion": self.fecha_actualizacion.isoformat(),
            "ultimo_mensaje": ultimo_msg.to_dict() if ultimo_msg else None,
        }


# ──────────────────────────────────────────────
# Tabla: mensajes_whatsapp (historial viejo)
# ──────────────────────────────────────────────
class MensajeWhatsapp(db.Model):
    __tablename__ = "mensajes_whatsapp"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    lead_id = db.Column(UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=False)
    lead = db.relationship("Lead", back_populates="mensajes")
    direccion = db.Column(
        db.Enum(DireccionMensaje, name="direccion_mensaje_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    contenido = db.Column(db.Text, nullable=False)
    meta_message_id = db.Column(db.String(100), unique=True, nullable=True)
    timestamp = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def __repr__(self):
        return f"<Mensaje [{self.direccion.value}] lead={self.lead_id}>"

    def to_dict(self):
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "direccion": self.direccion.value,
            "contenido": self.contenido,
            "meta_message_id": self.meta_message_id,
            "timestamp": self.timestamp.isoformat(),
        }


# ──────────────────────────────────────────────
# Tabla: conversaciones (nuevo historial de chat)
# ──────────────────────────────────────────────
class Conversacion(db.Model):
    __tablename__ = "conversaciones"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    lead_id = db.Column(UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=False)
    lead = db.relationship("Lead", back_populates="conversaciones")
    direccion = db.Column(db.Text, nullable=False)  # 'entrante' | 'saliente'
    mensaje = db.Column(db.Text, nullable=False)
    enviado_por = db.Column(db.Text, nullable=True)  # 'bot' | nombre del usuario
    timestamp = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "direccion": self.direccion,
            "mensaje": self.mensaje,
            "enviado_por": self.enviado_por,
            "timestamp": self.timestamp.isoformat(),
        }


# ──────────────────────────────────────────────
# Tabla: cotizaciones
# ──────────────────────────────────────────────
class Cotizacion(db.Model):
    __tablename__ = "cotizaciones"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    lead_id = db.Column(UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=False)
    lead = db.relationship("Lead", back_populates="cotizaciones_rel")
    contenido = db.Column(db.Text, nullable=False)
    generado_por = db.Column(db.Text, default="bot")
    fecha = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    enviada_whatsapp = db.Column(db.Boolean, default=False, nullable=False)

    # Campos extendidos para PDF
    folio = db.Column(db.String(20), unique=True, nullable=True)
    nombre_cliente = db.Column(db.String(200), nullable=True)
    empresa_cliente = db.Column(db.String(200), nullable=True)
    direccion_cliente = db.Column(db.Text, nullable=True)
    telefono_cliente = db.Column(db.String(30), nullable=True)
    correo_cliente = db.Column(db.String(200), nullable=True)
    marca = db.Column(db.String(80), nullable=True)
    items = db.Column(db.JSON, nullable=True, default=list)
    subtotal = db.Column(db.Numeric(14, 2), nullable=True)
    descuento_pct = db.Column(db.Numeric(5, 2), default=0)
    descuento_monto = db.Column(db.Numeric(14, 2), default=0)
    iva = db.Column(db.Numeric(14, 2), nullable=True)
    total = db.Column(db.Numeric(14, 2), nullable=True)
    condiciones_pago = db.Column(db.String(100), default="PUE")
    vigencia_dias = db.Column(db.Integer, default=15)
    vendedor_nombre = db.Column(db.String(150), nullable=True)
    enviada_correo = db.Column(db.Boolean, default=False, nullable=False)
    pdf_url = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id),
            "folio": self.folio,
            "nombre_cliente": self.nombre_cliente,
            "empresa_cliente": self.empresa_cliente,
            "direccion_cliente": self.direccion_cliente,
            "telefono_cliente": self.telefono_cliente,
            "correo_cliente": self.correo_cliente,
            "marca": self.marca,
            "items": self.items or [],
            "subtotal": float(self.subtotal) if self.subtotal else 0,
            "descuento_pct": float(self.descuento_pct) if self.descuento_pct else 0,
            "descuento_monto": float(self.descuento_monto) if self.descuento_monto else 0,
            "iva": float(self.iva) if self.iva else 0,
            "total": float(self.total) if self.total else 0,
            "condiciones_pago": self.condiciones_pago,
            "vigencia_dias": self.vigencia_dias,
            "vendedor_nombre": self.vendedor_nombre,
            "generado_por": self.generado_por,
            "fecha": self.fecha.isoformat(),
            "enviada_whatsapp": self.enviada_whatsapp,
            "enviada_correo": self.enviada_correo,
        }


# ──────────────────────────────────────────────
# Tabla: estado_bot_interno
# ──────────────────────────────────────────────
class EstadoBotInterno(db.Model):
    __tablename__ = "estado_bot_interno"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    usuario_id = db.Column(UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=False)
    usuario = db.relationship("Usuario", back_populates="estados_bot")
    lead_contexto_id = db.Column(UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=True)
    lead_contexto = db.relationship("Lead")
    esperando_input = db.Column(db.String(50), nullable=False, default="ninguno")

    def to_dict(self):
        return {
            "id": str(self.id),
            "usuario_id": str(self.usuario_id),
            "lead_contexto_id": str(self.lead_contexto_id) if self.lead_contexto_id else None,
            "esperando_input": self.esperando_input,
        }


# ──────────────────────────────────────────────
# Tabla: gastos_publicidad
# ──────────────────────────────────────────────
class GastoPublicidad(db.Model):
    __tablename__ = "gastos_publicidad"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    plataforma = db.Column(
        db.Enum(PlataformaAds, name="plataforma_ads_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    marca = db.Column(db.String(80), nullable=True)
    campana = db.Column(db.String(200), nullable=True)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    notas = db.Column(db.String(300), nullable=True)
    fecha_registro = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "plataforma": self.plataforma.value,
            "marca": self.marca,
            "campana": self.campana,
            "monto": float(self.monto),
            "fecha": self.fecha.isoformat(),
            "notas": self.notas,
        }


# ──────────────────────────────────────────────
# Tabla: users_crm (login del sistema)
# ──────────────────────────────────────────────
class UserCRM(db.Model):
    __tablename__ = "users_crm"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    nombre = db.Column(db.String(150), nullable=False)
    correo = db.Column(db.String(200), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    rol = db.Column(
        db.Enum(RolCRM, name="rol_crm_enum",
                values_callable=lambda e: [x.value for x in e]),
        nullable=False, default=RolCRM.VENDEDOR,
    )
    activo = db.Column(db.Boolean, default=True, nullable=False)
    foto_url = db.Column(db.String(500), nullable=True)
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    # FK → usuarios (vincula login con perfil comercial)
    usuario_id = db.Column(UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=True)
    usuario = db.relationship("Usuario")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": str(self.id),
            "nombre": self.nombre,
            "correo": self.correo,
            "rol": self.rol.value,
            "activo": self.activo,
            "foto_url": self.foto_url,
            "usuario_id": str(self.usuario_id) if self.usuario_id else None,
        }


# ──────────────────────────────────────────────
# Tabla: metas_vendedor
# ──────────────────────────────────────────────
class MetaVendedor(db.Model):
    __tablename__ = "metas_vendedor"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    usuario_id = db.Column(UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=False)
    usuario = db.relationship("Usuario", back_populates="metas")
    mes = db.Column(db.Text, nullable=False)  # '2026-04'
    meta_mxn = db.Column(db.Numeric(12, 2), nullable=True)
    created_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users_crm.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (db.UniqueConstraint('usuario_id', 'mes'),)

    def to_dict(self):
        return {
            "id": str(self.id),
            "usuario_id": str(self.usuario_id),
            "mes": self.mes,
            "meta_mxn": float(self.meta_mxn) if self.meta_mxn else None,
            "created_by": str(self.created_by) if self.created_by else None,
            "created_at": self.created_at.isoformat(),
        }


# ──────────────────────────────────────────────
# Tabla: proyecto_items (gestión colaborativa)
# ──────────────────────────────────────────────
class ProyectoItem(db.Model):
    __tablename__ = "proyecto_items"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    tipo = db.Column(db.String(20), nullable=False)
    titulo = db.Column(db.String(300), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    autor = db.Column(db.String(150), nullable=False)
    prioridad = db.Column(db.String(50), nullable=True)
    votos = db.Column(db.Integer, nullable=False, default=0)
    prompt_dev = db.Column(db.Text, nullable=True)
    completado = db.Column(db.Boolean, nullable=False, default=False)
    parent_id = db.Column(UUID(as_uuid=True), nullable=True)
    fase_num = db.Column(db.Integer, nullable=True)
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "tipo": self.tipo,
            "titulo": self.titulo,
            "descripcion": self.descripcion,
            "autor": self.autor,
            "prioridad": self.prioridad,
            "votos": self.votos,
            "prompt_dev": self.prompt_dev,
            "completado": self.completado,
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "fase_num": self.fase_num,
            "fecha_creacion": self.fecha_creacion.isoformat(),
        }


# ──────────────────────────────────────────────
# Tabla: api_keys (acceso externo controlado)
# ──────────────────────────────────────────────
class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    nombre = db.Column(db.String(100), nullable=False)
    api_key = db.Column(db.String(64), unique=True, nullable=False)
    permisos = db.Column(db.JSON, nullable=False, default=lambda: ["leads:read", "leads:write"])
    activo = db.Column(db.Boolean, default=True, nullable=False)
    creado_por = db.Column(UUID(as_uuid=True), db.ForeignKey("users_crm.id"), nullable=True)
    ultimo_uso = db.Column(db.DateTime(timezone=True), nullable=True)
    usos = db.Column(db.Integer, default=0, nullable=False)
    fecha_creacion = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "nombre": self.nombre,
            "api_key": self.api_key[:8] + "..." + self.api_key[-4:],
            "permisos": self.permisos,
            "activo": self.activo,
            "ultimo_uso": self.ultimo_uso.isoformat() if self.ultimo_uso else None,
            "usos": self.usos,
            "fecha_creacion": self.fecha_creacion.isoformat(),
        }

    def to_dict_full(self):
        """Solo se usa al crear — muestra la key completa una vez."""
        d = self.to_dict()
        d["api_key"] = self.api_key
        return d


# ──────────────────────────────────────────────
# Tabla: actividad_log (auditoría)
# ──────────────────────────────────────────────
# ──────────────────────────────────────────────
# Tablas: CS Dashboard (Customer Success)
# ──────────────────────────────────────────────
class CSAccount(db.Model):
    """Cuenta (cliente) bajo gestión de Customer Success."""
    __tablename__ = "cs_accounts"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    client_id = db.Column(db.String(10), unique=True, nullable=True)  # AX-0001, AX-0002, ...
    nombre = db.Column(db.String(200), nullable=False, unique=True)
    kam_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users_crm.id"), nullable=False)
    es_cuenta_nueva = db.Column(db.Boolean, default=False)
    mrr = db.Column(db.Numeric(14, 2), default=0)
    arr_proyectado = db.Column(db.Numeric(14, 2), default=0)
    sucursales = db.Column(db.Integer, default=0)
    unidades_contratadas = db.Column(db.String(100), default="")
    facturacion_q1 = db.Column(db.Numeric(14, 2), default=0)
    pagado_q1 = db.Column(db.Numeric(14, 2), default=0)
    pendiente_q1 = db.Column(db.Numeric(14, 2), default=0)
    num_facturas_q1 = db.Column(db.Integer, default=0)
    logo_url = db.Column(db.Text, default="")
    survey_token = db.Column(db.String(32))
    giro = db.Column(db.String(100), default="")
    tier = db.Column(db.String(20), default="")  # Gold, Silver, Bronze
    adjuntos = db.Column(db.JSON, default=list)  # [{nombre, url, tipo}]
    nps = db.Column(db.Float, nullable=True)
    pulso = db.Column(db.String(20), nullable=True)
    eficiencia_operativa = db.Column(db.Float, nullable=True)

    kam = db.relationship("UserCRM", backref="cs_accounts")
    invoices = db.relationship("CSInvoice", backref="account", lazy=True)
    appointments = db.relationship("CSAppointment", backref="account", lazy=True)
    notes = db.relationship("CSNote", backref="account", lazy=True, order_by="CSNote.created_at.desc()")
    tasks = db.relationship("CSTask", backref="account", lazy=True, order_by="CSTask.created_at.desc()")
    contactos = db.relationship("CSContacto", backref="account", lazy=True, order_by="CSContacto.is_owner.desc()")
    entregables = db.relationship("CSEntregable", backref="account", lazy=True, order_by="CSEntregable.orden")

    def to_dict(self):
        return {
            "id": str(self.id), "client_id": self.client_id or "",
            "nombre": self.nombre,
            "kam_id": str(self.kam_id), "mrr": float(self.mrr or 0),
            "sucursales": self.sucursales,
            "unidades_contratadas": self.unidades_contratadas,
        }


@db.event.listens_for(CSAccount, "before_insert")
def _auto_client_id(mapper, connection, target):
    """Auto-asigna client_id secuencial si no se proporcionó."""
    if not target.client_id:
        result = connection.execute(
            db.text("SELECT MAX(client_id) FROM cs_accounts WHERE client_id IS NOT NULL")
        ).scalar()
        if result:
            num = int(result.split("-")[1]) + 1
        else:
            num = 1
        target.client_id = f"AX-{num:04d}"


class CSInvoice(db.Model):
    __tablename__ = "cs_invoices"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    folio = db.Column(db.String(50), default="")
    serie = db.Column(db.String(20), default="")
    concepto = db.Column(db.String(300), default="")
    uen = db.Column(db.String(50), default="")
    subtotal = db.Column(db.Numeric(14, 2), default=0)
    impuestos = db.Column(db.Numeric(14, 2), default=0)
    total = db.Column(db.Numeric(14, 2), default=0)
    pendiente = db.Column(db.Numeric(14, 2), default=0)
    pagado = db.Column(db.Numeric(14, 2), default=0)
    fecha_cobro = db.Column(db.Date, nullable=True)
    fecha_vencimiento = db.Column(db.Date, nullable=True)
    fecha_pago = db.Column(db.Date, nullable=True)
    estatus = db.Column(db.String(30), default="")


class CSAppointment(db.Model):
    __tablename__ = "cs_appointments"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    propiedad = db.Column(db.String(300), default="")
    direccion = db.Column(db.String(500), default="")
    zona = db.Column(db.String(100), default="")
    tecnico = db.Column(db.String(120), default="")
    fecha_inicio = db.Column(db.DateTime(timezone=True), nullable=True)
    fecha_terminacion = db.Column(db.DateTime(timezone=True), nullable=True)
    estatus = db.Column(db.String(50), default="")
    titulo_servicio = db.Column(db.String(200), default="")
    cantidad = db.Column(db.Integer, default=1)


class CSNote(db.Model):
    __tablename__ = "cs_notes"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    autor = db.Column(db.String(120), default="")
    contenido = db.Column(db.Text, nullable=False)
    adjuntos = db.Column(db.JSON, default=list)  # [{nombre, url, tipo}]
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class CSTask(db.Model):
    __tablename__ = "cs_tasks"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    tipo = db.Column(db.String(50), default="check-in")
    descripcion = db.Column(db.Text, nullable=False)
    responsable = db.Column(db.String(120), default="")
    fecha_limite = db.Column(db.Date, nullable=True)
    completada = db.Column(db.Boolean, default=False)
    adjuntos = db.Column(db.JSON, default=list)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class CSContacto(db.Model):
    """Contacto de un cliente (cuenta CS)."""
    __tablename__ = "cs_contactos"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    puesto = db.Column(db.String(200), default="")
    telefono = db.Column(db.String(30), default="")
    correo = db.Column(db.String(200), default="")
    is_owner = db.Column(db.Boolean, default=False)
    notas = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    def to_dict(self):
        return {
            "id": str(self.id), "account_id": str(self.account_id),
            "nombre": self.nombre, "puesto": self.puesto,
            "telefono": self.telefono, "correo": self.correo,
            "is_owner": self.is_owner, "notas": self.notas,
            "cuenta": self.account.nombre if self.account else "",
        }


class CSEncuesta(db.Model):
    """Respuesta de encuesta NPS+CSAT por cuenta."""
    __tablename__ = "cs_encuestas"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    token = db.Column(db.String(32), nullable=False)
    nombre_respondente = db.Column(db.String(200), default="")
    puesto_respondente = db.Column(db.String(200), default="")
    nps = db.Column(db.Integer)  # 0-10
    csat = db.Column(db.Integer)  # 1-5 Satisfacción general
    csat_calidad = db.Column(db.Integer)  # 1-5 Calidad del servicio
    csat_respuesta = db.Column(db.Integer)  # 1-5 Tiempo de respuesta
    csat_comunicacion = db.Column(db.Integer)  # 1-5 Comunicación con asesor
    csat_precio = db.Column(db.Integer)  # 1-5 Relación calidad-precio
    csat_tecnico = db.Column(db.Integer)  # 1-5 Equipo técnico
    comentario = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    @property
    def csat_promedio(self):
        """Promedio de las 6 dimensiones CSAT."""
        vals = [v for v in [self.csat, self.csat_calidad, self.csat_respuesta,
                            self.csat_comunicacion, self.csat_precio, self.csat_tecnico] if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None


class CSPropiedad(db.Model):
    """Propiedad/sucursal de un cliente."""
    __tablename__ = "cs_propiedades"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    nombre = db.Column(db.String(300), nullable=False)
    direccion = db.Column(db.String(500), default="")
    zona = db.Column(db.String(100), default="")
    unidad_negocio = db.Column(db.String(30), default="")
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class CSIncidencia(db.Model):
    """Incidencia de servicio reportada por KAM."""
    __tablename__ = "cs_incidencias"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    propiedad_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_propiedades.id"), nullable=True)
    propiedad_nombre = db.Column(db.String(300), default="")
    servicio = db.Column(db.String(30), nullable=False, default="Aroma")  # Aroma / Fumigación
    tipo = db.Column(db.String(100), nullable=False)
    detalle = db.Column(db.Text, default="")
    status = db.Column(db.String(30), default="Abierta")  # Abierta / En proceso / Resuelta
    zona = db.Column(db.String(100), default="")
    quien_reporta = db.Column(db.String(200), default="")
    contacto_cliente = db.Column(db.String(200), default="")
    responsable = db.Column(db.String(200), default="")
    fecha_incidencia = db.Column(db.Date, nullable=True)
    fecha_compromiso = db.Column(db.Date, nullable=True)
    fecha_solucion = db.Column(db.Date, nullable=True)
    comentarios_operaciones = db.Column(db.Text, default="")
    evidencia = db.Column(db.Text, default="")
    tiempo_respuesta = db.Column(db.Integer, nullable=True)
    created_by = db.Column(db.String(200), default="")
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)

    account = db.relationship("CSAccount", backref="incidencias")
    propiedad = db.relationship("CSPropiedad")


class CSEntregable(db.Model):
    """Entregable/flujo de servicio recurrente por cuenta."""
    __tablename__ = "cs_entregables"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=False)
    unidad_negocio = db.Column(db.String(30), default="")
    descripcion = db.Column(db.Text, nullable=False)
    fecha_entrega = db.Column(db.String(100), default="")  # "1 al 5 de cada mes"
    responsable = db.Column(db.String(120), default="")
    orden = db.Column(db.Integer, default=0)
    adjuntos = db.Column(db.JSON, default=list)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)


class CSOnboardingAccount(db.Model):
    __tablename__ = "cs_onboarding_accounts"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    nombre = db.Column(db.String(200), nullable=False)
    sucursales = db.Column(db.Integer, default=0)
    tarifa = db.Column(db.Numeric(14, 2), default=0)
    frecuencia = db.Column(db.String(30), default="mensual")
    mrr_proyectado = db.Column(db.Numeric(14, 2), default=0)
    kam_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users_crm.id"), nullable=True)
    kam = db.relationship("UserCRM", foreign_keys=[kam_id])


class CSOpportunity(db.Model):
    __tablename__ = "cs_opportunities"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("cs_accounts.id"), nullable=True)
    prospecto_nombre = db.Column(db.String(200), default="")
    contacto = db.Column(db.String(200), default="")
    tipo = db.Column(db.String(50), nullable=False)
    unidad_negocio = db.Column(db.String(30), default="")
    descripcion = db.Column(db.Text, default="")
    valor_estimado = db.Column(db.Numeric(14, 2), default=0)
    etapa = db.Column(db.String(30), default="prospeccion")
    kam_id = db.Column(UUID(as_uuid=True), db.ForeignKey("users_crm.id"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow)
    account = db.relationship("CSAccount", backref="opportunities")
    kam = db.relationship("UserCRM", foreign_keys=[kam_id])

    @property
    def cliente_nombre(self):
        if self.account:
            return self.account.nombre
        return self.prospecto_nombre or "Sin nombre"


# ──────────────────────────────────────────────
# Tabla: actividad_log (auditoría)
# ──────────────────────────────────────────────
class ActividadLog(db.Model):
    __tablename__ = "actividad_log"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    usuario_nombre = db.Column(db.String(150), nullable=True)
    usuario_id = db.Column(UUID(as_uuid=True), nullable=True)
    accion = db.Column(db.String(50), nullable=False)
    entidad = db.Column(db.String(50), nullable=False)
    entidad_id = db.Column(UUID(as_uuid=True), nullable=True)
    detalle = db.Column(db.Text, nullable=True)
    fecha = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "usuario_nombre": self.usuario_nombre,
            "accion": self.accion,
            "entidad": self.entidad,
            "entidad_id": str(self.entidad_id) if self.entidad_id else None,
            "detalle": self.detalle,
            "fecha": self.fecha.isoformat(),
        }


# ──────────────────────────────────────────────
# SAVIO — Sincronización de cobranza y suscripciones
# Solo lectura desde la API de Savio. Nunca escribimos a Savio.
# Fuente de verdad para MRR del grupo.
# ──────────────────────────────────────────────

class SavioCustomer(db.Model):
    __tablename__ = "savio_customers"

    customer_id = db.Column(db.String(64), primary_key=True)  # ID nativo de Savio
    name = db.Column(db.String(255), nullable=True)
    legal_name = db.Column(db.String(255), nullable=True)
    tax_id = db.Column(db.String(20), nullable=True, index=True)  # RFC
    city = db.Column(db.String(120), nullable=True)
    state = db.Column(db.String(120), nullable=True)
    current_state = db.Column(db.String(60), nullable=True)  # active, paused, cancelled
    unit = db.Column(db.String(40), nullable=True)  # aromatex, pestex, weldex, weldu
    raw_data = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "customer_id": self.customer_id,
            "name": self.name,
            "legal_name": self.legal_name,
            "tax_id": self.tax_id,
            "city": self.city,
            "state": self.state,
            "current_state": self.current_state,
            "unit": self.unit,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SavioSubscription(db.Model):
    __tablename__ = "savio_subscriptions"

    id = db.Column(db.String(64), primary_key=True)  # ID nativo de Savio
    customer_id = db.Column(db.String(64), db.ForeignKey("savio_customers.customer_id"), nullable=True, index=True)
    description = db.Column(db.Text, nullable=True)
    mrr = db.Column(db.Numeric(14, 2), nullable=True)  # PRE-IVA. Fuente de verdad para MRR.
    amount = db.Column(db.Numeric(14, 2), nullable=True)
    status = db.Column(db.String(40), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    contract_end_date = db.Column(db.Date, nullable=True)
    uen = db.Column(db.String(120), nullable=True)  # UEN crudo de Savio (ej. "AROMATEX RECURRENTE")
    unit = db.Column(db.String(40), nullable=True, index=True)  # clasificado: aromatex/pestex/weldex/weldu
    type = db.Column(db.String(40), nullable=True)  # recurrente, eventual, poliza, refacturacion
    sum_mrr = db.Column(db.Boolean, default=False, nullable=False)  # ¿este registro suma al MRR?
    raw_data = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    customer = db.relationship("SavioCustomer", backref="subscriptions", lazy="joined")

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "description": self.description,
            "mrr": float(self.mrr) if self.mrr else 0,
            "amount": float(self.amount) if self.amount else 0,
            "status": self.status,
            "start_date": self.start_date.isoformat() if self.start_date else None,
            "contract_end_date": self.contract_end_date.isoformat() if self.contract_end_date else None,
            "uen": self.uen,
            "unit": self.unit,
            "type": self.type,
            "sum_mrr": self.sum_mrr,
        }


class SavioInvoice(db.Model):
    __tablename__ = "savio_invoices"

    id = db.Column(db.String(64), primary_key=True)
    customer_id = db.Column(db.String(64), db.ForeignKey("savio_customers.customer_id"), nullable=True, index=True)
    customer_name = db.Column(db.String(255), nullable=True)
    invoice_number = db.Column(db.String(80), nullable=True)
    amount = db.Column(db.Numeric(14, 2), nullable=True)  # CON IVA. NO usar para MRR.
    status = db.Column(db.String(40), nullable=True)
    date = db.Column(db.Date, nullable=True, index=True)
    uen = db.Column(db.String(120), nullable=True)
    unit = db.Column(db.String(40), nullable=True, index=True)
    type = db.Column(db.String(40), nullable=True)
    sum_mrr = db.Column(db.Boolean, default=False, nullable=False)
    sub = db.Column(db.String(40), nullable=True)  # intendencia / weldex_recurrente / weldex_eventual
    description = db.Column(db.Text, nullable=True)
    raw_data = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "customer_name": self.customer_name,
            "invoice_number": self.invoice_number,
            "amount": float(self.amount) if self.amount else 0,
            "status": self.status,
            "date": self.date.isoformat() if self.date else None,
            "uen": self.uen,
            "unit": self.unit,
            "type": self.type,
            "sub": self.sub,
        }


class SavioPayment(db.Model):
    __tablename__ = "savio_payments"

    id = db.Column(db.String(64), primary_key=True)
    invoice_id = db.Column(db.String(64), db.ForeignKey("savio_invoices.id"), nullable=True, index=True)
    customer_id = db.Column(db.String(64), db.ForeignKey("savio_customers.customer_id"), nullable=True, index=True)
    amount = db.Column(db.Numeric(14, 2), nullable=True)
    date = db.Column(db.Date, nullable=True, index=True)
    method = db.Column(db.String(60), nullable=True)
    raw_data = db.Column(db.JSON, nullable=True)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "invoice_id": self.invoice_id,
            "customer_id": self.customer_id,
            "amount": float(self.amount) if self.amount else 0,
            "date": self.date.isoformat() if self.date else None,
            "method": self.method,
        }


# ──────────────────────────────────────────────
# CUSTOMER MASTER — agrupa múltiples RFCs/customers (Savio + Zoho) bajo una
# misma entidad comercial. Foundation para el bridge Savio→CSAccount.
# ──────────────────────────────────────────────

class CustomerMaster(db.Model):
    __tablename__ = "customer_master"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    master_name = db.Column(db.String(255), nullable=True)
    zoho_account_id = db.Column(db.String(64), nullable=True)
    savio_customer_ids = db.Column(db.Text, nullable=True)  # CSV de IDs (legacy compat)
    cs_account_id = db.Column(UUID(as_uuid=True), nullable=True)  # link al sistema CS local
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    rfcs = db.relationship("CustomerRfc", backref="master", lazy="joined", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "master_name": self.master_name,
            "zoho_account_id": self.zoho_account_id,
            "savio_customer_ids": self.savio_customer_ids,
            "cs_account_id": str(self.cs_account_id) if self.cs_account_id else None,
            "rfcs": [r.to_dict() for r in self.rfcs],
        }


class CustomerRfc(db.Model):
    __tablename__ = "customer_rfcs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    master_id = db.Column(db.Integer, db.ForeignKey("customer_master.id", ondelete="CASCADE"), nullable=False, index=True)
    rfc = db.Column(db.String(20), unique=True, nullable=False, index=True)
    legal_name = db.Column(db.String(255), nullable=True)
    savio_customer_id = db.Column(db.String(64), nullable=True, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "master_id": self.master_id,
            "rfc": self.rfc,
            "legal_name": self.legal_name,
            "savio_customer_id": self.savio_customer_id,
        }


# ──────────────────────────────────────────────
# SDR — Sales Development Representative
# Port directo de vendedores.cloud (sdr*.js + sdr_*/sdr_dir_* tables).
# Dos sub-sistemas: SDR clásico (sdr_results) y SDR Directivo (sdr_dir_*).
# ──────────────────────────────────────────────


class SdrResult(db.Model):
    """Resultados scrapeados de Meta Ads / Google Maps. Cola de leads
    pre-asignación a vendedor. Status: nuevo/asignado/descartado/corporativo."""
    __tablename__ = "sdr_results"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_name = db.Column(db.Text, nullable=False)
    instagram_handle = db.Column(db.Text, default="", nullable=True)
    whatsapp = db.Column(db.Text, default="", nullable=True)
    address = db.Column(db.Text, default="", nullable=True)
    state = db.Column(db.String(120), default="", nullable=True)
    city = db.Column(db.String(120), default="", nullable=True)
    rating = db.Column(db.Float, nullable=True)
    reviews = db.Column(db.Integer, default=0, nullable=False)
    branches = db.Column(db.Integer, default=1, nullable=False)
    source = db.Column(db.String(60), default="", nullable=True)
    unit = db.Column(db.String(40), nullable=False, index=True)
    status = db.Column(db.String(40), default="nuevo", nullable=False, index=True)  # nuevo/asignado/descartado/corporativo
    assigned_to = db.Column(db.Integer, nullable=True, index=True)  # ref to legacy users.id (int) — no FK to leads-manager Usuario (UUID)
    assigned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    meta_ad_url = db.Column(db.Text, default="", nullable=True)
    facebook_url = db.Column(db.Text, default="", nullable=True)
    website = db.Column(db.Text, default="", nullable=True)
    maps_url = db.Column(db.Text, default="", nullable=True)
    wa_source = db.Column(db.Text, default="", nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id, "business_name": self.business_name,
            "instagram_handle": self.instagram_handle, "whatsapp": self.whatsapp,
            "address": self.address, "state": self.state, "city": self.city,
            "rating": self.rating, "reviews": self.reviews, "branches": self.branches,
            "source": self.source, "unit": self.unit, "status": self.status,
            "assigned_to": self.assigned_to,
            "assigned_at": self.assigned_at.isoformat() if self.assigned_at else None,
            "meta_ad_url": self.meta_ad_url, "facebook_url": self.facebook_url,
            "website": self.website, "maps_url": self.maps_url, "wa_source": self.wa_source,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SdrDirMasterCompany(db.Model):
    """Lista maestra de target accounts del SDR Directivo. El engine las procesa
    en priority_order. Apollo query es la base de búsqueda; Lusha enriquece
    contactos. tam = A/B/C (TAM tier)."""
    __tablename__ = "sdr_dir_master_companies"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    priority_order = db.Column(db.Integer, nullable=False)
    company_name = db.Column(db.Text, nullable=False)
    apollo_query = db.Column(db.Text, nullable=False)
    sector = db.Column(db.Text, nullable=False)
    tam = db.Column(db.String(10), nullable=True)  # A / B / C
    origen = db.Column(db.String(60), nullable=True)
    sucursales = db.Column(db.Integer, nullable=True)
    estados = db.Column(db.Text, nullable=True)  # CSV de estados
    seniorities = db.Column(db.Text, nullable=True)  # CSV
    departments = db.Column(db.Text, nullable=True)  # CSV
    priority_titles = db.Column(db.Text, nullable=True)  # CSV
    requires_manual = db.Column(db.Boolean, default=False, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    unit = db.Column(db.String(40), default="aromatex", nullable=False, index=True)
    status = db.Column(db.String(40), default="pending", nullable=False, index=True)
    contacts_found = db.Column(db.Integer, default=0, nullable=False)
    lusha_credits_used = db.Column(db.Integer, default=0, nullable=False)
    last_attempt_at = db.Column(db.DateTime(timezone=True), nullable=True)
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    skip_reason = db.Column(db.Text, nullable=True)
    apollo_alt_queries = db.Column(db.Text, nullable=True)
    apollo_industry = db.Column(db.Text, nullable=True)
    country = db.Column(db.String(40), default="Mexico", nullable=True)
    exclude_keywords = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        db.Index("idx_sdr_master_status_priority", "status", "priority_order"),
        db.Index("idx_sdr_master_unit_status", "unit", "status"),
    )

    def to_dict(self):
        return {
            "id": self.id, "priority_order": self.priority_order,
            "company_name": self.company_name, "apollo_query": self.apollo_query,
            "sector": self.sector, "tam": self.tam, "origen": self.origen,
            "sucursales": self.sucursales, "estados": self.estados,
            "seniorities": self.seniorities, "departments": self.departments,
            "priority_titles": self.priority_titles,
            "requires_manual": self.requires_manual, "notes": self.notes,
            "unit": self.unit, "status": self.status,
            "contacts_found": self.contacts_found,
            "lusha_credits_used": self.lusha_credits_used,
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "processed_at": self.processed_at.isoformat() if self.processed_at else None,
            "skip_reason": self.skip_reason,
            "apollo_alt_queries": self.apollo_alt_queries,
            "apollo_industry": self.apollo_industry, "country": self.country,
            "exclude_keywords": self.exclude_keywords,
        }


class SdrDirSuggestion(db.Model):
    """Sugerencias de empresas/contactos al directivo (pre-secuencia)."""
    __tablename__ = "sdr_dir_suggestions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    company_name = db.Column(db.Text, nullable=True)
    company_domain = db.Column(db.Text, nullable=True)
    company_industry = db.Column(db.Text, nullable=True)
    company_size = db.Column(db.String(40), nullable=True)
    company_country = db.Column(db.String(40), default="Mexico", nullable=True)
    unit = db.Column(db.String(40), nullable=True, index=True)
    suggested_to = db.Column(db.Integer, nullable=True, index=True)  # legacy users.id
    suggested_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    status = db.Column(db.String(40), default="pendiente", nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id, "company_name": self.company_name,
            "company_domain": self.company_domain,
            "company_industry": self.company_industry,
            "company_size": self.company_size,
            "company_country": self.company_country, "unit": self.unit,
            "suggested_to": self.suggested_to,
            "suggested_at": self.suggested_at.isoformat() if self.suggested_at else None,
            "status": self.status,
        }


class SdrDirSequence(db.Model):
    """Secuencia de cold outreach activa. 1 contacto = 1 secuencia.
    Sincronizada con Lemlist (lemlist_campaign_id, lemlist_lead_id)."""
    __tablename__ = "sdr_dir_sequences"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    company_name = db.Column(db.Text, nullable=True, index=True)
    company_domain = db.Column(db.Text, nullable=True)
    contact_name = db.Column(db.Text, nullable=True)
    contact_title = db.Column(db.Text, nullable=True)
    contact_email = db.Column(db.Text, nullable=True, index=True)
    contact_phone = db.Column(db.Text, nullable=True)
    contact_linkedin = db.Column(db.Text, nullable=True)
    whatsapp_verified = db.Column(db.Boolean, default=False, nullable=False)
    whatsapp_link = db.Column(db.Text, nullable=True)
    unit = db.Column(db.String(40), nullable=True, index=True)
    assigned_to = db.Column(db.Integer, nullable=True, index=True)
    status = db.Column(db.String(40), default="activa", nullable=False, index=True)
    current_step = db.Column(db.Integer, default=0, nullable=False)
    first_channel = db.Column(db.String(40), default="email", nullable=False)
    lemlist_campaign_id = db.Column(db.Text, nullable=True)
    lemlist_lead_id = db.Column(db.Text, nullable=True)
    last_action_at = db.Column(db.DateTime(timezone=True), nullable=True)
    next_action_at = db.Column(db.DateTime(timezone=True), nullable=True)
    paused_reason = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    lead_state = db.Column(db.String(60), default="sin_respuesta", nullable=False, index=True)
    state_reason = db.Column(db.Text, nullable=True)
    state_changed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    master_company_id = db.Column(db.Integer, db.ForeignKey("sdr_dir_master_companies.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    history = db.relationship("SdrDirHistory", backref="sequence", lazy="dynamic", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id, "company_name": self.company_name,
            "company_domain": self.company_domain,
            "contact_name": self.contact_name, "contact_title": self.contact_title,
            "contact_email": self.contact_email, "contact_phone": self.contact_phone,
            "contact_linkedin": self.contact_linkedin,
            "whatsapp_verified": self.whatsapp_verified,
            "whatsapp_link": self.whatsapp_link,
            "unit": self.unit, "assigned_to": self.assigned_to,
            "status": self.status, "current_step": self.current_step,
            "first_channel": self.first_channel,
            "lemlist_campaign_id": self.lemlist_campaign_id,
            "lemlist_lead_id": self.lemlist_lead_id,
            "last_action_at": self.last_action_at.isoformat() if self.last_action_at else None,
            "next_action_at": self.next_action_at.isoformat() if self.next_action_at else None,
            "paused_reason": self.paused_reason, "notes": self.notes,
            "lead_state": self.lead_state, "state_reason": self.state_reason,
            "state_changed_at": self.state_changed_at.isoformat() if self.state_changed_at else None,
            "master_company_id": self.master_company_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SdrDirHistory(db.Model):
    """Per-step history de cada secuencia (envíos)."""
    __tablename__ = "sdr_dir_history"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    sequence_id = db.Column(db.Integer, db.ForeignKey("sdr_dir_sequences.id", ondelete="CASCADE"), nullable=True, index=True)
    step_number = db.Column(db.Integer, nullable=True)
    channel = db.Column(db.String(40), nullable=True)
    message_preview = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(40), default="enviado", nullable=False)
    sent_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id, "sequence_id": self.sequence_id,
            "step_number": self.step_number, "channel": self.channel,
            "message_preview": self.message_preview, "status": self.status,
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
        }


class SdrDirEngineConfig(db.Model):
    """Config del engine SDR por unidad de negocio. PK = unit."""
    __tablename__ = "sdr_dir_engine_config"

    unit = db.Column(db.String(40), primary_key=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    max_companies_per_day = db.Column(db.Integer, default=10, nullable=False)
    max_contacts_per_company = db.Column(db.Integer, default=2, nullable=False)
    max_lusha_credits_per_day = db.Column(db.Integer, default=25, nullable=False)
    min_lusha_balance_alert = db.Column(db.Integer, default=50, nullable=False)
    lemlist_master_campaign_id = db.Column(db.Text, nullable=True)
    cron_hour = db.Column(db.Integer, default=9, nullable=False)
    cron_minute = db.Column(db.Integer, default=0, nullable=False)
    last_run_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_run_summary = db.Column(db.Text, nullable=True)
    # TAM-aware enrichment policies
    tam_a_enrich_phone = db.Column(db.Boolean, default=True, nullable=False)
    tam_bc_enrich_phone = db.Column(db.Boolean, default=False, nullable=False)
    tam_a_phones_per_company = db.Column(db.Integer, default=2, nullable=False)
    tam_bc_phones_per_company = db.Column(db.Integer, default=0, nullable=False)
    # Lusha credit budget
    lusha_monthly_limit = db.Column(db.Integer, default=600, nullable=False)
    lusha_hard_cap = db.Column(db.Boolean, default=True, nullable=False)
    lusha_alert_threshold = db.Column(db.Float, default=0.8, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "unit": self.unit, "enabled": self.enabled,
            "max_companies_per_day": self.max_companies_per_day,
            "max_contacts_per_company": self.max_contacts_per_company,
            "max_lusha_credits_per_day": self.max_lusha_credits_per_day,
            "min_lusha_balance_alert": self.min_lusha_balance_alert,
            "lemlist_master_campaign_id": self.lemlist_master_campaign_id,
            "cron_hour": self.cron_hour, "cron_minute": self.cron_minute,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_run_summary": self.last_run_summary,
            "tam_a_enrich_phone": self.tam_a_enrich_phone,
            "tam_bc_enrich_phone": self.tam_bc_enrich_phone,
            "tam_a_phones_per_company": self.tam_a_phones_per_company,
            "tam_bc_phones_per_company": self.tam_bc_phones_per_company,
            "lusha_monthly_limit": self.lusha_monthly_limit,
            "lusha_hard_cap": self.lusha_hard_cap,
            "lusha_alert_threshold": self.lusha_alert_threshold,
        }


class SdrDirEngineRun(db.Model):
    """Una fila por corrida del engine. Trackea resultados y errores."""
    __tablename__ = "sdr_dir_engine_runs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    unit = db.Column(db.String(40), nullable=False, index=True)
    started_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(40), nullable=True)
    companies_attempted = db.Column(db.Integer, default=0, nullable=False)
    companies_processed = db.Column(db.Integer, default=0, nullable=False)
    companies_no_contacts = db.Column(db.Integer, default=0, nullable=False)
    contacts_pushed_to_lemlist = db.Column(db.Integer, default=0, nullable=False)
    lusha_credits_used = db.Column(db.Integer, default=0, nullable=False)
    apollo_calls = db.Column(db.Integer, default=0, nullable=False)
    total_cost_usd = db.Column(db.Float, default=0, nullable=False)
    error_log = db.Column(db.Text, nullable=True)
    details_json = db.Column(db.JSON, nullable=True)

    def to_dict(self):
        return {
            "id": self.id, "unit": self.unit,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "companies_attempted": self.companies_attempted,
            "companies_processed": self.companies_processed,
            "companies_no_contacts": self.companies_no_contacts,
            "contacts_pushed_to_lemlist": self.contacts_pushed_to_lemlist,
            "lusha_credits_used": self.lusha_credits_used,
            "apollo_calls": self.apollo_calls,
            "total_cost_usd": self.total_cost_usd,
            "error_log": self.error_log,
        }


class ApiCost(db.Model):
    """Una fila por llamada a API externa con costo. Power para reportes
    de spend por servicio/unidad/día."""
    __tablename__ = "api_costs"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    service = db.Column(db.String(60), nullable=False, index=True)  # google_places_api/apollo_api/lusha_api/claude_api/etc.
    action = db.Column(db.String(80), nullable=True, index=True)
    unit = db.Column(db.String(40), nullable=True, index=True)  # aromatex/pestex/weldex
    user_id = db.Column(UUID(as_uuid=True), nullable=True)  # opcional, no FK estricto
    tokens_input = db.Column(db.Integer, default=0, nullable=False)
    tokens_output = db.Column(db.Integer, default=0, nullable=False)
    cost_usd = db.Column(db.Numeric(12, 6), default=0, nullable=False)
    cost_mxn = db.Column(db.Numeric(12, 4), default=0, nullable=False)
    api_metadata = db.Column(db.JSON, nullable=True)  # 'metadata' es palabra reservada de SQLAlchemy
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self):
        return {
            "id": self.id, "service": self.service, "action": self.action,
            "unit": self.unit,
            "user_id": str(self.user_id) if self.user_id else None,
            "tokens_input": self.tokens_input, "tokens_output": self.tokens_output,
            "cost_usd": float(self.cost_usd or 0),
            "cost_mxn": float(self.cost_mxn or 0),
            "metadata": self.api_metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Sale(db.Model):
    """Venta cerrada con cálculo de comisión.
    sale_type: suscripcion_nueva | servicio_unico | upsell
    commission_type: autogenerado (rate=1.0) | lead_otorgado (rate=0.5)
    Comisión: subs/upsell → monthly_amount * rate; servicio único → total_amount * 0.08
    """
    __tablename__ = "sales"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    lead_id = db.Column(UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=True, index=True)
    user_id = db.Column(UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=True, index=True)  # vendedor
    unit = db.Column(db.String(40), nullable=False, index=True)  # aromatex/pestex/weldex
    sale_type = db.Column(db.String(40), nullable=False)  # suscripcion_nueva/servicio_unico/upsell
    sale_category = db.Column(db.String(40), default="recurrente", nullable=False)  # recurrente/eventual
    uen = db.Column(db.String(120), nullable=True)
    lead_source = db.Column(db.String(120), nullable=True)  # snapshot para reporteo
    monthly_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    total_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    closed_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    contract_signed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    first_payment_at = db.Column(db.DateTime(timezone=True), nullable=True)
    service_start_at = db.Column(db.DateTime(timezone=True), nullable=True)
    commission_type = db.Column(db.String(40), nullable=True)  # autogenerado/lead_otorgado
    commission_rate = db.Column(db.Numeric(4, 2), nullable=True)  # 0.5 o 1.0
    commission_amount = db.Column(db.Numeric(14, 2), default=0, nullable=False)
    commission_status = db.Column(db.String(40), default="pendiente", nullable=False)  # pendiente/pagada/cancelada
    commission_pay_date = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(40), default="activa", nullable=False)  # activa/cancelada
    canceled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancel_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "lead_id": str(self.lead_id) if self.lead_id else None,
            "user_id": str(self.user_id) if self.user_id else None,
            "unit": self.unit, "sale_type": self.sale_type,
            "sale_category": self.sale_category, "uen": self.uen,
            "lead_source": self.lead_source,
            "monthly_amount": float(self.monthly_amount or 0),
            "total_amount": float(self.total_amount or 0),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "contract_signed_at": self.contract_signed_at.isoformat() if self.contract_signed_at else None,
            "first_payment_at": self.first_payment_at.isoformat() if self.first_payment_at else None,
            "service_start_at": self.service_start_at.isoformat() if self.service_start_at else None,
            "commission_type": self.commission_type,
            "commission_rate": float(self.commission_rate) if self.commission_rate else None,
            "commission_amount": float(self.commission_amount or 0),
            "commission_status": self.commission_status,
            "commission_pay_date": self.commission_pay_date.isoformat() if self.commission_pay_date else None,
            "status": self.status,
            "canceled_at": self.canceled_at.isoformat() if self.canceled_at else None,
            "cancel_reason": self.cancel_reason,
        }


class Client(db.Model):
    """Cliente post-venta. Se crea cuando una Sale cierra. Tracks NPS y churn."""
    __tablename__ = "clients"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=_genuuid)
    sale_id = db.Column(UUID(as_uuid=True), db.ForeignKey("sales.id"), nullable=True, index=True)
    company = db.Column(db.String(255), nullable=False)
    trade_name = db.Column(db.String(255), nullable=True)
    rfc = db.Column(db.String(20), nullable=True, index=True)
    service_address = db.Column(db.Text, nullable=True)
    city = db.Column(db.String(120), nullable=True)
    unit = db.Column(db.String(40), nullable=False, index=True)
    service = db.Column(db.Text, nullable=True)  # descripción del servicio contratado
    frequency = db.Column(db.String(60), nullable=True)
    monthly_amount = db.Column(db.Numeric(14, 2), nullable=True)
    contract_start = db.Column(db.Date, nullable=True)
    contract_end = db.Column(db.Date, nullable=True)
    assigned_to = db.Column(UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=True, index=True)
    nps_score = db.Column(db.Integer, nullable=True)
    nps_date = db.Column(db.DateTime(timezone=True), nullable=True)
    status = db.Column(db.String(40), default="activo", nullable=False)  # activo/en_riesgo/cancelado
    cancel_reason = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": str(self.id),
            "sale_id": str(self.sale_id) if self.sale_id else None,
            "company": self.company, "trade_name": self.trade_name,
            "rfc": self.rfc, "service_address": self.service_address,
            "city": self.city, "unit": self.unit, "service": self.service,
            "frequency": self.frequency,
            "monthly_amount": float(self.monthly_amount) if self.monthly_amount else None,
            "contract_start": self.contract_start.isoformat() if self.contract_start else None,
            "contract_end": self.contract_end.isoformat() if self.contract_end else None,
            "assigned_to": str(self.assigned_to) if self.assigned_to else None,
            "nps_score": self.nps_score,
            "nps_date": self.nps_date.isoformat() if self.nps_date else None,
            "status": self.status, "cancel_reason": self.cancel_reason,
        }


class SdrDirCreditsMonthly(db.Model):
    """Monthly bucket de créditos consumidos por servicio (Lusha/Apollo).
    PK compuesta vía UNIQUE(unit, service, year_month)."""
    __tablename__ = "sdr_dir_credits_monthly"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    unit = db.Column(db.String(40), nullable=False)
    service = db.Column(db.String(40), nullable=False)  # lusha / apollo / anthropic
    year_month = db.Column(db.String(7), nullable=False)  # YYYY-MM
    credits_used = db.Column(db.Integer, default=0, nullable=False)
    credits_limit = db.Column(db.Integer, nullable=False)
    hard_cap = db.Column(db.Boolean, default=True, nullable=False)
    alert_threshold = db.Column(db.Float, default=0.8, nullable=False)
    alerted_80 = db.Column(db.Boolean, default=False, nullable=False)
    alerted_95 = db.Column(db.Boolean, default=False, nullable=False)
    alerted_100 = db.Column(db.Boolean, default=False, nullable=False)
    last_sync_at = db.Column(db.DateTime(timezone=True), nullable=True)
    details_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("unit", "service", "year_month", name="uq_sdr_credits_unit_service_month"),
        db.Index("idx_sdr_credits_unit_service_month", "unit", "service", "year_month"),
    )

    def to_dict(self):
        return {
            "id": self.id, "unit": self.unit, "service": self.service,
            "year_month": self.year_month, "credits_used": self.credits_used,
            "credits_limit": self.credits_limit, "hard_cap": self.hard_cap,
            "alert_threshold": self.alert_threshold,
            "alerted_80": self.alerted_80, "alerted_95": self.alerted_95,
            "alerted_100": self.alerted_100,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
        }
