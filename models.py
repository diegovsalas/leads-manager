# models.py
import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal

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
    NUEVO_LEAD            = "Nuevo Lead"
    CALIFICANDO           = "Calificando"
    PRESENTACION_COTIZACION = "Presentación/Cotización"
    SEGUIMIENTO           = "Seguimiento"
    CIERRE_GANADO         = "Cierre Ganado"
    CIERRE_PERDIDO        = "Cierre Perdido"


class DireccionMensaje(enum.Enum):
    ENTRANTE         = "Entrante"
    SALIENTE_VENDEDOR = "Saliente_Vendedor"
    SALIENTE_BOT     = "Saliente_Bot"


# ──────────────────────────────────────────────
# Helper
# ──────────────────────────────────────────────
def _utcnow():
    return datetime.now(timezone.utc)


def _genuuid():
    return uuid.uuid4()


# ──────────────────────────────────────────────
# Tabla 1: usuarios (equipo comercial)
# ──────────────────────────────────────────────
class Usuario(db.Model):
    __tablename__ = "usuarios"

    id = db.Column(
        UUID(as_uuid=True), primary_key=True, default=_genuuid
    )
    nombre = db.Column(db.String(150), nullable=False)
    telefono_whatsapp = db.Column(db.String(30), nullable=True)
    rol_comercial = db.Column(
        db.Enum(RolComercial, name="rol_comercial_enum", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=RolComercial.ASESOR_COMERCIAL,
    )
    # Array de marcas: ['Aromatex', 'Pestex', 'Weldex', 'Todas']
    especialidad_marca = db.Column(
        ARRAY(db.String(80)),
        nullable=False,
        default=list,
        server_default="{}",
    )
    # Round-Robin: se asigna al vendedor con el timestamp más antiguo
    ultimo_lead_asignado = db.Column(
        db.DateTime(timezone=True), nullable=True
    )
    en_turno = db.Column(db.Boolean, default=True, nullable=False)

    # Relaciones
    leads = db.relationship("Lead", back_populates="usuario_asignado", lazy="dynamic")
    estados_bot = db.relationship("EstadoBotInterno", back_populates="usuario", lazy="dynamic")

    def __repr__(self):
        return f"<Usuario {self.nombre} | {self.rol_comercial.value}>"

    def to_dict(self):
        return {
            "id": str(self.id),
            "nombre": self.nombre,
            "telefono_whatsapp": self.telefono_whatsapp,
            "rol_comercial": self.rol_comercial.value,
            "especialidad_marca": self.especialidad_marca or [],
            "ultimo_lead_asignado": (
                self.ultimo_lead_asignado.isoformat()
                if self.ultimo_lead_asignado
                else None
            ),
            "en_turno": self.en_turno,
        }


# ──────────────────────────────────────────────
# Tabla 2: leads (oportunidades de venta)
# ──────────────────────────────────────────────
class Lead(db.Model):
    __tablename__ = "leads"

    id = db.Column(
        UUID(as_uuid=True), primary_key=True, default=_genuuid
    )
    telefono = db.Column(db.String(30), unique=True, nullable=False)
    nombre = db.Column(db.String(200), nullable=True)
    origen = db.Column(
        db.Enum(OrigenLead, name="origen_lead_enum", values_callable=lambda e: [x.value for x in e]),
        nullable=True,
    )
    marca_interes = db.Column(db.String(80), nullable=True)
    etapa_pipeline = db.Column(
        db.Enum(EtapaPipeline, name="etapa_pipeline_enum", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
        default=EtapaPipeline.NUEVO_LEAD,
    )

    # Cotizacion: cantidad × precio_unitario = valor_estimado
    cantidad_productos = db.Column(db.Integer, nullable=True)
    precio_unitario = db.Column(db.Numeric(14, 2), nullable=True)
    valor_estimado = db.Column(db.Numeric(14, 2), nullable=True)
    motivo_perdida = db.Column(db.String(300), nullable=True)

    # FK → usuarios
    usuario_asignado_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=True
    )
    usuario_asignado = db.relationship("Usuario", back_populates="leads")

    # Metadatos Meta Ads (conservados del esquema anterior)
    meta_lead_id = db.Column(db.String(100), unique=True, nullable=True)
    meta_form_id = db.Column(db.String(100), nullable=True)
    meta_ad_id = db.Column(db.String(100), nullable=True)
    meta_campaign = db.Column(db.String(200), nullable=True)

    # Timestamps
    fecha_creacion = db.Column(
        db.DateTime(timezone=True), default=_utcnow, nullable=False
    )
    fecha_actualizacion = db.Column(
        db.DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # Relaciones
    mensajes = db.relationship(
        "MensajeWhatsapp",
        back_populates="lead",
        order_by="MensajeWhatsapp.timestamp",
        lazy="dynamic",
    )

    def __repr__(self):
        return f"<Lead {self.nombre} | {self.telefono}>"

    @property
    def valor_calculado(self):
        """cantidad × precio_unitario si ambos existen, si no valor_estimado."""
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
            "usuario_asignado": vendedor,
            "fecha_creacion": self.fecha_creacion.isoformat(),
            "fecha_actualizacion": self.fecha_actualizacion.isoformat(),
            "ultimo_mensaje": ultimo_msg.to_dict() if ultimo_msg else None,
        }


# ──────────────────────────────────────────────
# Tabla 3: mensajes_whatsapp (historial de chat)
# ──────────────────────────────────────────────
class MensajeWhatsapp(db.Model):
    __tablename__ = "mensajes_whatsapp"

    id = db.Column(
        UUID(as_uuid=True), primary_key=True, default=_genuuid
    )
    lead_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=False
    )
    lead = db.relationship("Lead", back_populates="mensajes")

    direccion = db.Column(
        db.Enum(DireccionMensaje, name="direccion_mensaje_enum", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    contenido = db.Column(db.Text, nullable=False)
    meta_message_id = db.Column(db.String(100), unique=True, nullable=True)
    timestamp = db.Column(
        db.DateTime(timezone=True), default=_utcnow, nullable=False
    )

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
# Tabla 4: estado_bot_interno
# ──────────────────────────────────────────────
class EstadoBotInterno(db.Model):
    __tablename__ = "estado_bot_interno"

    id = db.Column(
        UUID(as_uuid=True), primary_key=True, default=_genuuid
    )
    usuario_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("usuarios.id"), nullable=False
    )
    usuario = db.relationship("Usuario", back_populates="estados_bot")

    lead_contexto_id = db.Column(
        UUID(as_uuid=True), db.ForeignKey("leads.id"), nullable=True
    )
    lead_contexto = db.relationship("Lead")

    esperando_input = db.Column(
        db.String(50), nullable=False, default="ninguno"
    )

    def __repr__(self):
        return f"<BotState usuario={self.usuario_id} esperando={self.esperando_input}>"

    def to_dict(self):
        return {
            "id": str(self.id),
            "usuario_id": str(self.usuario_id),
            "lead_contexto_id": str(self.lead_contexto_id) if self.lead_contexto_id else None,
            "esperando_input": self.esperando_input,
        }


# ──────────────────────────────────────────────
# Tabla 5: gastos_publicidad (inversion en ads)
# ──────────────────────────────────────────────
class PlataformaAds(enum.Enum):
    FACEBOOK  = "Facebook"
    INSTAGRAM = "Instagram"
    GOOGLE    = "Google"
    TIKTOK    = "TikTok"
    OTRO      = "Otro"


class GastoPublicidad(db.Model):
    __tablename__ = "gastos_publicidad"

    id = db.Column(
        UUID(as_uuid=True), primary_key=True, default=_genuuid
    )
    plataforma = db.Column(
        db.Enum(PlataformaAds, name="plataforma_ads_enum", values_callable=lambda e: [x.value for x in e]),
        nullable=False,
    )
    marca = db.Column(db.String(80), nullable=True)  # Aromatex, Pestex, etc.
    campana = db.Column(db.String(200), nullable=True)
    monto = db.Column(db.Numeric(14, 2), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    notas = db.Column(db.String(300), nullable=True)

    fecha_registro = db.Column(
        db.DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self):
        return f"<Gasto {self.plataforma.value} ${self.monto} {self.fecha}>"

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
