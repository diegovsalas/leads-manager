from datetime import datetime, date, timezone
from types import SimpleNamespace
from cs_services import summarize_rows
from models import CSAppointment, CSWeeklyReport, RolCRM
from extensions import db
from tests import fabricas as f
from tests.test_cs_weekly import client_for
import json


def row(status='Agendada', start=None, end=None, branch='Sucursal'):
    return SimpleNamespace(estatus=status,fecha_inicio=start,fecha_terminacion=end,propiedad=branch,
                           titulo_servicio='Fumigación',zoho_appointment_id=None)


def test_counts_preserve_statuses_and_identify_duplicates():
    start=datetime(2026,10,5,12,tzinfo=timezone.utc)
    rows=[row(start=start),row(start=start),row('Cancelada',start,branch='B'),row('Personalizado',start,branch='C'),
          row('Terminada',start,datetime(2026,10,12,12,tzinfo=timezone.utc),branch='D'),row('Terminada',start,branch='E')]
    s=summarize_rows(rows,'2026-10',date(2026,10,9))
    assert s['records']==6 and s['completed']==0 and s['pending']==2 and s['overdue']==2
    assert s['missing_end']==1 and s['duplicate_groups']==1
    assert {r['status'] for r in s['statuses']}=={'Agendada','Cancelada','Personalizado','Terminada'}


def test_completed_uses_finish_month_and_local_timezone():
    r=row('Terminada',datetime(2026,9,20,tzinfo=timezone.utc),datetime(2026,10,1,2,tzinfo=timezone.utc))
    assert summarize_rows([r],'2026-09',date(2026,10,9))['completed']==1
    assert summarize_rows([r],'2026-10',date(2026,10,9))['completed']==0


def test_own_account_all_statuses_filters_and_no_false_zero(app):
    with app.app_context():
        kam=f.login('KAM',RolCRM.KAM);other=f.login('Otro',RolCRM.KAM)
        a=f.cuenta_cs('Cliente',kam=kam);b=f.cuenta_cs('Ajeno',kam=other)
        c=client_for(app,kam)
        assert b'no confirma' in c.get(f'/cs/cuentas/{a.id}').data
        db.session.add(CSAppointment(account_id=a.id,estatus='Cancelada',fecha_inicio=datetime(2026,10,5,12,tzinfo=timezone.utc)))
        db.session.commit()
        url=f'/cs/cuentas/{a.id}/servicios'
        assert b'Cancelada' in c.get(url).data
        assert b'1 coincidencias' in c.get(url+'?mes=2026-10&estatus=Cancelada').data
        assert c.get(url+'?mes=2026-13').status_code==400
        assert c.get(url+'?pagina=0').status_code==400
        assert c.get(f'/cs/cuentas/{b.id}/servicios').status_code==403


def test_saved_snapshot_is_server_generated(app):
    from cs_weekly import latest_cut
    with app.app_context():
        kam=f.login('KAM',RolCRM.KAM);a=f.cuenta_cs('Cliente',kam=kam);c=client_for(app,kam)
        appointment=CSAppointment(account_id=a.id,estatus='No Realizada',fecha_inicio=datetime.now(timezone.utc))
        db.session.add(appointment);db.session.commit()
        url=f'/cs/account/{a.id}/semanal?corte={latest_cut().isoformat()}'
        assert c.post(url,data={'payload':json.dumps({'services':{'total':9999},'metrics':{}}),'version':''}).status_code==302
        report=CSWeeklyReport.query.one()
        assert report.payload['services']['total']==1
        frozen=report.payload['services']
        report.estado='Entregado';db.session.commit()
        db.session.delete(appointment);db.session.commit()
        r=c.get(url)
        assert b'Servicios registrados' in r.data and '1 citas' in r.get_data(as_text=True)
        assert CSWeeklyReport.query.one().payload['services']==frozen


