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
