"""Pantallas de trabajo diario; tareas compartidas con el CS existente."""
import uuid
import hashlib
import io
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, send_file
from sqlalchemy import func
from extensions import db
from models import CSAccount, CSContacto, CSTask, CSGestion, CSIncidencia, UserCRM, RolCRM, CSWeeklyReport, CSContrato
from blueprints.cs import (_ctx, _is_kam, _is_cs_admin, _get_kams, _get_cs_account,
    _current_kam_id, _can_edit_account, require_cs_analisis, require_cs_admin, require_cs_account_access)
from cs_weekly import MX
from cs_daily import (SOURCES, DONE, REAL_ACTIONS, name_key, duplicate_account, text, due_date,
    new_task, change_task, record_event, task_status, import_legacy)

cs_daily_bp = Blueprint('cs_daily', __name__, template_folder='../templates/cs')


def scoped_accounts():
    q = CSAccount.query.filter(CSAccount.en_due_diligence.is_not(True))
    if _is_kam():
        q = q.filter(CSAccount.kam_id == _current_kam_id())
    elif request.args.get('kam'):
        try:
            q = q.filter(CSAccount.kam_id == uuid.UUID(request.args['kam']))
        except ValueError:
            raise ValueError('KAM no válido.')
    return q


def actor():
    return session.get('user_nombre') or 'Usuario CS', session.get('user_id', '')


def today_view():
    try:
        accounts = scoped_accounts().order_by(CSAccount.nombre).all()
    except ValueError as e:
        return str(e), 400
    ids = [a.id for a in accounts]
    today = datetime.now(MX).date()
    tasks = CSTask.query.filter(CSTask.account_id.in_(ids), CSTask.completada.is_(False)).order_by(CSTask.fecha_limite.asc().nullsfirst()).all()
    incidents = CSIncidencia.query.filter(CSIncidencia.account_id.in_(ids), CSIncidencia.status != 'Resuelta').order_by(CSIncidencia.fecha_compromiso.asc().nullsfirst()).all()
    latest = dict(db.session.query(CSGestion.task_id, func.max(CSGestion.created_at)).filter(
        CSGestion.task_id.in_([t.id for t in tasks]), CSGestion.accion.in_(REAL_ACTIONS)).group_by(CSGestion.task_id).all())
    latest = {k:v.astimezone(MX) for k,v in latest.items()}
    view = request.args.get('ver', 'prioridad')
    shown = [t for t in tasks if view == 'todas' or
             (view == 'vencidas' and t.fecha_limite and t.fecha_limite < today) or
             (view == 'hoy' and t.fecha_limite == today) or
             (view == 'bloqueos' and (task_status(t) == 'Escalado' or (t.detalle or {}).get('source') == 'blockers')) or
             (view == 'prioridad' and (not t.fecha_limite or t.fecha_limite <= today or task_status(t) == 'Escalado' or (t.detalle or {}).get('source') == 'blockers'))]
    account_map = {a.id:a for a in accounts}
    stats = dict(overdue=sum(bool(t.fecha_limite and t.fecha_limite < today) for t in tasks),
        due=sum(t.fecha_limite == today for t in tasks), incidents=len(incidents),
        blocked=sum(task_status(t) == 'Escalado' or (t.detalle or {}).get('source') == 'blockers' for t in tasks))
    since = datetime.now(MX) - timedelta(days=7)
    closed = CSTask.query.filter(CSTask.account_id.in_(ids), CSTask.estado == 'Cerrado', CSTask.completed_at >= since).all()
    activity = CSGestion.query.filter(CSGestion.account_id.in_(ids), CSGestion.created_at >= since,
        CSGestion.accion.in_(REAL_ACTIONS)).all()
    team = []
    if not _is_kam():
        for kam in _get_kams():
            aid = {a.id for a in accounts if a.kam_id == kam.id}
            kt = [t for t in tasks if t.account_id in aid]
            kc = [t for t in closed if t.account_id in aid]
            team.append(dict(kam=kam, accounts=len(aid), overdue=sum(bool(t.fecha_limite and t.fecha_limite < today) for t in kt),
                touched=len({e.account_id for e in activity if e.account_id in aid}), closed=len(kc),
                on_time=sum(bool(t.fecha_original and t.completed_at.astimezone(MX).date() <= t.fecha_original) for t in kc)))
    legacy = []
    reports = CSWeeklyReport.query.filter(CSWeeklyReport.account_id.in_(ids)).order_by(CSWeeklyReport.cut_date.desc()).all()
    imported = {t.detalle.get('legacy_key') for t in CSTask.query.filter(CSTask.account_id.in_(ids)).all() if t.detalle}
    seen = set()
    for report in reports:
        if report.account_id in seen:
            continue
        seen.add(report.account_id)
        if any(row.get('status') not in DONE and not row.get('task_id') and
               hashlib.sha256(f"{source}|{name_key(row.get('title'))}".encode()).hexdigest() not in imported
               for source in SOURCES for row in report.payload.get(source, [])):
            # El botón permite importar una sola vez, con historial.
            legacy.append(account_map[report.account_id])
    return render_template('cs_hoy.html', accounts=accounts, tasks=shown,
        incidents=incidents, account_map=account_map, today=today, stats=stats,
        latest=latest, team=team, view=view, legacy=legacy, task_status=task_status,
        kams=_get_kams(), **_ctx())