def test_scope_comparison_uses_active_rows_and_does_not_invent_ids():
    from cs_services import compare_scope
    s=compare_scope({'filas':[{'propiedad':'Sucursal A','activo':True},
                              {'propiedad':'Sucursal B','activo':True},
                              {'propiedad':'Cancelada','activo':False}],
                     'importe_activos':'1200','cada_meses':2},[row(branch='Sucursal A'),row(branch='Otra')])
    assert s['contracted']==2 and s['matched']==1 and s['missing']==['Sucursal B'] and s['outside']==1
    assert s['api_ids']==0 and s['cycle']==2
    assert compare_scope({},[])['has_scope'] is False


def test_previous_month_no_manual_inputs_and_ignores_tampering(app):
    from cs_weekly import latest_cut
    with app.app_context():
        kam=f.login('KAM',RolCRM.KAM);a=f.cuenta_cs('Cliente',kam=kam);c=client_for(app,kam)
        url=f'/cs/account/{a.id}/semanal?corte={latest_cut().isoformat()}'
        page=c.get(url).get_data(as_text=True)
        assert 'Pendiente de validar' in page and 'data-key="previous_comment"' in page
        for key in ('prev_expected','prev_done','prev_billed','prev_target'):
            assert f'data-key="{key}"' not in page
        metrics={k:0 for k in ('expected','done','scheduled','forecast','billed','target','billing_forecast')}
        metrics.update(prev_expected=99,prev_done=88,prev_billed=777,prev_target=999,previous_comment='Seguimiento registrado')
        response=c.post(url,data={'payload':json.dumps({'metrics':metrics,'previous_auto':{'prev_done':{'value':88}}}),'accion':'enviar','version':''})
        assert response.status_code==302
        report=CSWeeklyReport.query.one()
        assert report.estado=='Entregado' and report.payload['metrics']['previous_comment']=='Seguimiento registrado'
        assert all(report.payload['metrics'][k] is None for k in ('prev_expected','prev_done','prev_billed','prev_target'))
        assert report.payload['previous_auto']['prev_done']['value'] is None
        assert 'Pendiente de validar' in c.get(url).get_data(as_text=True)


def test_previous_completed_is_automatic_and_frozen_on_delivery(app):
    from cs_weekly import latest_cut, month_bounds
    cut=latest_cut()
    previous,_,_=month_bounds(cut)
    closed=datetime(previous.year,previous.month,15,12,tzinfo=timezone.utc)
    with app.app_context():
        kam=f.login('KAM auto',RolCRM.KAM)
        a=f.cuenta_cs('Cliente auto',kam=kam)
        c=client_for(app,kam)
        db.session.add_all([
            CSAppointment(account_id=a.id,estatus='Terminada',fecha_inicio=closed,fecha_terminacion=closed),
            CSAppointment(account_id=a.id,estatus='Cancelada',fecha_inicio=closed)])
        db.session.commit()
        url=f'/cs/account/{a.id}/semanal?corte={cut.isoformat()}'
        page=c.get(url).get_data(as_text=True)
        assert '<strong>1</strong>' in page and '>Registrados</span>' in page
        assert 'data-key="prev_done"' not in page
        metrics={k:0 for k in ('expected','done','scheduled','forecast','billed','target','billing_forecast')}
        metrics['prev_done']=999
        assert c.post(url,data={'payload':json.dumps({'metrics':metrics}),'accion':'enviar','version':''}).status_code==302
        report=CSWeeklyReport.query.one()
        assert report.estado=='Entregado' and report.payload['metrics']['prev_done']==1
        assert report.payload['previous_auto']['prev_done']['status']=='Registrados'
        db.session.add(CSAppointment(account_id=a.id,estatus='Terminada',fecha_inicio=closed,fecha_terminacion=closed))
        db.session.commit()
        assert '<strong>1</strong>' in c.get(url).get_data(as_text=True)
        assert CSWeeklyReport.query.one().payload['metrics']['prev_done']==1


