# incidencia_tipos.py
"""
Catálogo de tipos de incidencia por servicio.  FEAT-2026-09-03

Antes esta lista vivía duplicada en dos plantillas: el portal público de
tickets y el modal de CS. Dos copias de la misma verdad terminan
divergiendo, y peor: el portal filtraba los tipos según el servicio, pero
el modal de CS los mostraba TODOS agrupados, así que se podía registrar
"Equipo no huele" —una falla de aroma— dentro de una incidencia de
Fumigación. Dos registros entraron así.

El bloqueo de verdad está en el backend (validar_tipo). Filtrar la lista en
pantalla evita el error de dedo, pero no impide un POST directo ni protege
a un formulario que alguien agregue después.
"""

TIPOS_POR_SERVICIO = {
    "Aroma": [
        "Equipo no huele",
        "Equipo apagado",
        "Derrame de aroma",
        "Ruidos en equipo",
        "Equipo desprendido",
        "Robo/Extravio equipo",
        "Reubicacion equipo",
        "Incumplimiento visita",
        "Mantenimiento equipo",
    ],
    "Fumigacion": [
        "Roedores",
        "Cucarachas",
        "Moscas/Mosquitos",
        "Alacranes",
        "Termitas",
        "Chinches",
        "Aves",
        "Otro insecto",
        "Garantia fumigacion",
    ],
}

SERVICIOS = tuple(TIPOS_POR_SERVICIO)


def tipos_de(servicio):
    """Los tipos válidos para ese servicio. Lista vacía si no existe."""
    return list(TIPOS_POR_SERVICIO.get((servicio or "").strip(), []))


def servicio_de(tipo):
    """A qué servicio pertenece un tipo. None si no está en el catálogo.

    Sirve para diagnosticar registros viejos: si el servicio guardado no
    coincide con este, la incidencia quedó mal clasificada.
    """
    t = (tipo or "").strip()
    for servicio, tipos in TIPOS_POR_SERVICIO.items():
        if t in tipos:
            return servicio
    return None


def validar_tipo(servicio, tipo):
    """(ok, mensaje). El mensaje explica qué está mal, no solo que falló.

    Un tipo fuera del catálogo se acepta: puede ser un registro histórico o
    un caso que todavía no se tipificó. Lo que se rechaza es usar un tipo
    que pertenece explícitamente a OTRO servicio, que es el error real:
    ensucia el análisis haciendo pasar una falla de equipo por una plaga.
    """
    s = (servicio or "").strip()
    t = (tipo or "").strip()
    if not t:
        return False, "Falta el tipo de incidencia."
    if s not in TIPOS_POR_SERVICIO:
        return False, f"Servicio «{s}» desconocido. Debe ser Aroma o Fumigacion."
    if t in TIPOS_POR_SERVICIO[s]:
        return True, ""

    duenio = servicio_de(t)
    if duenio:
        return False, (f"«{t}» es un tipo de {duenio}, no de {s}. "
                       f"Cambia el servicio o elige un tipo de {s}.")
    return True, ""      # tipo libre: no está en el catálogo de nadie


def diagnosticar(incidencias):
    """Incidencias cuyo tipo pertenece a otro servicio. Para revisar lo ya
    capturado antes de que el bloqueo existiera."""
    malas = []
    for i in incidencias:
        duenio = servicio_de(i.tipo)
        if duenio and duenio != (i.servicio or "").strip():
            malas.append((i, duenio))
    return malas
