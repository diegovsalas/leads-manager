"""Permisos, trazabilidad y fuente compartida del trabajo diario del KAM."""
import json
from datetime import date, datetime, timedelta, timezone
from extensions import db
from models import CSAccount, CSTask, CSGestion, CSContacto, CSNote, CSWeeklyReport, RolCRM
from cs_daily import new_task, weekly_tasks
from cs_weekly import MX, latest_cut
from tests import fabricas as f
from tests.test_cs_weekly import client_for


def setup(app):
    kam = f.login('KAM propio', RolCRM.KAM)
    other = f.login('KAM ajeno', RolCRM.KAM)
    a = f.cuenta_cs('Cuenta normal propia', kam=kam)
    b = f.cuenta_cs('Cuenta privada ajena', kam=other)
    return kam, other, a, b, client_for(app, kam)


def task(a, kam, due=None):
    t = new_task(a, {'descripcion':'Confirmar servicio', 'fecha_limite':(due or date.today()).isoformat()}, kam.nombre, str(kam.id))
    db.session.commit()
    return t


def action(c, a, t, mode, **data):
    db.session.refresh(t)
    return c.post(f'/cs/cuentas/{a.id}/gestion?tarea={t.id}&modo={mode}', data={'version':t.updated_at.isoformat(), **data})