@cs_daily_bp.route('/cuentas')
@require_cs_analisis
def accounts_view():
    try:
        q = scoped_accounts()
    except ValueError as e:
        return str(e), 400
    search = (request.args.get('q') or '').strip()
    if search:
        q = q.filter(CSAccount.nombre.ilike('%' + search.replace('%', r'\%').replace('_', r'\_') + '%'))
    accounts = q.order_by(CSAccount.nombre).all()
    ids = [a.id for a in accounts]
    counts = dict(db.session.query(CSTask.account_id, func.count(CSTask.id)).filter(
        CSTask.account_id.in_(ids), CSTask.completada.is_(False)).group_by(CSTask.account_id).all())
    contacts = CSContacto.query.filter(CSContacto.account_id.in_(ids), CSContacto.is_owner.is_(True)).all()
    by_contact = {}
    for c in contacts:
        by_contact.setdefault(c.account_id, c)
    return render_template('cs_cuentas_diarias.html', accounts=accounts, counts=counts,
        contacts=by_contact, search=search, kams=_get_kams(), **_ctx())


@cs_daily_bp.route('/cuentas/buscar')
@require_cs_analisis
def search_accounts():
    search = name_key(request.args.get('q'))
    if len(search) < 3:
        return jsonify([])
    rows = []
    # Resultado mínimo para evitar duplicados: no expone datos comerciales ni contacto ajeno.
    for account in CSAccount.query.filter(CSAccount.en_due_diligence.is_not(True)).order_by(CSAccount.nombre).all():
        if search in name_key(account.nombre):
            rows.append(dict(name=account.nombre, kam=account.kam.nombre if account.kam else 'Sin KAM',
                             url=url_for('cs_daily.account_work', account_id=account.id) if _can_edit_account(account) else None))
    return jsonify(rows[:10])


