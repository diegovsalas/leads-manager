"""Gestión diaria y una sola fuente para tareas y reporte semanal."""
import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from extensions import db
from models import CSAccount, CSTask, CSGestion, CSNote, CSWeeklyReport
from cs_weekly import MX

SOURCES = {'agreements': 'Tarea / acuerdo', 'qbr': 'Compromiso QBR',
           'blockers': 'Bloqueo / escalamiento', 'plans': 'Plan de trabajo'}
DONE = ('Cerrado', 'Cancelado')
REAL_ACTIONS = ('Gestión','Seguimiento','Escalado','Reprogramado','Cerrado','Reabierto')


def name_key(name):
    name = unicodedata.normalize('NFKD', name or '').casefold()
    return re.sub(r'[^\w]+', ' ', ''.join(c for c in name if not unicodedata.combining(c))).strip()


def duplicate_account(name):
    key = name_key(name)
    # Todos los puntos de alta adquieren el mismo candado, incluso el legacy.
    lock = 731092817  # serializa también la generación del client_id
    db.session.execute(db.text('SELECT pg_advisory_xact_lock(:key)'), {'key': lock})
    for a in CSAccount.query.all():
        if name_key(a.nombre) == key:
            return a
    return None


def text(value, label, required=False, maximum=3000):
    value = (value or '').strip()
    if required and not value:
        raise ValueError(f'{label} es obligatorio.')
    if len(value) > maximum:
        raise ValueError(f'{label}: máximo {maximum} caracteres.')
    return value


def due_date(value, required=True):
    if not value and not required:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise ValueError('Indica una fecha compromiso válida.')


def task_status(task):
    return task.estado if task.estado == 'Cancelado' else 'Cerrado' if task.completada else task.estado or 'Pendiente'


def task_source(task):
    source = (task.detalle or {}).get('source')
    if source in SOURCES:
        return source
    return 'qbr' if 'qbr' in (task.tipo or '').casefold() else 'agreements'


def task_snapshot(task):
    extra = dict(task.detalle or {})
    extra.update(task_id=str(task.id), title=task.descripcion, owner=task.responsable,
                 due=task.fecha_limite.isoformat() if task.fecha_limite else '',
                 original_due=(task.fecha_original or task.fecha_limite).isoformat() if (task.fecha_original or task.fecha_limite) else '',
                 status=task_status(task), source=task_source(task),
                 completed_at=task.completed_at.isoformat() if task.completed_at else '')
    return extra


def record_event(account, action, result, actor, actor_id, task=None, evidence='', before=None):
    db.session.flush()
    event = CSGestion(account_id=account.id, task_id=task.id if task else None,
                      accion=action, resultado=result, respaldo=evidence,
                      actor=actor, actor_id=str(actor_id),
                      snapshot={'before': before, 'after': task_snapshot(task)} if task else {})
    db.session.add(event)
    # El historial ya existente y el scorecard también ven la gestión real.
    if action in REAL_ACTIONS:
        db.session.add(CSNote(account_id=account.id, autor=actor,
            contenido=f'{action}: {result}' + (f'\nRespaldo: {evidence}' if evidence else '')))
    return event


def new_task(account, form, actor, actor_id):
    description = text(form.get('descripcion'), 'La siguiente acción', True)
    owner = text(form.get('responsable') or actor, 'El responsable', True, 120)
    due = due_date(form.get('fecha_limite'))
    source = form.get('source') or ('qbr' if 'qbr' in (form.get('tipo') or '').casefold() else 'agreements')
    if source not in SOURCES:
        raise ValueError('Selecciona un tipo de compromiso válido.')
    detail = {'source': source, 'next': description}
    if source == 'blockers':
        detail['support'] = text(form.get('apoyo'), 'El apoyo requerido', True)
    if source == 'plans':
        detail['scope'] = text(form.get('alcance'), 'El alcance', True)
        detail['progress'] = 0
    legacy_type = form.get('tipo')
    kind = legacy_type if legacy_type in ('check-in','QBR','Junta','Catch-up','Reunión','Seguimiento') else 'QBR' if source == 'qbr' else SOURCES[source]
    task = CSTask(account_id=account.id, tipo=kind, descripcion=description,
                  responsable=owner, fecha_limite=due, fecha_original=due,
                  estado='Pendiente', detalle=detail)
    db.session.add(task)
    record_event(account, 'Programado', description, actor, actor_id, task)
    return task


