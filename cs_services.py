"""Lectura operativa de citas importadas; no presume cobertura ni historial."""
from collections import Counter
from datetime import datetime, timezone
from cs_weekly import MX, month_bounds
from cs_daily import name_key

DONE = {'terminada', 'terminado', 'completada', 'completado', 'realizada', 'realizado'}
PENDING = {'agendada', 'agendado', 'programada', 'programado', 'tecnico asignado', 'en proceso'}


def local_date(value):
    if value is None:
        return None
    # Los registros legacy sin zona no permiten inferir una conversión.
    return value.astimezone(MX).date() if value.tzinfo else value.date()


def summarize_rows(rows, month, cut=None):
    selected = [r for r in rows if local_date(r.fecha_inicio) and local_date(r.fecha_inicio).strftime('%Y-%m') == month]
    statuses = Counter(r.estatus or 'Sin estatus' for r in selected)
    keys = Counter((name_key(r.propiedad), r.fecha_inicio, name_key(r.titulo_servicio)) for r in selected if name_key(r.propiedad))
    completed = [r for r in rows if name_key(r.estatus) in DONE and local_date(r.fecha_terminacion)
                 and local_date(r.fecha_terminacion).strftime('%Y-%m') == month
                 and (cut is None or local_date(r.fecha_terminacion) <= cut)]
    pending = [r for r in selected if name_key(r.estatus) in PENDING]
    return dict(month=month, records=len(selected), statuses=[{'status': k, 'count': v} for k,v in sorted(statuses.items())],
                completed=len(completed), pending=len(pending),
                overdue=sum(bool(cut and local_date(r.fecha_inicio) <= cut) for r in pending),
                missing_end=sum(name_key(r.estatus) in DONE and not r.fecha_terminacion for r in selected),
                duplicate_groups=sum(n > 1 for n in keys.values()),
                api_ids=sum(bool(r.zoho_appointment_id) for r in selected))


def service_snapshot(account_id, cut):
    from models import CSAppointment, CSContrato
    rows = CSAppointment.query.filter_by(account_id=account_id).all()
    previous, current, _ = month_bounds(cut)
    contract = CSContrato.query.filter_by(account_id=account_id, activo=True).first()
    return dict(comparison=compare_scope(contract.resumen if contract else {}, rows),
                captured_at=datetime.now(timezone.utc).isoformat(), total=len(rows),
                undated=sum(r.fecha_inicio is None for r in rows),
                months=[summarize_rows(rows, d.strftime('%Y-%m'), cut) for d in (previous,current)])


def compare_scope(summary, rows):
    """Cobertura por nombre en toda la historia; no acredita cumplimiento mensual."""
    scope = {name_key(r['propiedad']): r['propiedad'] for r in summary.get('filas', []) if r.get('activo')}
    observed = {name_key(r.propiedad) for r in rows if name_key(r.propiedad)}
    missing = sorted(scope[k] for k in scope.keys() - observed)
    return dict(has_scope=bool(scope), contracted=len(scope), observed=len(observed),
                matched=len(scope.keys() & observed), missing=missing,
                outside=len(observed - scope.keys()) if scope else None,
                amount=summary.get('importe_activos'), cycle=summary.get('cada_meses'),
                api_ids=sum(bool(r.zoho_appointment_id) for r in rows))


PREVIOUS_AUTO_KEYS = ('prev_expected', 'prev_done', 'prev_billed', 'prev_target')


def previous_month_data(services):
    """Datos de sistema, con pendientes explícitos en lugar de recaptura KAM.

    El feed actual no acredita cobertura de citas ni dispone de calendario
    contractual por sucursal o facturas conciliadas. No convierte falta de
    información en cero ni utiliza el importe por ciclo como meta mensual.
    """
    months = (services or {}).get('months', [])
    previous = months[0] if months else {}
    # Mostrar la evidencia disponible sin certificar cobertura ni inventar cero.
    has_records = bool(previous.get('records') or previous.get('completed'))
    completed = previous.get('completed') if has_records else None
    return {
        'prev_expected': dict(value=None, source='Contrato y calendario', owner='Administración / Comercial',
            reason='Falta validar fechas del ciclo por sucursal; la frecuencia sola no determina el mes.'),
        'prev_done': dict(value=completed, source='Citas importadas de Analytics y otras cargas', owner='Operaciones / Integraciones',
            observed=previous.get('completed'), status='Registrados' if completed is not None else 'Sin registros del mes',
            reason=('Citas terminadas con fecha de cierre en el mes. Cifra registrada; cobertura y posibles duplicados pendientes de validar.'
                    if completed is not None else 'Sin registros suficientes para calcular los servicios realizados; no se interpreta como cero.')),
        'prev_billed': dict(value=None, source='Facturas del mes', owner='Administración / Finanzas',
            reason='Falta conectar y conciliar las facturas del periodo; el precio cotizado no es facturación.'),
        'prev_target': dict(value=None, source='Condiciones comerciales y calendario', owner='Administración / Comercial',
            reason='Falta validar el importe exigible de este mes; un ciclo bimestral no se divide automáticamente entre dos.'),
    }
