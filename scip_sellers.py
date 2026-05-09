"""
SCIP Sellers — atribución de ads a vendedores.

Port focal de scip/sellers.routes.js (526 líneas legacy). Lógica clave:
parsear ad_name buscando primer nombre del vendedor (ej. "AROMATEX_TIENDAS_JANETH_v3"
→ vendedor con primer nombre "Janeth"). Útil para SCIP cuando un director
quiere reasignar tráfico de un ad a otro vendedor o ver quién recibe leads
de cada ad.
"""
import logging
import re
import unicodedata
from typing import Optional

from extensions import db
from models import Usuario

log = logging.getLogger("scip_sellers")


def _normalize(s: str) -> str:
    """Quita acentos, lowercase, solo a-z."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    only_ascii = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", only_ascii.lower())


def parse_ad_name(name: str) -> dict:
    """Extrae partes útiles del ad_name. Convención típica:
    'AROMATEX_RETAIL_HOMEDEPOT_JANETH_v3' → tokens ['aromatex','retail','homedepot','janeth','v3'].
    """
    if not name:
        return {"tokens": [], "unit_hint": None, "version": None}
    parts = re.split(r"[_\-\s]+", name.strip())
    tokens = [t for t in parts if t]
    norm = [_normalize(t) for t in tokens]
    unit_hint = None
    for t in norm:
        if "aromatex" in t:
            unit_hint = "Aromatex"; break
        if "pestex" in t:
            unit_hint = "Pestex"; break
        if "weldex" in t or "weldu" in t:
            unit_hint = "Weldex"; break
    version = next((t for t in tokens if re.match(r"^v\d+$", t.lower())), None)
    return {"tokens": tokens, "tokens_norm": norm, "unit_hint": unit_hint, "version": version}


def match_seller_by_first_name(first_name: str) -> Optional[Usuario]:
    """Busca un Usuario activo cuyo primer nombre matche (normalizado)."""
    if not first_name:
        return None
    target = _normalize(first_name)
    if not target:
        return None
    candidates = Usuario.query.filter(Usuario.en_turno.is_(True)).all()
    for u in candidates:
        primer = (u.nombre or "").split(" ")[0]
        if _normalize(primer) == target:
            return u
    # Match parcial (prefix)
    for u in candidates:
        primer = _normalize((u.nombre or "").split(" ")[0])
        if primer and primer.startswith(target[:5]):
            return u
    return None


def match_seller_by_any_part(ad_name: str) -> Optional[Usuario]:
    """Recorre todos los tokens del ad_name buscando match con primer nombre
    de cualquier Usuario activo. Devuelve la PRIMERA coincidencia."""
    parsed = parse_ad_name(ad_name)
    for tok in parsed["tokens_norm"]:
        if len(tok) < 3:
            continue
        u = match_seller_by_first_name(tok)
        if u:
            return u
    return None


def attribute_ads(ads: list) -> list:
    """Recibe lista de ads (dicts con 'name'), agrega campo 'attributed_seller'
    con {id, nombre, especialidad_marca} si hay match, o None."""
    out = []
    for ad in ads:
        seller = match_seller_by_any_part(ad.get("name", ""))
        ad_copy = dict(ad)
        if seller:
            ad_copy["attributed_seller"] = {
                "id": str(seller.id),
                "nombre": seller.nombre,
                "especialidad_marca": list(seller.especialidad_marca or []),
            }
        else:
            ad_copy["attributed_seller"] = None
        out.append(ad_copy)
    return out


def list_sellers_for_unit(unit: str) -> list:
    """Lista vendedores activos cuya especialidad_marca incluye el unit dado.
    Usado en SCIP cuando director quiere reasignar tráfico a otro vendedor."""
    if not unit:
        rows = Usuario.query.filter(Usuario.en_turno.is_(True)).all()
    else:
        # PostgreSQL ARRAY contains
        from sqlalchemy import any_
        rows = Usuario.query.filter(
            Usuario.en_turno.is_(True),
            Usuario.especialidad_marca.any(unit),
        ).all()
    return [{
        "id": str(u.id), "nombre": u.nombre,
        "especialidad_marca": list(u.especialidad_marca or []),
        "zona_cobertura": list(u.zona_cobertura or []),
        "rol_comercial": u.rol_comercial.value if u.rol_comercial else None,
    } for u in rows]