def test_previous_completed_distinguishes_missing_data_from_recorded_zero():
    from cs_services import previous_month_data
    assert previous_month_data({'months':[{'records':0,'completed':0}]})['prev_done']['value'] is None
    assert previous_month_data({'months':[{'records':3,'completed':0}]})['prev_done']['value']==0
    assert previous_month_data({'months':[{'records':0,'completed':2}]})['prev_done']['value']==2


def test_current_month_auto_services_and_invoices_are_frozen(app):
    from datetime import timedelta
    from cs_weekly import latest_cut, month_bounds
    from models import CSInvoice, SavioInvoice
    cut=latest_cut()
    previous,current,_=month_bounds(cut)
    start=datetime(current.year,current.month,1,12,tzinfo=timezone.utc)
    close=datetime(cut.year,cut.month,cut.day,12,tzinfo=timezone.utc)
    with app.app_context():
        kam=f.login('KAM actual',RolCRM.KAM);a=f.cuenta_cs('Cuenta actual',kam=kam)
        c=client_for(app,kam)
        db.session.add_all([
            CSAppointment(account_id=a.id,estatus='Terminada',fecha_inicio=start,fecha_terminacion=close),
            CSAppointment(account_id=a.id,estatus='Terminada',fecha_inicio=start,fecha_terminacion=close+timedelta(days=1)),
            CSAppointment(account_id=a.id,estatus='Agendada',fecha_inicio=start),
            CSAppointment(account_id=a.id,estatus='Cancelada',fecha_inicio=start),
            SavioInvoice(id='234567',date=current,status='valid',amount=1234.56,raw_data={'currency':'MXN'}),
            CSInvoice(account_id=a.id,savio_invoice_id=234567,total=1234.56,fecha_cobro=current),
            SavioInvoice(id='234568',date=previous,status='valid',amount=50,raw_data={'currency':'MXN'}),
            CSInvoice(account_id=a.id,savio_invoice_id=234568,total=50,fecha_cobro=previous),
        ])
        db.session.commit()
        url=f'/cs/account/{a.id}/semanal?corte={cut.isoformat()}'
        page=c.get(url).get_data(as_text=True)
        assert '$1,234.56' in page and '$50.00' in page
        assert all(f'data-key="{key}"' not in page for key in ('done','scheduled','billed'))
        assert 'Proyección de servicios del KAM' in page and 'Confirmar meta con Comercial' in page
        metrics={'expected':1,'done':999,'scheduled':999,'billed':999,'forecast':3,'target':2000,'billing_forecast':1500}
        assert c.post(url,data={'payload':json.dumps({'metrics':metrics}),'accion':'enviar','version':''}).status_code==302
        report=CSWeeklyReport.query.one()
        assert report.payload['metrics']['done']==1
        assert report.payload['metrics']['scheduled']==1
        assert report.payload['metrics']['billed']==1234.56
        assert report.payload['metrics']['prev_billed']==50
        assert report.payload['current_auto']['billed']['value']==1234.56
        db.session.add(CSAppointment(account_id=a.id,estatus='Agendada',fecha_inicio=start))
        db.session.commit()
        c.get(url)
        assert CSWeeklyReport.query.one().payload['current_auto']['scheduled']['value']==1


def test_invoice_summary_requires_verified_currency_date_status_and_amount():
    from decimal import Decimal
    from cs_services import summarize_invoices
    cut=date(2026,10,9)
    def invoice(total='100',source_date=date(2026,10,1),status='valid',currency='MXN',amount='100'):
        return (SimpleNamespace(total=Decimal(total),fecha_cobro=source_date),
                SimpleNamespace(date=source_date,status=status,raw_data={'currency':currency},amount=Decimal(amount)))
    rows=[invoice(),invoice(status='cancelled'),invoice(currency='USD'),invoice(currency=''),
          invoice(source_date=date(2026,10,10)),invoice(amount='200'),
          (SimpleNamespace(total=100,fecha_cobro=date(2026,10,1)),None)]
    result=summarize_invoices(rows,'2026-10',cut)
    assert result['value']==100 and result['count']==1 and result['omitted']==5
    assert summarize_invoices([],'2026-10',cut)['value'] is None
