# asignacion.py
"""
Algoritmo de asignación de leads v2:
  1. Filtro por marca (especialidad) + en_turno
  2. Filtro por zona geográfica (estado del cliente)
  3. Ordenar por carga de trabajo (menos leads activos primero)
  4. Desempate por rendimiento (mejor % conversión primero)
"""
from datetime import datetime, timezone

from extensions import db
from models import Usuario, Lead, EtapaPipeline


# ── Normalización de estado (fuzzy matching) ──────────────
ESTADO_ALIASES = {
    # Nuevo León
    "nuevo leon": "Nuevo León", "nuevo león": "Nuevo León",
    "monterrey": "Nuevo León", "monterey": "Nuevo León", "mty": "Nuevo León",
    "nl": "Nuevo León", "san pedro": "Nuevo León", "apodaca": "Nuevo León",
    "san nicolas": "Nuevo León", "guadalupe nl": "Nuevo León", "escobedo": "Nuevo León",
    # CDMX
    "cdmx": "CDMX", "ciudad de mexico": "CDMX", "ciudad de méxico": "CDMX",
    "df": "CDMX", "mexico city": "CDMX", "coyoacan": "CDMX",
    # Estado de México
    "estado de mexico": "Estado de México", "estado de méxico": "Estado de México",
    "edomex": "Estado de México", "naucalpan": "Estado de México",
    "tlalnepantla": "Estado de México", "ecatepec": "Estado de México",
    # Jalisco
    "jalisco": "Jalisco", "guadalajara": "Jalisco", "gdl": "Jalisco",
    "zapopan": "Jalisco", "tlaquepaque": "Jalisco",
    # Querétaro
    "queretaro": "Querétaro", "querétaro": "Querétaro", "qro": "Querétaro",
    # Puebla
    "puebla": "Puebla", "pue": "Puebla",
    # Guanajuato
    "guanajuato": "Guanajuato", "leon": "Guanajuato", "león": "Guanajuato",
    "irapuato": "Guanajuato", "celaya": "Guanajuato", "gto": "Guanajuato",
    # Chihuahua
    "chihuahua": "Chihuahua", "juarez": "Chihuahua", "juárez": "Chihuahua",
    "ciudad juarez": "Chihuahua", "ciudad juárez": "Chihuahua",
    # Tamaulipas
    "tamaulipas": "Tamaulipas", "reynosa": "Tamaulipas", "tampico": "Tamaulipas",
    "matamoros": "Tamaulipas",
    # Coahuila
    "coahuila": "Coahuila", "saltillo": "Coahuila", "torreon": "Coahuila",
    "torreón": "Coahuila", "monclova": "Coahuila",
    # Sonora
    "sonora": "Sonora", "hermosillo": "Sonora", "nogales": "Sonora",
    "obregon": "Sonora", "obregón": "Sonora",
    # Baja California
    "baja california": "Baja California", "tijuana": "Baja California",
    "mexicali": "Baja California", "ensenada": "Baja California",
    # Sinaloa
    "sinaloa": "Sinaloa", "culiacan": "Sinaloa", "culiacán": "Sinaloa",
    "mazatlan": "Sinaloa", "mazatlán": "Sinaloa", "los mochis": "Sinaloa",
    # Veracruz
    "veracruz": "Veracruz", "xalapa": "Veracruz", "coatzacoalcos": "Veracruz",
    # Yucatán
    "yucatan": "Yucatán", "yucatán": "Yucatán", "merida": "Yucatán", "mérida": "Yucatán",
    # Michoacán
    "michoacan": "Michoacán", "michoacán": "Michoacán", "morelia": "Michoacán",
    # San Luis Potosí
    "san luis potosi": "San Luis Potosí", "san luis potosí": "San Luis Potosí",
    "slp": "San Luis Potosí",
    # Aguascalientes
    "aguascalientes": "Aguascalientes", "ags": "Aguascalientes",
    # Otros estados
    "oaxaca": "Oaxaca", "tabasco": "Tabasco", "chiapas": "Chiapas",
    "guerrero": "Guerrero", "acapulco": "Guerrero",
    "morelos": "Morelos", "cuernavaca": "Morelos",
    "hidalgo": "Hidalgo", "pachuca": "Hidalgo",
    "durango": "Durango", "nayarit": "Nayarit",
    "colima": "Colima", "campeche": "Campeche",
    "quintana roo": "Quintana Roo", "cancun": "Quintana Roo", "cancún": "Quintana Roo",
    "tlaxcala": "Tlaxcala", "zacatecas": "Zacatecas",
    "baja california sur": "Baja California Sur", "la paz bcs": "Baja California Sur",
}

