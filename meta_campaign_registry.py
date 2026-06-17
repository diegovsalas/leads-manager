"""
Registry de campañas Meta con asignación dirigida.

Mapeo campaign_id → marca + zona + unidad. Cuando llega un lead vía
meta_lead_polling.py, este registry decide:
  - qué marca poner (override del form, porque la campaña ES la marca)
  - qué estado default usar si el form no trae uno
  - a qué unit reportar (para SCIP / SDR)
  - en qué zonas geográficas se está pautando (para validar/filtrar
    candidatos al asignar)

Para añadir una campaña nueva: agrega un entry con la campaign_id de Meta
como key. Si tiene zona única (ej. MTY) → lista de un solo estado. Si pauta
en varios estados, lista todos.

Zonas AX-B2B (Norte/Centro/Sur) usan la convención comercial estándar.
Ajustar si cambian.
"""
from typing import Optional


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


# campaign_id (string, como lo devuelve la Graph API) → metadata
CAMPAIGNS = {
    # ── Weldex (Weldu) — Monterrey ───────────────────────────────
    "120248752029380246": {
        "nombre":          "Weldu - Servicios - MTY",
        "marca":           "Weldex",
        "unidad":          "weldex",
        "estado_default":  "Nuevo León",
        "zonas":           ["Nuevo León"],
    },
    "120248749817010246": {
        "nombre":          "Weldu - Limpieza Carrusel - MTY",
        "marca":           "Weldex",
        "unidad":          "weldex",
        "estado_default":  "Nuevo León",
        "zonas":           ["Nuevo León"],
    },

    # ── Aromatex B2B — Formularios Junio 2026 ────────────────────
    "120245034081530080": {
        "nombre":          "[AX-B2B] Contactos Sur · Formularios · Junio 2026",
        "marca":           "Aromatex",
        "unidad":          "aromatex_b2b",
        "estado_default":  None,  # depende del form
        "zonas":           ZONA_SUR,
    },
    "120245032865400080": {
        "nombre":          "[AX-B2B] Contactos Norte · Formularios · Junio 2026",
        "marca":           "Aromatex",
        "unidad":          "aromatex_b2b",
        "estado_default":  None,
        "zonas":           ZONA_NORTE,
    },
    "120245034025690080": {
        "nombre":          "[AX-B2B] Contactos Centro · Formularios · Junio 2026",
        "marca":           "Aromatex",
        "unidad":          "aromatex_b2b",
        "estado_default":  None,
        "zonas":           ZONA_CENTRO,
    },
}


def lookup(campaign_id: Optional[str]) -> Optional[dict]:
    """Devuelve metadata de la campaña o None si no está registrada."""
    if not campaign_id:
        return None
    return CAMPAIGNS.get(str(campaign_id))


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
