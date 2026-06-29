# un_filter.py
"""
Filtro global por Unidad de Negocio (UN). FEAT-2026-06-29.

Centraliza la lógica de filtrado por UN para que cada endpoint la aplique
de forma consistente.

UN canónicas (4):
  - Aromatex      (agrupa Aromatex Home)
  - Pestex
  - Weldex
  - Nexo

Decisiones aprobadas por Diego (2026-06-29):
  - Aromatex Home cae dentro del filtro Aromatex.
  - Leads sin marca_interes son SIEMPRE visibles aunque haya filtro
    activo, para forzar al equipo a clasificarlos (UI los marca con
    badge rojo "Sin UN — clasificar").
  - CS Accounts con unidades_contratadas multi-UN (ej. "AROMATEX,PESTEX")
    aparecen en cualquier filtro que las contenga.
  - Usuarios cuya especialidad_marca contiene la UN buscada (incluido
    el caso Aromatex Home → Aromatex).
"""
from sqlalchemy import or_, func

UN_CANONICAS = ("Aromatex", "Pestex", "Weldex", "Nexo")

# Aliases que mapean a cada UN canónica (case-insensitive).
# Si un valor matchea cualquiera de estos aliases, se considera de esa UN.
_UN_ALIASES = {
    "Aromatex": ("aromatex", "aromatex home", "aromatex_home", "aromatexhome"),
    "Pestex":   ("pestex",),
    "Weldex":   ("weldex",),
    "Nexo":     ("nexo",),
}


def normalizar_un(value):
    """Dado un string de marca/UN, retorna la UN canónica o None.
    Ej.: 'Aromatex Home' → 'Aromatex'; 'PESTEX' → 'Pestex'."""
    if not value:
        return None
    v = str(value).strip().lower()
    for canon, aliases in _UN_ALIASES.items():
        if v in aliases:
            return canon
    return None


def es_un_valida(un):
    """True si la UN pasada es una de las 4 canónicas (case-insensitive)."""
    if not un:
        return False
    return un.capitalize() in UN_CANONICAS


def filtrar_leads_por_un(query, lead_model, un):
    """Aplica filtro de UN a un query de Lead.

    Política: leads con marca_interes vacío/NULL siguen visibles
    (para forzar clasificación). Solo se OCULTAN los leads con
    marca_interes seteada que NO corresponde a la UN buscada.
    """
    if not un or not es_un_valida(un):
        return query
    aliases = _UN_ALIASES[un.capitalize()]
    # OR: marca vacía (siempre visible) OR marca matchea cualquier alias
    return query.filter(or_(
        lead_model.marca_interes.is_(None),
        lead_model.marca_interes == "",
        func.lower(lead_model.marca_interes).in_(aliases),
    ))


def filtrar_cs_accounts_por_un(query, account_model, un):
    """Aplica filtro de UN a CSAccount.

    CS Accounts tienen unidades_contratadas como string (ej.
    'AROMATEX,PESTEX'). Filtramos por substring de la UN canónica
    (case-insensitive). Cuentas sin unidades_contratadas se ocultan
    cuando hay filtro (a diferencia de leads — en CS no esperamos
    cuentas sin UN seteada).
    """
    if not un or not es_un_valida(un):
        return query
    canon = un.capitalize()
    aliases = _UN_ALIASES[canon]
    # OR de LIKEs: marca_interes ilike '%aromatex%' OR '%aromatex home%' etc.
    ors = [func.lower(account_model.unidades_contratadas).like(f"%{a}%") for a in aliases]
    return query.filter(or_(*ors))


def usuario_pertenece_a_un(especialidad_marca_list, un):
    """True si el usuario tiene la UN buscada (o un alias) en su
    especialidad_marca. especialidad_marca es lista JSONB.
    Si la lista está vacía, no pertenece a ninguna UN específica."""
    if not un or not es_un_valida(un):
        return True  # sin filtro → todos pasan
    if not especialidad_marca_list:
        return False
    aliases = _UN_ALIASES[un.capitalize()]
    for marca in especialidad_marca_list:
        if normalizar_un(marca) == un.capitalize():
            return True
        if str(marca or "").strip().lower() in aliases:
            return True
    return False


def filtrar_usuarios_por_un(query, usuario_model, un):
    """Filtra Usuario.query por especialidad_marca que contenga la UN.
    Como especialidad_marca es JSONB ARRAY, hacemos un OR de LIKEs
    sobre la representación texto para no requerir extensión jsonb_path."""
    if not un or not es_un_valida(un):
        return query
    aliases = _UN_ALIASES[un.capitalize()]
    # Cast JSONB → text y LIKE (Postgres permite jsonb::text directamente)
    ors = [
        func.lower(func.cast(usuario_model.especialidad_marca, db_text_type()))
        .like(f"%{a}%") for a in aliases
    ]
    return query.filter(or_(*ors))


def db_text_type():
    """Helper para evitar import circular con extensions."""
    from sqlalchemy import Text
    return Text


def default_un_para_usuario(especialidad_marca_list, rol=None):
    """Default del filtro UN al iniciar sesión, por rol + especialidad.

    - Super Admin / KAM → 'Todas' (None)
    - Vendedor con 1 sola UN canónica en su especialidad → esa UN
    - Vendedor multi-UN → 'Todas' (None)
    - Vendedor sin especialidad → 'Todas' (None)
    """
    if rol and rol.lower().replace(" ", "_") in ("super_admin", "kam"):
        return None
    if not especialidad_marca_list:
        return None
    uns_canon = set()
    for m in especialidad_marca_list:
        c = normalizar_un(m)
        if c:
            uns_canon.add(c)
    if len(uns_canon) == 1:
        return uns_canon.pop()
    return None