# Lista de estados oficiales para matching directo
ESTADOS_MEXICO = [
    "Aguascalientes", "Baja California", "Baja California Sur", "Campeche",
    "Chiapas", "Chihuahua", "CDMX", "Coahuila", "Colima", "Durango",
    "Estado de México", "Guanajuato", "Guerrero", "Hidalgo", "Jalisco",
    "Michoacán", "Morelos", "Nayarit", "Nuevo León", "Oaxaca", "Puebla",
    "Querétaro", "Quintana Roo", "San Luis Potosí", "Sinaloa", "Sonora",
    "Tabasco", "Tamaulipas", "Tlaxcala", "Veracruz", "Yucatán", "Zacatecas",
]


def normalizar_estado(texto: str) -> str:
    """Normaliza texto libre a nombre de estado oficial."""
    if not texto:
        return ""
    t = texto.strip().lower()
    # Match directo en aliases
    if t in ESTADO_ALIASES:
        return ESTADO_ALIASES[t]
    # Match por nombre oficial (case insensitive)
    for estado in ESTADOS_MEXICO:
        if estado.lower() == t:
            return estado
    # Match parcial — si el texto contiene un alias
    for alias, estado in ESTADO_ALIASES.items():
        if alias in t or t in alias:
            return estado
    return texto.strip().title()


def _carga_trabajo(vendedor) -> int:
    """Cuenta leads activos (no cerrados) del vendedor."""
    return (
        Lead.query.filter(
            Lead.usuario_asignado_id == vendedor.id,
            Lead.etapa_pipeline.notin_([
                EtapaPipeline.CIERRE_GANADO,
                EtapaPipeline.CIERRE_PERDIDO,
            ]),
        ).count()
    )


def _pct_conversion(vendedor) -> float:
    """Calcula % de leads cerrados ganados vs total asignados."""
    total = Lead.query.filter(Lead.usuario_asignado_id == vendedor.id).count()
    if total == 0:
        return 0.5  # sin historial, asumir 50%
    ganados = Lead.query.filter(
        Lead.usuario_asignado_id == vendedor.id,
        Lead.etapa_pipeline == EtapaPipeline.CIERRE_GANADO,
    ).count()
    return ganados / total


def asignar_lead_comercial(datos_lead: dict) -> Lead:
    """
    Asigna un lead al mejor vendedor según:
      1. Filtro por marca (especialidad) + en_turno
      2. Filtro por zona geográfica
      3. Menor carga de trabajo
      4. Desempate: mejor % conversión
    """
    marca = datos_lead.get("marca_interes", "")
    estado = normalizar_estado(datos_lead.get("estado", ""))

    # ── 1. Filtrar por marca + en_turno ──
    candidatos = (
        Usuario.query.filter(
            Usuario.en_turno.is_(True),
            db.or_(
                Usuario.especialidad_marca.any(marca),
                Usuario.especialidad_marca.any("Todas"),
            ),
        ).all()
    )

    if not candidatos:
        raise ValueError(f"No hay vendedores en turno para '{marca}'")

    # ── 2. Filtrar por zona geográfica ──
    if estado:
        en_zona = [v for v in candidatos if estado in (v.zona_cobertura or [])]
        if en_zona:
            candidatos = en_zona

    # ── 3. Ordenar por carga (menor primero), luego por conversión (mayor primero) ──
    candidatos.sort(key=lambda v: (_carga_trabajo(v), -_pct_conversion(v)))

    vendedor = candidatos[0]

    # ── 4. Crear el lead ──
    from models import OrigenLead

    origen_valor = datos_lead.get("origen")
    origen_enum = None
    if origen_valor:
        try:
            origen_enum = OrigenLead(origen_valor)
        except ValueError:
            origen_enum = None

    lead = Lead(
        telefono=datos_lead["telefono"],
        nombre=datos_lead.get("nombre"),
        origen=origen_enum,
        marca_interes=marca,
        estado_cliente=estado,
        etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        valor_estimado=datos_lead.get("valor_estimado"),
        usuario_asignado_id=vendedor.id,
        meta_lead_id=datos_lead.get("meta_lead_id"),
        meta_form_id=datos_lead.get("meta_form_id"),
        meta_ad_id=datos_lead.get("meta_ad_id"),
        meta_campaign=datos_lead.get("meta_campaign"),
    )

    vendedor.ultimo_lead_asignado = datetime.now(timezone.utc)

    db.session.add(lead)
    db.session.commit()

    return lead
