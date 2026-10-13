"""Conciliación conservadora entre nombres/IDs de fuentes distintas."""
import unicodedata


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