def change_task(account, task, form, actor, actor_id):
    action = form.get('accion')
    if action not in ('seguimiento', 'escalar', 'cerrar', 'cancelar', 'reabrir'):
        raise ValueError('Selecciona una acción válida.')
    result = text(form.get('resultado'), 'El resultado o motivo', True)
    evidence = text(form.get('respaldo'), 'El respaldo', action == 'cerrar')
    if action in ('cerrar', 'cancelar') and len(result) < 8:
        raise ValueError('Describe el resultado o motivo de la acción.')
    if action == 'cerrar' and len(evidence) < 8:
        raise ValueError('Indica un respaldo verificable: folio, confirmación del cliente o documento.')
    before = task_snapshot(task)
    if task_status(task) in DONE and action != 'reabrir':
        raise ValueError('El compromiso ya está cerrado; primero reábrelo con un motivo.')
    if action == 'reabrir' and task_status(task) not in DONE:
        raise ValueError('El compromiso ya está abierto.')
    if action in ('cerrar', 'cancelar'):
        task.estado = 'Cerrado' if action == 'cerrar' else 'Cancelado'
        task.completada = True
        task.completed_at = datetime.now(timezone.utc)
    else:
        due = due_date(form.get('fecha_limite') or (task.fecha_limite.isoformat() if task.fecha_limite else None))
        owner = text(form.get('responsable') or task.responsable, 'El responsable', True, 120)
        next_step = text(form.get('siguiente_paso'), 'El siguiente paso', True)
        if not task.fecha_original:
            task.fecha_original = task.fecha_limite or due
        task.fecha_limite, task.responsable = due, owner
        task.estado = 'Escalado' if action == 'escalar' else 'En proceso'
        task.completada, task.completed_at = False, None
        extra = dict(task.detalle or {})
        extra['next'] = next_step
        if action == 'escalar':
            extra['support'] = text(form.get('apoyo'), 'El apoyo requerido', True)
        if task_source(task) == 'plans' and form.get('avance'):
            try:
                progress = int(form['avance'])
            except ValueError:
                raise ValueError('El avance debe ser un entero entre 0 y 100.')
            if not 0 <= progress <= 100:
                raise ValueError('El avance debe estar entre 0 y 100.')
            extra['progress'] = progress
        task.detalle = extra
    task.updated_at = datetime.now(timezone.utc)
    action_label = {'seguimiento': 'Seguimiento', 'escalar': 'Escalado',
                    'cerrar': 'Cerrado', 'cancelar': 'Cancelado', 'reabrir': 'Reabierto'}[action]
    if before['due'] and before['due'] != (task.fecha_limite.isoformat() if task.fecha_limite else ''):
        action_label = 'Reprogramado' if action == 'seguimiento' else action_label
    return record_event(account, action_label, result, actor, actor_id, task, evidence, before)


def weekly_tasks(account_id, cut):
    """Reconstruye compromisos al corte sin cambiar tareas ni perder su ID."""
    end = datetime.combine(cut + timedelta(days=1), datetime.min.time(), MX)
    start = datetime.combine(cut - timedelta(days=4), datetime.min.time(), MX)
    tasks = CSTask.query.filter(CSTask.account_id == account_id, CSTask.created_at < end).all()
    events = CSGestion.query.filter(CSGestion.account_id == account_id, CSGestion.task_id.in_([t.id for t in tasks])).order_by(CSGestion.created_at, CSGestion.id).all()
    by_task = {}
    for event in events:
        by_task.setdefault(str(event.task_id), []).append(event)
    output = {source: [] for source in SOURCES}
    for task in tasks:
        history = by_task.get(str(task.id), [])
        past = [e for e in history if e.created_at < end]
        future = [e for e in history if e.created_at >= end]
        state = past[-1].snapshot.get('after') if past else future[0].snapshot.get('before') if future else task_snapshot(task)
        if not state:
            continue
        state = dict(state)
        closed_at = state.get('completed_at')
        if state.get('status') in DONE and closed_at:
            completed = datetime.fromisoformat(closed_at)
            if completed < start:
                continue
        elif state.get('status') == 'Cerrado' and not closed_at:
            state['status'] = 'Cierre sin fecha'
        output[state.get('source', task_source(task))].append(state)
    activity = CSGestion.query.filter(CSGestion.account_id == account_id,
        CSGestion.created_at >= start, CSGestion.created_at < end,
        CSGestion.accion.in_(REAL_ACTIONS)).order_by(CSGestion.created_at).all()
    output['activity'] = [dict(date=e.created_at.astimezone(MX).strftime('%d/%m %H:%M'),
        action=e.accion, result=e.resultado, actor=e.actor, evidence=e.respaldo) for e in activity]
    return output


def import_legacy(account, actor, actor_id):
    """Importación explícita e idempotente del último reporte con filas manuales."""
    report = CSWeeklyReport.query.filter_by(account_id=account.id).order_by(CSWeeklyReport.cut_date.desc()).first()
    if not report:
        return 0
    existing = {t.detalle.get('legacy_key') for t in CSTask.query.filter_by(account_id=account.id).all() if t.detalle}
    count = 0
    for source in SOURCES:
        for row in report.payload.get(source, []):
            if row.get('task_id') or row.get('status') in DONE:
                continue
            key = hashlib.sha256(f"{source}|{name_key(row.get('title'))}".encode()).hexdigest()
            if key in existing:
                continue
            due = due_date(row.get('due'), required=False)
            task = CSTask(account_id=account.id, tipo='QBR' if source == 'qbr' else SOURCES[source], descripcion=row['title'],
                responsable=row.get('owner') or (account.kam.nombre if account.kam else actor),
                fecha_limite=due, fecha_original=due, estado='Pendiente',
                detalle={**row, 'source': source, 'legacy_key': key})
            db.session.add(task)
            record_event(account, 'Importado', f'Compromiso del corte {report.cut_date}: {row["title"]}', actor, actor_id, task)
            existing.add(key)
            count += 1
    return count