@cs_daily_bp.route('/cuentas/agregar', methods=['GET','POST'])
@require_cs_analisis
def add_account():
    error = None
    status = 200
    if request.method == 'POST':
        try:
            name = text(request.form.get('nombre'), 'El nombre de la cuenta', True, 200)
            if not name_key(name):
                raise ValueError('Indica un nombre de cuenta válido.')
            unit = request.form.get('unidad')
            if unit not in ('Aromatex','Pestex','Weldex','Nexo'):
                raise ValueError('Selecciona la unidad de negocio.')
            contact = text(request.form.get('contacto'), 'El contacto', True, 200)
            phone = text(request.form.get('telefono'), 'El teléfono', maximum=30)
            email = text(request.form.get('correo'), 'El correo', maximum=200)
            if not phone and not email:
                raise ValueError('Indica teléfono o correo del contacto.')
            if email and ('@' not in email or ' ' in email):
                raise ValueError('El correo del contacto no es válido.')
            kam_id = _current_kam_id() if _is_kam() else request.form.get('kam_id')
            kam = db.session.get(UserCRM, uuid.UUID(str(kam_id)))
            if not kam or not kam.activo or kam.rol not in (RolCRM.KAM, RolCRM.SUPER_ADMIN):
                raise ValueError('Selecciona un KAM activo.')
            duplicate = duplicate_account(name)
            if duplicate:
                raise ValueError(f'La cuenta ya existe como «{duplicate.nombre}». Revisa la asignación con Dirección.')
            from blueprints.cs import _genera_ticket_token
            account = CSAccount(nombre=name, kam_id=kam.id, unidades_contratadas=unit.upper(),
                datos_por_validar=True, ticket_token=_genera_ticket_token())
            db.session.add(account); db.session.flush()
            db.session.add(CSContacto(account_id=account.id,nombre=contact,telefono=phone,correo=email,is_owner=True))
            who, uid = actor()
            record_event(account,'Alta','Cuenta incorporada para gestión; datos pendientes de validación.',who,uid)
            db.session.commit()
        except (ValueError, TypeError) as e:
            db.session.rollback(); error, status = str(e), 400
        else:
            flash('Cuenta agregada. Ya puedes registrar gestión; Administración validará sus datos.', 'success')
            return redirect(url_for('cs_daily.account_work',account_id=account.id))
    return render_template('cs_agregar_cuenta.html', error=error, kams=_get_kams(), **_ctx()), status


@cs_daily_bp.route('/cuentas/<uuid:account_id>/validar',methods=['GET','POST'])
@require_cs_admin
@require_cs_account_access
def validate_account(account_id):
    account = _get_cs_account(account_id)
    error = None
    if request.method == 'POST':
        try:
            code = text(request.form.get('client_id'), 'El identificador de cliente', True, 10).upper()
            validation = text(request.form.get('resultado'), 'La validación realizada', True)
            if CSAccount.query.filter(CSAccount.client_id == code, CSAccount.id != account.id).first():
                raise ValueError('Ese identificador ya pertenece a otra cuenta.')
            account.client_id, account.datos_por_validar = code, False
            who, uid = actor()
            record_event(account,'Validación',validation,who,uid)
            db.session.commit()
            flash('Datos de la cuenta validados.', 'success')
            return redirect(url_for('cs_daily.account_work',account_id=account.id))
        except ValueError as e:
            db.session.rollback(); error = str(e)
    return render_template('cs_validar_cuenta.html',account=account,error=error,**_ctx()), 400 if error else 200


@cs_daily_bp.route('/cuentas/<uuid:account_id>')
@require_cs_account_access
def account_work(account_id):
    account = _get_cs_account(account_id)
    tasks = CSTask.query.filter_by(account_id=account_id).order_by(CSTask.completada, CSTask.fecha_limite.asc().nullsfirst()).all()
    events = CSGestion.query.filter_by(account_id=account_id).order_by(CSGestion.created_at.desc()).limit(50).all()
    for e in events:
        e.local_date = e.created_at.astimezone(MX)
    from cs_services import service_snapshot
    services = service_snapshot(account_id, datetime.now(MX).date())
    return render_template('cs_trabajo_cuenta.html', services=services, account=account,tasks=tasks,events=events,
        task_status=task_status, today=datetime.now(MX).date(), can_validate=_is_cs_admin(), contract=CSContrato.query.filter_by(account_id=account_id,activo=True).first(), **_ctx())


