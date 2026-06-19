"""
Registry de campañas Meta con asignación dirigida.

Mapeo campaign_id → marca + zona + unidad. Lee de la tabla `meta_campaigns`
(editable vía UI admin en /api/meta-campaigns). Cuando llega un lead vía
meta_lead_polling.py, este registry decide:
  - qué marca poner (override del form, porque la campaña ES la marca)
  - qué estado default usar si el form no trae uno
  - a qué unit reportar (para SCIP / SDR)
  - en qué zonas geográficas se está pautando (para validar/filtrar
    candidatos al asignar)

Cache TTL corto (60s) para no martillar BD desde el polling. invalidate()
fuerza recarga inmediata después de un edit en UI.
"""
import time
from typing import Optional

# Convenciones de zona comerciales estándar de México — útiles como presets
# en la UI cuando se da de alta una campaña nueva.
ZONA_NORTE = [
    "Nuevo León", "Coahuila", "Tamaulipas", "Chihuahua", "Durango",
    "Sonora", "Baja California", "Baja California Sur", "Sinaloa",
]

ZONA_CENTRO = [
    "Jalisco", "Guanajuato", "Querétaro", "Aguascalientes",
    "San Luis Potosí", "Zacatecas", "Nayarit", "Michoacán", "Colima",
    "CDMX", "Estado de México", "Hidalgo", "Tlaxcala", "Morelos", "Puebla",
]

ZONA_SUR = [
    "Veracruz", "Oaxaca", "Guerrero", "Chiapas", "Tabasco",
    "Campeche", "Yucatán", "Quintana Roo",
]

ZONA_PRESETS = {"Norte": ZONA_NORTE, "Centro": ZONA_CENTRO, "Sur": ZONA_SUR}


_cache: dict = {}
_cache_expires: float = 0.0
_CACHE_TTL = 60  # segundos


def _load_from_db() -> dict:
    """Lee meta_campaigns y arma el dict {campaign_id: meta}."""
    try:
        from models import MetaCampaign
        rows = MetaCampaign.query.filter_by(activa=True).all()
        return {
            r.campaign_id: {
                "nombre":         r.nombre,
                "marca":          r.marca,
                "unidad":         r.unidad,
                "estado_default": r.estado_default,
                "zonas":          list(r.zonas or []),
            }
            for r in rows
        }
    except Exception:
        # BD no disponible (ej. en pruebas o boot) — devolver dict vacío
        return {}


def _get_cache() -> dict:
    global _cache, _cache_expires
    now = time.time()
    if now > _cache_expires:
        _cache = _load_from_db()
        _cache_expires = now + _CACHE_TTL
    return _cache


def invalidate():
    """Fuerza recarga inmediata desde BD en el siguiente lookup.
    Llamar después de crear/editar/borrar una campaña en la UI."""
    global _cache_expires
    _cache_expires = 0.0


def lookup(campaign_id: Optional[str]) -> Optional[dict]:
    """Devuelve metadata de la campaña o None si no está registrada o inactiva."""
    if not campaign_id:
        return None
    return _get_cache().get(str(campaign_id))


def aplicar_a_lead(datos_lead: dict, campaign_id: Optional[str]) -> dict:
    """Enriquecer datos_lead con info del registry.
    Modifica el dict in-place (también lo retorna) y agrega:
      - marca_interes (override si la campaña la define)
      - estado por default (si form viene vacío)
      - meta_campaign_unit (para SCIP/SDR)
    No-op si la campaña no está en el registry."""
    meta = lookup(campaign_id)
    if not meta:
        return datos_lead

    # Marca: la campaña manda — refleja la unidad de negocio que pauta.
    datos_lead["marca_interes"] = meta["marca"]

    # Estado: solo rellena si form no trajo uno.
    if not datos_lead.get("estado_cliente") and not datos_lead.get("estado"):
        if meta.get("estado_default"):
            datos_lead["estado_cliente"] = meta["estado_default"]

    # Unit para reporting (SCIP, SDR).
    datos_lead["meta_campaign_unit"] = meta["unidad"]
    datos_lead["meta_campaign_nombre"] = meta["nombre"]

    return datos_lead
