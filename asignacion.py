# asignacion.py
"""
Lógica de enrutamiento inteligente de leads:
  Especialidad por Marca + Round-Robin
"""
from datetime import datetime, timezone

from extensions import db
from models import Usuario, Lead, EtapaPipeline


def asignar_lead_comercial(datos_lead: dict) -> Lead:
    """
    Recibe los datos de un lead entrante, lo persiste en la BD
    y lo asigna al vendedor óptimo según:

      1. Filtra usuarios en_turno=True cuya especialidad_marca
         contenga la marca de interés del lead O contenga 'Todas'.
      2. De los candidatos, elige al que tenga el
         ultimo_lead_asignado más antiguo (Round-Robin).
         Si hay empate (ej. vendedores nuevos sin asignación),
         se toma el primero (NULLS FIRST).
      3. Actualiza ultimo_lead_asignado del vendedor seleccionado.

    Parámetros esperados en datos_lead:
        - telefono       (str, obligatorio)
        - nombre         (str, opcional)
        - origen         (str, opcional — valor del enum OrigenLead)
        - marca_interes  (str, obligatorio para el enrutamiento)
        - valor_estimado (float, opcional)
        - meta_lead_id   (str, opcional)
        - meta_form_id   (str, opcional)
        - meta_ad_id     (str, opcional)
        - meta_campaign  (str, opcional)

    Retorna:
        Lead creado y asignado (ya persistido en la BD).

    Raises:
        ValueError  — si no hay vendedores disponibles para la marca.
    """
    marca = datos_lead.get("marca_interes", "")

    # ── 1. Buscar vendedores candidatos ───────────────────────
    # ANY() compara contra cada elemento del array PostgreSQL
    candidatos = (
        Usuario.query
        .filter(
            Usuario.en_turno.is_(True),
            db.or_(
                Usuario.especialidad_marca.any(marca),
                Usuario.especialidad_marca.any("Todas"),
            ),
        )
        # NULLS FIRST: vendedores sin asignación previa van primero
        .order_by(Usuario.ultimo_lead_asignado.asc().nullsfirst())
        .all()
    )

    if not candidatos:
        raise ValueError(
            f"No hay vendedores en turno con especialidad "
            f"en '{marca}' o 'Todas'."
        )

    # ── 2. Seleccionar al siguiente en Round-Robin ────────────
    vendedor = candidatos[0]

    # ── 3. Crear el lead ──────────────────────────────────────
    from models import OrigenLead  # import local para evitar circular

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
        etapa_pipeline=EtapaPipeline.NUEVO_LEAD,
        valor_estimado=datos_lead.get("valor_estimado"),
        usuario_asignado_id=vendedor.id,
        meta_lead_id=datos_lead.get("meta_lead_id"),
        meta_form_id=datos_lead.get("meta_form_id"),
        meta_ad_id=datos_lead.get("meta_ad_id"),
        meta_campaign=datos_lead.get("meta_campaign"),
    )

    # ── 4. Actualizar timestamp Round-Robin del vendedor ──────
    vendedor.ultimo_lead_asignado = datetime.now(timezone.utc)

    db.session.add(lead)
    db.session.commit()

    return lead
