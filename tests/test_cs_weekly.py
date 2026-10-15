"""Regresiones: alcance AAA, faltantes, corte histórico y guardado del KAM."""
import json
from datetime import date, datetime, timedelta

import pytest
from extensions import db
from models import CSWeeklyReport, CSIncidencia, CSIncidenciaSeguimiento, CSAppointment, RolCRM
from cs_weekly import validate_payload, summarize, parse_cut, latest_cut, MX
from tests import fabricas as f


def client_for(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = str(user.id)
        session['user_rol'] = user.rol.value
        session['user_nombre'] = user.nombre
    return client


def test_el_corte_del_lunes_es_el_viernes_y_no_acepta_cortes_futuros():
    assert latest_cut(date(2026, 10, 12)) == date(2026, 10, 9)
    assert latest_cut(date(2026, 10, 9)) == date(2026, 10, 2)
    with pytest.raises(ValueError):
        parse_cut('2026-10-16', date(2026, 10, 12))
    with pytest.raises(ValueError):
        parse_cut('2026-10-12', date(2026, 10, 12))


def test_los_faltantes_no_se_interpretan_como_cero_ni_como_verde():
    payload = validate_payload({'metrics': {'expected': 10, 'done': 5, 'forecast': 10}})
    summary = summarize(payload, date(2026, 10, 9))
    assert summary['status'] == 'Sin confirmar'
    assert summary['unscheduled'] is None
    payload['metrics']['scheduled'] = 5
    assert summarize(payload, date(2026, 10, 9))['status'] == 'En tiempo'
    payload['metrics']['scheduled'] = 3
    assert summarize(payload, date(2026, 10, 9))['unscheduled'] == 2
    assert summarize(payload, date(2026, 10, 9))['status'] == 'En riesgo'
    payload['metrics']['forecast'] = 9
    assert summarize(payload, date(2026, 10, 9))['status'] == 'Atrasado'


@pytest.mark.parametrize('metrics', [
    {'expected': -1}, {'expected': 'NaN'}, {'expected': 1.5},
    {'expected': 2, 'done': 3}, {'expected': 4, 'done': 3, 'scheduled': 2},
    {'done': 4, 'forecast': 3}])
def test_rechaza_proyecciones_y_cantidades_inconsistentes(metrics):
    with pytest.raises(ValueError):
        validate_payload({'metrics': metrics})


def test_inicio_solo_muestra_aaa_y_kam_no_puede_ampliar_su_alcance(app):
    with app.app_context():
        kam = f.login('KAM dueño', RolCRM.KAM)
        other = f.login('Otro KAM', RolCRM.KAM)
        own = f.cuenta_cs('AAA propia', kam=kam)
        own.es_aaa = True
        foreign = f.cuenta_cs('AAA ajena', kam=other)
        foreign.es_aaa = True
        normal = f.cuenta_cs('Normal propia', kam=kam)
        db.session.commit()
        client = client_for(app, kam)
        response = client.get('/cs/semanal?kam=' + str(other.id))
        assert response.status_code == 200
        assert b'AAA propia' in response.data
        assert b'AAA ajena' not in response.data
        assert b'Normal propia' not in response.data
        assert b'Normal propia' in client.get('/cs/semanal?alcance=todas').data
        assert client.get(f'/cs/account/{foreign.id}/semanal').status_code == 403
        assert client.post(f'/cs/account/{foreign.id}/semanal', data={'payload': '{}'}).status_code == 403
        assert client.post('/cs/cuentas-aaa', data={'account_id': str(normal.id)}).status_code == 403


def test_direccion_selecciona_aaa_sin_cambiar_tier(app):
    with app.app_context():
        admin = f.login()
        a = f.cuenta_cs('Cuenta seleccionada')
        a.tier = 'Bronze'
        b = f.cuenta_cs('Cuenta deseleccionada')
        b.es_aaa = True
        db.session.commit()
        client = client_for(app, admin)
        assert client.post('/cs/cuentas-aaa', data={'account_id': str(a.id)}).status_code == 302
        db.session.refresh(a); db.session.refresh(b)
        assert a.es_aaa and not b.es_aaa
        assert a.tier == 'Bronze'
        assert client.get('/cs/analisis').status_code == 200
        assert client.get('/cs/administracion').status_code == 200


def test_guardar_corte_valida_y_detecta_edicion_obsoleta(app):
    with app.app_context():
        kam = f.login('KAM', RolCRM.KAM)
        a = f.cuenta_cs(kam=kam)
        client = client_for(app, kam)
        cut = latest_cut()
        stamp = datetime.combine(cut, datetime.min.time(), MX) + timedelta(hours=12)
        db.session.add_all([CSAppointment(account_id=a.id,estatus='Terminada',fecha_inicio=stamp,fecha_terminacion=stamp) for _ in range(5)] +
                           [CSAppointment(account_id=a.id,estatus='Agendada',fecha_inicio=stamp) for _ in range(5)])
        db.session.commit()
        url = f'/cs/account/{a.id}/semanal?corte={cut.isoformat()}'
        raw = {'metrics': {'expected': 10, 'done': 5, 'scheduled': 5, 'forecast': 10}}
        assert client.get(url).status_code == 200
        assert client.post(url, data={'payload': json.dumps(raw)}, headers={'Accept': 'application/json'}).status_code == 302
        report = CSWeeklyReport.query.one()
        version = report.updated_at.isoformat()
        assert report.payload['metrics']['done'] == 5
        assert client.get(url).status_code == 200
        assert client.post(url, data={'payload': json.dumps(raw)}, headers={'Accept': 'application/json'}).status_code == 409
        raw['metrics']['forecast'] = 4
        error = client.post(url, data={'payload': json.dumps(raw), 'version': version}, headers={'Accept':'application/json'})
        assert error.status_code == 400
        assert error.get_json()['error']
        db.session.refresh(report)
        assert report.payload['metrics']['done'] == 5
        assert CSWeeklyReport.query.count() == 1


def test_incidencias_cerradas_despues_del_corte_y_foto_guardada(app):
    with app.app_context():
        from blueprints.cs import _weekly_incidents
        kam = f.login('KAM', RolCRM.KAM)
        a = f.cuenta_cs(kam=kam)
        cut = latest_cut()
        stamp = datetime.combine(cut, datetime.min.time(), MX)
        inc = CSIncidencia(account_id=a.id, tipo='Queja', detalle='Problema al corte',
            status='Resuelta', fecha_incidencia=cut-timedelta(days=1),
            fecha_solucion=cut+timedelta(days=1), created_at=stamp-timedelta(days=1))
        db.session.add(inc); db.session.flush()
        db.session.add_all([
            CSIncidenciaSeguimiento(incidencia_id=inc.id,texto='Acción del viernes',created_at=stamp),
            CSIncidenciaSeguimiento(incidencia_id=inc.id,texto='Acción posterior',created_at=stamp+timedelta(days=1))])
        db.session.commit()
        snapshot = _weekly_incidents(a.id, cut)
        assert snapshot['open'] == 1 and snapshot['closed'] == 0
        assert snapshot['rows'][0]['action'] == 'Acción del viernes'
        client = client_for(app, kam)
        url = f'/cs/account/{a.id}/semanal?corte={cut.isoformat()}'
        assert client.post(url,data={'payload':'{}'}).status_code == 302
        report = CSWeeklyReport.query.one()
        inc.detalle = 'Texto posterior'
        db.session.commit()
        assert b'Problema al corte' in client.get(url).data
        version = report.updated_at.isoformat()
        assert client.post(url,data={'payload':'{}','version':version}).status_code == 302
        db.session.refresh(report)
        assert report.payload['incidents']['rows'][0]['title'] == 'Problema al corte'


def test_se_precargan_compromisos_abiertos_sin_copiar_metricas(app):
    with app.app_context():
        kam = f.login('KAM', RolCRM.KAM)
        a = f.cuenta_cs(kam=kam)
        cut = latest_cut()
        db.session.add(CSWeeklyReport(account_id=a.id,cut_date=cut-timedelta(days=7),payload={
            'metrics': {'done': 999},
            'qbr':[{'title':'Pendiente anterior','status':'Pendiente','due':cut.isoformat()},
                   {'title':'Compromiso ya cerrado','status':'Cerrado'}]}))
        db.session.commit()
        response = client_for(app,kam).get(f'/cs/account/{a.id}/semanal?corte={cut.isoformat()}')
        assert response.status_code == 200
        assert b'Pendiente anterior' in response.data
        assert b'Compromiso ya cerrado' not in response.data
        assert b'value="999"' not in response.data
        assert CSWeeklyReport.query.count() == 1