@cs_daily_bp.route('/cuentas/<uuid:account_id>/gestion', methods=['GET','POST'])
@require_cs_account_access
def manage(account_id):
    account = _get_cs_account(account_id)
    task_id = request.args.get('tarea')
    task = None
    if task_id:
        try:
            task = db.session.execute(db.select(CSTask).where(CSTask.id == uuid.UUID(task_id)).with_for_update()).scalar_one_or_none() if request.method == 'POST' else db.session.get(CSTask, uuid.UUID(task_id))
        except ValueError:
            return 'Tarea no válida.', 400
        if not task or task.account_id != account.id:
            return 'Tarea no encontrada.', 404
    mode = request.args.get('modo') or ('seguimiento' if task else 'gestion')
    allowed = ('seguimiento','cerrar','cancelar','escalar','reabrir') if task else ('gestion','programar')
    if mode not in allowed:
        return 'Acción no válida.',400
    error, code = None, 200
    if request.method == 'POST':
        who, uid = actor()
        try:
            if task:
                if request.form.get('version','') != (task.updated_at.isoformat() if task.updated_at else ''):
                    return 'La tarea cambió. Recarga antes de guardar.',409
                form = request.form.to_dict(); form['accion']=mode
                change_task(account,task,form,who,uid)
            elif mode == 'programar':
                new_task(account,request.form,who,uid)
            else:
                result = text(request.form.get('resultado'),'La gestión y su resultado',True)
                evidence = text(request.form.get('respaldo'),'El respaldo')
                record_event(account,'Gestión',result,who,uid,evidence=evidence)
                next_step = (request.form.get('siguiente_paso') or '').strip()
                if next_step:
                    form = request.form.to_dict();form['descripcion']=next_step
                    new_task(account,form,who,uid)
            db.session.commit()
        except ValueError as e:
            db.session.rollback(); error, code = str(e),400
        else:
            flash('Gestión guardada y pendientes actualizados.', 'success')
            return redirect(url_for('cs_daily.account_work',account_id=account.id))
    return render_template('cs_registrar_gestion.html',account=account,task=task,mode=mode,
        sources=SOURCES,error=error, default_owner=account.kam.nombre if account.kam else actor()[0],
        task_status=task_status, **_ctx()), code


@cs_daily_bp.route('/cuentas/<uuid:account_id>/importar-compromisos',methods=['POST'])
@require_cs_account_access
def import_commitments(account_id):
    account = _get_cs_account(account_id)
    # Serializa dos importaciones de la misma cuenta.
    db.session.execute(db.select(CSAccount).where(CSAccount.id==account.id).with_for_update()).scalar_one()
    who, uid = actor()
    count = import_legacy(account,who,uid)
    db.session.commit()
    flash(f'{count} compromisos incorporados a la gestión diaria.', 'success')
    return redirect(url_for('cs_daily.account_work',account_id=account.id))


@cs_daily_bp.route('/cuentas/<uuid:account_id>/contratos',methods=['GET','POST'])
@require_cs_account_access
def contracts(account_id):
    from cs_contracts import prepare_file, validity, MAX_BYTES
    from cs_daily import name_key
    account=_get_cs_account(account_id)
    error=None
    if request.method=='POST':
        request.max_content_length=MAX_BYTES+131072
        try:
            client_name=account.nombre[:-8].strip() if name_key(account.nombre).endswith(' ejemplo') else account.nombre
            filename,mime,data,digest,summary=prepare_file(request.files.get('archivo'),account_name=client_name)
            start,end=validity(request.form)
            expected=name_key(account.nombre)
            if expected.endswith(' ejemplo'):expected=expected[:-8].strip()
            if summary and name_key(summary['cliente'])!=expected:
                raise ValueError('El cliente del CSV no coincide con esta cuenta. Abre la cuenta correcta antes de cargarlo.')
            # El candado de la cuenta serializa cambios de versión activa.
            db.session.execute(db.select(CSAccount.id).where(CSAccount.id==account_id).with_for_update())
            if CSContrato.query.filter_by(account_id=account_id,sha256=digest).first():
                raise ValueError('Este archivo ya fue cargado. Puedes activar la versión existente.')
            active=request.form.get('activo')=='1'
            if active:
                CSContrato.query.filter_by(account_id=account_id,activo=True).update({CSContrato.activo:False},synchronize_session=False)
            who,uid=actor()
            contract=CSContrato(account_id=account_id,nombre=filename,mimetype=mime,contenido=data,
                sha256=digest,bytes=len(data),activo=active,inicio=start,fin=end,resumen=summary,created_by=who,actor_id=uid)
            db.session.add(contract);db.session.flush()
            event=record_event(account,'Contrato cargado',f'Archivo: {filename}. '+('Marcado como contrato activo.' if active else 'Versión de referencia.'),who,uid)
            event.snapshot={'contract_id':str(contract.id),'sha256':digest,'activo':active}
            db.session.commit()
            flash('Archivo cargado. Se conserva el original y las versiones anteriores.','success')
            return redirect(url_for('cs_daily.contracts',account_id=account_id))
        except ValueError as e:
            db.session.rollback();error=str(e)
    versions=CSContrato.query.filter_by(account_id=account_id).order_by(CSContrato.created_at.desc()).all()
    return render_template('cs_contratos.html',account=account,contracts=versions,error=error,
        today=datetime.now(MX).date(),**_ctx()),400 if error else 200


