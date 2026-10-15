"""Conciliación conservadora entre nombres/IDs de fuentes distintas."""
import unicodedata


def unique_appointment_rows(rows):
    """Colapsa repeticiones del export; aísla IDs con datos contradictorios.

    El export puede incluir distintas líneas de equipo para la misma cita.
    Solo se comparan los campos que CS importa, nunca se elige una versión
    arbitraria de un estatus, fecha, cliente o cantidad contradictorios.
    """
    fields = ('Cliente', 'ID cliente', 'ID Cliente', 'Propiedad', 'Dirección',
              'Direccion', 'Zona', 'Tecnico', 'Técnico', 'Fecha de Inicio',
              'Fecha de Terminación', 'Fecha de Terminacion', 'Estatus',
              'Titulo Servicio', 'Título Servicio', 'Cantidad')
    groups = {}
    for row in rows:
        key = str(row.get('ID cita') or row.get('ID') or row.get('Appointment ID') or '').strip()
        # El importador informa los IDs ausentes como errores por fila.
        if not key:
            key = ('missing', len(groups))
        groups.setdefault(key, []).append(row)
    unique, duplicates, conflicts = [], 0, 0
    for group in groups.values():
        signatures = {tuple(str(r.get(f) or '').strip() for f in fields) for r in group}
        if len(signatures) > 1:
            conflicts += len(group)
        else:
            unique.append(group[0])
            duplicates += len(group) - 1
    return unique, duplicates, conflicts


def _name(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',str(value or '')) if not unicodedata.combining(c)).casefold().split())


def resolve_account(cliente, client_id, names, ids):
    """IDs coincidentes requieren nombre compatible; nunca elegir un ambiguo.

    El ID de Operandium puede diferir del ID comercial en CS. Cuando no hay
    correspondencia por ID, acepta un único nombre exacto o con sufijo.
    """
    name=_name(cliente)
    matches={account for label,account in names.items() if name and (_name(label)==name or name.startswith(_name(label)+' ') or _name(label).startswith(name+' '))}
    candidate=ids.get(str(client_id or '').strip().upper())
    if candidate:
        return candidate if not name or matches=={candidate} else None
    return next(iter(matches)) if len(matches)==1 else None