def test_alta_kam_asigna_su_cartera_y_rechaza_duplicado_normalizado(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        data = dict(nombre='Palácio de Hierro',unidad='Aromatex',contacto='Contacto',correo='contacto@example.test',kam_id=str(other.id),mrr='999999',es_aaa='true')
        r = c.post('/cs/cuentas/agregar',data=data)
        assert r.status_code == 302
        created = CSAccount.query.filter_by(nombre=data['nombre']).one()
        assert created.kam_id == kam.id and created.datos_por_validar
        assert not created.es_aaa and not created.mrr
        assert CSContacto.query.filter_by(account_id=created.id,is_owner=True).one().correo == data['correo']
        assert CSGestion.query.filter_by(account_id=created.id,accion='Alta').count() == 1
        data['nombre'] = '  PALACIO   DE HIERRO '
        assert client_for(app,other).post('/cs/cuentas/agregar',data=data).status_code == 400
        assert CSAccount.query.count() == 3


def test_busqueda_global_evitar_duplicados_sin_datos_comerciales(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        row = c.get('/cs/cuentas/buscar?q=privada').get_json()[0]
        assert row == dict(name=b.nombre,kam=other.nombre,url=None)
        assert c.get(f'/cs/cuentas/{b.id}').status_code == 403
        assert c.get('/cs/cuentas/agregar').status_code == 200
        assert c.get('/cs/cuentas').status_code == 200
        assert b.nombre.encode() not in c.get('/cs/cuentas').data


def test_hoy_incluye_no_aaa_pero_no_tareas_ajenas(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        t = task(a,kam); foreign = task(b,other)
        page = c.get('/cs/?kam='+str(other.id))
        assert page.status_code == 200
        assert a.nombre.encode() in page.data and b.nombre.encode() not in page.data
        assert c.get(f'/cs/cuentas/{a.id}/gestion?tarea={foreign.id}').status_code == 404
        assert c.get(f'/cs/cuentas/{a.id}').status_code == 200
        assert c.get(f'/cs/cuentas/{a.id}/gestion?tarea={t.id}').status_code == 200


def test_gestion_y_siguiente_paso_son_atomicos(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        url = f'/cs/cuentas/{a.id}/gestion'
        assert c.post(url,data={'resultado':'Cliente confirmó avance','siguiente_paso':'Agendar visita'}).status_code == 400
        assert CSGestion.query.count() == CSNote.query.count() == CSTask.query.count() == 0
        assert c.post(url,data={'resultado':'Cliente confirmó avance','siguiente_paso':'Agendar visita','fecha_limite':date.today().isoformat()}).status_code == 302
        assert CSGestion.query.count() == 2 and CSNote.query.count() == 1
        assert CSTask.query.one().responsable == kam.nombre


def test_reprogramacion_conserva_fecha_original_y_cierre_exige_respaldo(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        original = date.today()-timedelta(days=2)
        t = task(a,kam,original)
        assert action(c,a,t,'seguimiento',resultado='Cliente pidió nueva visita',siguiente_paso='Visitar al cliente',fecha_limite=date.today().isoformat()).status_code == 302
        db.session.refresh(t)
        assert t.fecha_original == original and t.fecha_limite == date.today()
        event = CSGestion.query.filter_by(accion='Reprogramado').one()
        assert event.snapshot['before']['due'] == original.isoformat()
        assert event.actor_id == str(kam.id)
        assert action(c,a,t,'cerrar',resultado='Servicio realizado').status_code == 400
        db.session.refresh(t); assert not t.completada
        assert action(c,a,t,'cerrar',resultado='Servicio realizado',respaldo='Folio de servicio 12345').status_code == 302
        db.session.refresh(t); assert t.completada and t.estado == 'Cerrado' and t.completed_at
        assert t.descripcion.encode() not in c.get('/cs/').data
        assert b'Folio de servicio 12345' in c.get(f'/cs/cuentas/{a.id}').data


def test_conflicto_de_version_y_cancelacion_conservan_tarea(app):
    with app.app_context():
        kam, other, a, b, c = setup(app); t = task(a,kam)
        url=f'/cs/cuentas/{a.id}/gestion?tarea={t.id}&modo=cancelar'
        assert c.post(url,data={'version':'obsoleta','resultado':'Cliente suspendió servicio'}).status_code == 409
        assert action(c,a,t,'cancelar',resultado='Cliente suspendió servicio').status_code == 302
        db.session.refresh(t); assert t.estado == 'Cancelado' and CSTask.query.count() == 1
        assert CSGestion.query.filter_by(accion='Cancelado').count() == 1


def test_corte_historico_no_toma_reprogramaciones_posteriores(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        cut = latest_cut(); original=cut-timedelta(days=1)
        t = task(a,kam,original)
        event = CSGestion.query.filter_by(task_id=t.id).one()
        t.created_at = datetime.combine(cut-timedelta(days=4),datetime.min.time(),MX)
        event.created_at=t.created_at
        db.session.commit()
        assert action(c,a,t,'seguimiento',resultado='Se reprogramó después del corte',siguiente_paso='Visitar',fecha_limite=(cut+timedelta(days=10)).isoformat()).status_code == 302
        rows = weekly_tasks(a.id,cut)['agreements']
        assert rows[0]['due'] == original.isoformat() and rows[0]['status'] == 'Pendiente'
        assert not weekly_tasks(a.id,cut)['activity']


def test_entrega_no_acepta_vacios_bloquea_y_direccion_puede_devolver(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        cut = latest_cut(); url=f'/cs/account/{a.id}/semanal?corte={cut}'
        assert c.post(url,data={'payload':'{}','accion':'enviar'}).status_code == 400
        assert c.post(url,data={'payload':'{}'}).status_code == 302
        r=CSWeeklyReport.query.one(); assert r.estado == 'Borrador'
        metrics={key:0 for key in ('prev_expected','prev_done','prev_billed','prev_target','expected','done','scheduled','forecast','billed','target','billing_forecast')}
        assert c.post(url,data={'payload':json.dumps({'metrics':metrics,'agreements':[{'title':'Inyección'}]}),'accion':'enviar','version':r.updated_at.isoformat()}).status_code == 302
        db.session.refresh(r); assert r.estado=='Entregado' and r.submitted_at
        assert r.payload['agreements']==[]
        assert c.get(url).status_code == 200
        assert c.post(url,data={'payload':'{}','version':r.updated_at.isoformat()}).status_code == 409
        review=f'/cs/account/{a.id}/semanal/revisar?corte={cut}'
        data={'accion':'devolver','version':r.updated_at.isoformat()}
        assert c.post(review,data=data).status_code == 403
        admin=client_for(app,f.login())
        assert admin.post(review,data=data).status_code == 400
        data['observaciones']='Confirmar cifra con operaciones'
        assert admin.post(review,data=data).status_code == 302
        db.session.refresh(r); assert r.estado=='Devuelto'
        assert c.post(url,data={'payload':'{}','version':r.updated_at.isoformat()}).status_code == 302


def test_importacion_es_idempotente_y_oculta_aviso_al_terminar(app):
    with app.app_context():
        kam, other, a, b, c = setup(app)
        db.session.add(CSWeeklyReport(account_id=a.id,cut_date=latest_cut(),payload={'agreements':[{'title':'Acuerdo anterior','status':'Pendiente','due':date.today().isoformat()}]}));db.session.commit()
        assert b'Incorporar compromisos' in c.get('/cs/').data
        url=f'/cs/cuentas/{a.id}/importar-compromisos'
        assert c.post(url).status_code == c.post(url).status_code == 302
        assert CSTask.query.count()==1 and CSGestion.query.count()==1
        assert b'Incorporar compromisos' not in c.get('/cs/').data


def test_vias_antiguas_no_pueden_cerrar_ni_borrar_sin_bitacora(app):
    with app.app_context():
        kam, other, a, b, c = setup(app); t=task(a,kam)
        for suffix in ('toggle','delete'):
            assert c.post(f'/cs/account/{a.id}/tasks/{t.id}/{suffix}').status_code==302
            db.session.refresh(t); assert not t.completada and CSTask.query.count()==1
        assert CSGestion.query.count()==1
        assert c.post(f'/cs/account/{a.id}/tasks',data={'descripcion':'Sin fecha'}).status_code==400
        assert CSTask.query.count()==1


def test_reporte_usa_tarea_existente_sin_duplicar_al_guardar(app):
    with app.app_context():
        kam, other, a, b, c=setup(app);cut=latest_cut();t=task(a,kam,cut)
        t.created_at=datetime.combine(cut-timedelta(days=4),datetime.min.time(),MX)
        e=CSGestion.query.one();e.created_at=t.created_at;db.session.commit()
        url=f'/cs/account/{a.id}/semanal?corte={cut}'
        assert c.post(url,data={'payload':'{}'}).status_code==302
        r=CSWeeklyReport.query.one()
        assert r.payload['agreements'][0]['task_id']==str(t.id)
        assert c.post(url,data={'payload':'{}','version':r.updated_at.isoformat()}).status_code==302
        db.session.refresh(r); assert len(r.payload['agreements'])==1 and CSTask.query.count()==1


def test_validacion_de_identidad_es_exclusiva_de_administracion(app):
    with app.app_context():
        kam, other, a, b, c=setup(app);a.datos_por_validar=True;db.session.commit()
        url=f'/cs/cuentas/{a.id}/validar'
        assert c.post(url,data={'client_id':'REAL-1','resultado':'Contrato verificado'}).status_code==403
        admin=client_for(app,f.login())
        assert admin.get(url).status_code==200
        assert admin.post(url,data={'client_id':'REAL-1','resultado':'Contrato verificado'}).status_code==302
        db.session.refresh(a);assert a.client_id=='REAL-1' and not a.datos_por_validar
        assert CSGestion.query.filter_by(accion='Validación').count()==1


def test_tipo_de_junta_legacy_y_qbr_siguen_alimentando_vistas_existentes(app):
    with app.app_context():
        kam, other, a, b, c=setup(app)
        assert c.post(f'/cs/account/{a.id}/tasks',data={'descripcion':'Junta con operaciones','tipo':'Junta','fecha_limite':date.today().isoformat()}).status_code==302
        assert CSTask.query.one().tipo=='Junta'
        assert c.post(f'/cs/cuentas/{a.id}/gestion?modo=programar',data={'descripcion':'Compromiso trimestral','source':'qbr','fecha_limite':date.today().isoformat()}).status_code==302
        assert CSTask.query.filter_by(tipo='QBR').count()==1