@cs_daily_bp.route('/cuentas/<uuid:account_id>/contratos/<uuid:contract_id>/descargar')
@require_cs_account_access
def download_contract(account_id,contract_id):
    contract=CSContrato.query.filter_by(account_id=account_id,id=contract_id).first()
    if not contract:return 'Contrato no encontrado.',404
    response=send_file(io.BytesIO(contract.contenido),as_attachment=True,
        download_name=contract.nombre,mimetype=contract.mimetype,max_age=0)
    response.headers['Cache-Control']='private, no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    return response


@cs_daily_bp.route('/cuentas/<uuid:account_id>/contratos/<uuid:contract_id>/activar',methods=['POST'])
@require_cs_account_access
def activate_contract(account_id,contract_id):
    db.session.execute(db.select(CSAccount.id).where(CSAccount.id==account_id).with_for_update())
    contract=CSContrato.query.filter_by(account_id=account_id,id=contract_id).first()
    if not contract:return 'Contrato no encontrado.',404
    CSContrato.query.filter_by(account_id=account_id,activo=True).filter(CSContrato.id != contract.id).update({CSContrato.activo:False},synchronize_session=False)
    contract.activo=True
    who,uid=actor()
    event=record_event(_get_cs_account(account_id),'Contrato activo',f'Versión activa: {contract.nombre}.',who,uid)
    event.snapshot={'contract_id':str(contract.id),'sha256':contract.sha256}
    db.session.commit()
    flash('Versión activa actualizada; el historial se conserva.','success')
    return redirect(url_for('cs_daily.contracts',account_id=account_id))


@cs_daily_bp.route('/cuentas/<uuid:account_id>/servicios')
@require_cs_account_access
def services_view(account_id):
    from models import CSAppointment
    from cs_services import local_date
    from datetime import date
    account = _get_cs_account(account_id)
    month = request.args.get('mes', '').strip()
    status = request.args.get('estatus', '').strip()
    try:
        if month and date.fromisoformat(month + '-01').strftime('%Y-%m') != month:
            raise ValueError()
        page = int(request.args.get('pagina', 1))
        if page < 1:
            raise ValueError()
    except ValueError:
        return 'Mes o página no válida.', 400
    all_rows = CSAppointment.query.filter_by(account_id=account_id).order_by(CSAppointment.fecha_inicio.desc().nullslast(), CSAppointment.id).all()
    statuses = sorted({r.estatus or 'Sin estatus' for r in all_rows})
    rows = [r for r in all_rows if (not month or (local_date(r.fecha_inicio) and local_date(r.fecha_inicio).strftime('%Y-%m') == month))
            and (not status or (r.estatus or 'Sin estatus') == status)]
    return render_template('cs_servicios.html', account=account, rows=rows[(page-1)*100:page*100],
                           dates=local_date, total=len(rows), statuses=statuses, month=month, status=status, page=page, **_ctx())
