"""Contrato y cálculos del corte semanal AAA; los faltantes nunca son ceros."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

MX = ZoneInfo('America/Monterrey')
MONTHS = ('Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
          'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre')
METRICS = (
    ('prev_expected', 'Servicios comprometidos', 'integer', 'previous'),
    ('prev_done', 'Servicios realizados', 'integer', 'previous'),
    ('prev_billed', 'Facturado del mes (MXN)', 'money', 'previous'),
    ('prev_target', 'Meta de facturación (MXN)', 'money', 'previous'),
    ('expected', 'Servicios comprometidos del mes', 'integer', 'current'),
    ('done', 'Servicios realizados al corte', 'integer', 'current'),
    ('scheduled', 'Pendientes agendados dentro del mes', 'integer', 'current'),
    ('forecast', 'Servicios previstos al cierre', 'integer', 'current'),
    ('billed', 'Facturado al corte (MXN)', 'money', 'current'),
    ('target', 'Meta de facturación (MXN)', 'money', 'current'),
    ('billing_forecast', 'Facturación prevista al cierre (MXN)', 'money', 'current'),
    ('closing_date', 'Fecha estimada de cierre', 'date', 'current'),
    ('previous_comment', 'Qué salió bien / qué faltó y por qué', 'text', 'previous'),
    ('current_comment', 'Avances y pendientes de este mes', 'text', 'current'),
    ('next_qbr', 'Próximo QBR', 'date', 'qbr'),
    ('qbr_attendees', 'Asistentes del cliente al próximo QBR', 'text', 'qbr'),
)
# Las secciones tienen filas tipadas; permiten cualquier número de compromisos.
SECTIONS = (
    ('blockers', '03 · Stopers y retos', (
        ('title', 'Qué frena el servicio', 'text'), ('impact', 'Impacto (servicios / MXN)', 'text'),
        ('cause', 'Causa', 'text'), ('action', 'Acción en curso', 'text'),
        ('support', 'Apoyo o decisión requerida', 'text'), ('owner', 'Responsable', 'text'),
        ('due', 'Fecha compromiso', 'date'), ('status', 'Estatus', ('Pendiente', 'En proceso', 'Cerrado')))),
    ('qbr', '04 · Compromisos del QBR', (
        ('title', 'Compromiso acordado', 'text'), ('owner', 'Responsable', 'text'),
        ('due', 'Fecha compromiso', 'date'),
        ('status', 'Estatus', ('Pendiente', 'En proceso', 'Reprogramado', 'Cerrado')),
        ('next', 'Siguiente paso / motivo de reprogramación', 'text'))),
    ('plans', '06 · Planes de trabajo', (
        ('title', 'Nombre del plan', 'text'), ('scope', 'Alcance y sucursales', 'text'),
        ('status', 'Etapa', ('Nuevo', 'En curso', 'Proyectado', 'Cerrado')),
        ('start', 'Inicio', 'date'), ('due', 'Fin previsto', 'date'),
        ('progress', 'Avance (%)', 'percent'), ('value', 'Valor (MXN)', 'money'),
        ('dependency', 'Dependencia / bloqueo', 'text'), ('next', 'Acción de esta semana', 'text'),
        ('owner', 'Responsable', 'text'))),
    ('agreements', 'Acuerdos de la sesión', (
        ('title', 'Acción acordada', 'text'), ('owner', 'Responsable', 'text'),
        ('due', 'Fecha compromiso', 'date'),
        ('status', 'Estatus', ('Pendiente', 'En proceso', 'Cerrado')))),
)


def latest_cut(today=None):
    today = today or datetime.now(MX).date()
    # En viernes todavía no cerró el día; el último corte completo es el anterior.
    return today - timedelta(days=(today.weekday() - 4) % 7 or 7)


def parse_cut(value, today=None):
    if not value:
        return latest_cut(today)
    try:
        cut = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError('La fecha de corte no es válida.')
    if cut.weekday() != 4 or cut > latest_cut(today):
        raise ValueError('Selecciona un viernes de corte ya concluido.')
    return cut


def month_bounds(cut):
    current = cut.replace(day=1)
    previous = (current - timedelta(days=1)).replace(day=1)
    end = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return previous, current, end


def _value(value, kind, label):
    if value is None or value == '':
        return None if kind in ('integer', 'money', 'percent') else ''
    if isinstance(kind, tuple):
        if value not in kind:
            raise ValueError(f'{label}: selecciona una opción válida.')
        return value
    if kind == 'date':
        try:
            return date.fromisoformat(str(value)).isoformat()
        except ValueError:
            raise ValueError(f'{label}: fecha no válida.')
    if kind in ('integer', 'money', 'percent'):
        try:
            n = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError(f'{label}: usa un número válido.')
        if not n.is_finite() or n < 0 or n > 10**12:
            raise ValueError(f'{label}: el valor está fuera de rango.')
        if kind == 'integer' and n != n.to_integral_value():
            raise ValueError(f'{label}: usa un número entero.')
        if kind == 'percent' and n > 100:
            raise ValueError(f'{label}: el avance debe estar entre 0 y 100.')
        return int(n) if kind == 'integer' else float(n)
    if not isinstance(value, str) or len(value) > 3000:
        raise ValueError(f'{label}: máximo 3,000 caracteres.')
    return value.strip()


def validate_payload(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('metrics', {}), dict):
        raise ValueError('El reporte no tiene un formato válido.')
    m = {key: _value(raw.get('metrics', {}).get(key), kind, label)
         for key, label, kind, _ in METRICS}
    for prefix in ('prev_', ''):
        expected, done = m[prefix + 'expected'], m[prefix + 'done']
        if expected is not None and done is not None and done > expected:
            raise ValueError('Los servicios realizados no pueden superar los comprometidos; revisa el alcance.')
    if all(m[k] is not None for k in ('expected', 'done', 'scheduled')):
        if m['done'] + m['scheduled'] > m['expected']:
            raise ValueError('Los realizados más los pendientes agendados superan los servicios comprometidos.')
    if m['forecast'] is not None and m['done'] is not None and m['forecast'] < m['done']:
        raise ValueError('La proyección de cierre no puede ser menor a los servicios ya realizados.')
    out = {'metrics': m}
    for key, label, fields in SECTIONS:
        rows = raw.get(key, [])
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError(f'{label}: máximo 100 registros.')
        out[key] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f'{label}: registro no válido.')
            clean = {k: _value(row.get(k), kind, name) for k, name, kind in fields}
            if not clean['title']:
                raise ValueError(f'{label}: cada registro necesita una descripción.')
            if not clean.get('status'):
                raise ValueError(f'{label}: cada registro necesita un estatus.')
            if key == 'plans' and clean['start'] and clean['due'] and clean['start'] > clean['due']:
                raise ValueError('El fin del plan no puede ser anterior a su inicio.')
            out[key].append(clean)
    return out


def summarize(payload, cut):
    m = payload.get('metrics', {})
    expected, done, scheduled, forecast = (m.get(k) for k in ('expected', 'done', 'scheduled', 'forecast'))
    pending = max(expected - done, 0) if expected is not None and done is not None else None
    unscheduled = max(pending - scheduled, 0) if pending is not None and scheduled is not None else None
    active_blockers = [r for r in payload.get('blockers', []) if r.get('status') not in ('Cerrado', 'Cancelado')]
    if expected is None or done is None or forecast is None or scheduled is None:
        status, tone = 'Sin confirmar', 'neutral'
    elif expected == 0:
        status, tone = 'Sin servicios', 'neutral'
    elif forecast < expected:
        status, tone = 'Atrasado', 'danger'
    elif unscheduled or active_blockers:
        status, tone = 'En riesgo', 'warn'
    else:
        status, tone = 'En tiempo', 'ok'
    commitments = [r for key in ('qbr', 'agreements') for r in payload.get(key, [])]
    overdue = [r for r in commitments if r.get('status') not in ('Cerrado', 'Cancelado')
               and r.get('due') and r['due'] < cut.isoformat()]
    actions = [r for key in ('blockers', 'qbr', 'agreements', 'plans')
               for r in payload.get(key, []) if r.get('status') not in ('Cerrado', 'Cancelado')]
    actions.sort(key=lambda r: r.get('due') or '9999-12-31')
    prev_expected, prev_done = m.get('prev_expected'), m.get('prev_done')
    pct = lambda n, d: round(n / d * 100, 1) if n is not None and d else None
    return dict(status=status, tone=tone, pending=pending, unscheduled=unscheduled,
                progress=pct(done, expected), previous=pct(prev_done, prev_expected),
                projection=pct(forecast, expected), overdue=len(overdue),
                blockers=len(active_blockers), blocker=active_blockers[0] if active_blockers else None,
                next_action=actions[0] if actions else None)
