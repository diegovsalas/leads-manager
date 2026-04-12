# icp_scoring.py
"""
ICP Scoring Engine — Grupo Avantex
Marketing Sensorial y Aromatización Inteligente

Criterios diseñados para empresas de aromatización con contratos recurrentes.
NOTA: Score preliminar. Alejandro Gil debe validar pesos definitivos.
"""

# ── Industrias por tier (0-30 pts) ──
INDUSTRIA_SCORES = {
    # Tier S (30 pts)
    "Hoteles y Resorts": 30,
    "Casinos y Entretenimiento": 30,
    # Tier A (25 pts)
    "Centros Comerciales": 25,
    "Hospitales y Clínicas": 25,
    "Concesionarios Automotrices": 25,
    "Restaurantes Cadena": 25,
    # Tier B (20 pts)
    "Gimnasios y Spas": 20,
    "Corporativos y Oficinas": 20,
    "Retail y Moda": 20,
    "Escuelas y Universidades": 20,
    # Tier C (15 pts)
    "Inmobiliarias": 15,
    "Consultorios y Estéticas": 15,
    # Tier D (10 pts)
    "Comercio Independiente": 10,
}
DEFAULT_INDUSTRIA_SCORE = 5  # "Otro / Sin definir"

# ── Tamaño de empresa (0-25 pts) ──
TAMANO_SCORES = {
    "Enterprise": 25,     # +50 empleados
    "Mediana": 20,        # 11-50 empleados
    "Pequeña": 12,        # 4-10 empleados
    "Micro": 5,           # 1-3 empleados
}
DEFAULT_TAMANO_SCORE = 5

# ── Sucursales (0-25 pts) ──
def _score_sucursales(num):
    if num is None or num <= 0:
        return 5
    if num >= 10:
        return 25
    if num >= 4:
        return 20
    if num >= 2:
        return 15
    return 8  # 1 sucursal

# ── Señales de compra (0-20 pts) ──
SENAL_SCORES = {
    "recompra": 10,
    "cambio_proveedor": 8,
    "solicito_cotizacion": 7,
    "referido": 6,
    "respuesta_rapida": 5,
    "info_general": 2,
}


def calcular_icp(tipo_industria=None, tamano_empresa=None,
                 num_sucursales=None, tipo_cliente=None,
                 respondio_ultimo_contacto=False, **kwargs):
    """
    Calcula el ICP score y nivel de un lead.

    Returns: (score: int, nivel: str)
        score: 0-100
        nivel: 'A', 'B', 'C', 'D'
    """
    score = 0

    # 1. Industria (0-30)
    score += INDUSTRIA_SCORES.get(tipo_industria, DEFAULT_INDUSTRIA_SCORE)

    # 2. Tamaño (0-25)
    score += TAMANO_SCORES.get(tamano_empresa, DEFAULT_TAMANO_SCORE)

    # 3. Sucursales (0-25)
    score += _score_sucursales(num_sucursales)

    # 4. Señales (0-20)
    senales = 0
    if tipo_cliente == "Recurrente":
        senales += SENAL_SCORES["recompra"]
    if respondio_ultimo_contacto:
        senales += SENAL_SCORES["respuesta_rapida"]
    score += min(senales, 20)

    # Clamp
    score = min(score, 100)

    # Nivel
    if score >= 80:
        nivel = "A"
    elif score >= 55:
        nivel = "B"
    elif score >= 30:
        nivel = "C"
    else:
        nivel = "D"

    return score, nivel


# Listas para UI dropdowns
INDUSTRIAS = list(INDUSTRIA_SCORES.keys()) + ["Otro"]
TAMANOS = list(TAMANO_SCORES.keys())
