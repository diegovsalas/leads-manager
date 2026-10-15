"""Lectura operativa de citas importadas; no presume cobertura ni historial."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
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
    from models import CSAppointment, CSContrato, CSInvoice, SavioInvoice
    from extensions import db
    rows = CSAppointment.query.filter_by(account_id=account_id).all()
    previous, current, _ = month_bounds(cut)
    contract = CSContrato.query.filter_by(account_id=account_id, activo=True).first()
    invoices = db.session.query(CSInvoice, SavioInvoice).outerjoin(
        SavioInvoice, db.cast(CSInvoice.savio_invoice_id, db.String) == SavioInvoice.id
    ).filter(CSInvoice.account_id == account_id).all()
    return dict(billing={'months': [summarize_invoices(invoices, d.strftime('%Y-%m'), cut) for d in (previous, current)]},
                comparison=compare_scope(contract.resumen if contract else {}, rows),
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
    billing = (services or {}).get('billing', {}).get('months', [])
    previous_billing = billing[0] if billing else {}
    return {
        'prev_expected': dict(value=None, source='Contrato y calendario', owner='Administración / Comercial',
            reason='Falta validar fechas del ciclo por sucursal; la frecuencia sola no determina el mes.'),
        'prev_done': dict(value=completed, source='Citas importadas de Analytics y otras cargas', owner='Operaciones / Integraciones',
            observed=previous.get('completed'), status='Registrados' if completed is not None else 'Sin registros del mes',
            reason=('Citas terminadas con fecha de cierre en el mes. Cifra registrada; cobertura y posibles duplicados pendientes de validar.'
                    if completed is not None else 'Sin registros suficientes para calcular los servicios realizados; no se interpreta como cero.')),
        'prev_billed': billing_metric(previous_billing),
        'prev_target': dict(value=None, source='Condiciones comerciales y calendario', owner='Administración / Comercial',
            reason='Falta validar el importe exigible de este mes; un ciclo bimestral no se divide automáticamente entre dos.'),
    }


CURRENT_AUTO_KEYS = ('done', 'scheduled', 'billed')


def summarize_invoices(rows, month, cut):
    """Facturación observada con fecha de emisión y moneda de Savio verificables.

    fecha_cobro de las cargas manuales puede ser fecha de cobro; no se usa
    como emisión. Se excluyen canceladas, monedas distintas y estados no válidos.
    """
    included=[]
    omitted=0
    for invoice, source in rows:
        if source is None:
            if invoice.fecha_cobro and invoice.fecha_cobro.strftime('%Y-%m')==month and invoice.fecha_cobro<=cut:
                omitted+=1
            continue
        if not source.date or source.date.strftime('%Y-%m')!=month or source.date>cut:
            continue
        raw=source.raw_data or {}
        if str(raw.get('currency') or '').upper()!='MXN' or name_key(source.status)!='valid':
            omitted+=1
            continue
        if invoice.total is None or source.amount is None or Decimal(str(invoice.total))!=Decimal(str(source.amount)):
            omitted+=1
            continue
        included.append(invoice)
    return dict(month=month,value=float(sum((Decimal(str(i.total)) for i in included),Decimal('0'))) if included else None,
                count=len(included),omitted=omitted)


def billing_metric(billing):
    value=billing.get('value')
    reason=(f"{billing.get('count',0)} facturas vigentes en MXN, por fecha de emisión hasta el corte. Total con impuestos; no es cobranza."
            if value is not None else 'Sin facturas vigentes en MXN con fecha de emisión verificada para este periodo. Administración debe revisar la integración.')
    if billing.get('omitted'):
        reason+=f" {billing['omitted']} registros excluidos por estado, moneda o datos sin verificar."
    return dict(value=value,source='Facturas registradas de Savio',owner='Administración / Finanzas',
                status='Registrado' if value is not None else 'Sin fuente verificada',reason=reason)


def current_month_data(services):
    months=(services or {}).get('months',[])
    current=months[1] if len(months)>1 else {}
    has_records=bool(current.get('records') or current.get('completed'))
    billing=(services or {}).get('billing',{}).get('months',[])
    return {
        'done':dict(value=current.get('completed') if has_records else None,
            source='Citas importadas de Analytics y otras cargas',owner='Operaciones / Integraciones',status='Registrados',
            reason='Citas terminadas con fecha de cierre dentro del mes y hasta el corte. Cobertura y posibles duplicados por validar.'),
        'scheduled':dict(value=current.get('pending') if has_records else None,
            source='Agenda importada de Analytics y otras cargas',owner='Operaciones / Integraciones',status='Estatus actual',
            reason=f"Citas pendientes fechadas dentro del mes según su estatus actual; {current.get('overdue',0)} con fecha vencida al corte. No hay historial para reconstruir el estatus del viernes." if has_records else 'Sin citas del mes para consultar pendientes; no se interpreta como cero.'),
        'billed':billing_metric(billing[1] if len(billing)>1 else {}),
    }
