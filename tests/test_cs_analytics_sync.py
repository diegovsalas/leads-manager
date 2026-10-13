from extensions import db
from models import CSAppointment, RolCRM
from tests import fabricas as f
from tests.test_cs_weekly import client_for


def test_analytics_ids_match_client_and_reimport_without_duplicates(app, monkeypatch):
    import zoho_analytics
    from blueprints import cs
    with app.app_context():
        admin=f.login('Admin Analytics', RolCRM.DEVELOPER)
        account=f.cuenta_cs('AUTOZONE',kam=admin)
        account.client_id='3115'
        db.session.commit()
        db.session.execute(db.text('CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_appointments_zoho_id ON cs_appointments (zoho_appointment_id) WHERE zoho_appointment_id IS NOT NULL'))
        db.session.commit()
        monkeypatch.setattr(cs,'_HAS_ZOHO_APPT_COL',True)
        monkeypatch.setattr(zoho_analytics,'is_configured',lambda:True)
        rows=[{'ID cita':'162978','ID cliente':'3490','Cliente':'Autozone de México','Propiedad':'Choix','Fecha de Inicio':'02/10/2026 09:58:00','Estatus':'Agendada'},
              {'ID cliente':'3490','Cliente':'Autozone de México','Estatus':'Terminada'}]
        monkeypatch.setattr(zoho_analytics,'fetch_citas',lambda **kw:dict(rows=rows,count=len(rows)))
        client=client_for(app,admin)
        for _ in range(2):
            response=client.post('/cs/cargar-datos/zoho-sync',data={'days':'90'})
            assert response.status_code==200
            assert CSAppointment.query.count()==1
            assert CSAppointment.query.one().account_id==account.id
            assert CSAppointment.query.one().zoho_appointment_id=='162978'
        rows[0]['Estatus']='Técnico Asignado'
        client.post('/cs/cargar-datos/zoho-sync',data={'days':'90'})
        assert CSAppointment.query.count()==1
        assert CSAppointment.query.one().estatus=='Técnico Asignado'
        assert CSAppointment.query.one().fecha_inicio.month==10


def test_failed_analytics_batch_is_not_inserted_without_ids(app,monkeypatch):
    import zoho_analytics
    from blueprints import cs
    with app.app_context():
        admin=f.login('Admin Analytics',RolCRM.DEVELOPER)
        account=f.cuenta_cs('AUTOZONE',kam=admin)
        account.client_id='3115'
        db.session.commit()
        monkeypatch.setattr(cs,'_HAS_ZOHO_APPT_COL',True)
        monkeypatch.setattr(zoho_analytics,'is_configured',lambda:True)
        rows=[{'ID cita':'162978','ID cliente':'3490','Cliente':'Autozone de México','Estatus':'X'*80}]
        monkeypatch.setattr(zoho_analytics,'fetch_citas',lambda **kw:dict(rows=rows,count=1))
        client=client_for(app,admin)
        assert client.post('/cs/cargar-datos/zoho-sync',data={'days':'90'}).status_code==200
        assert CSAppointment.query.count()==0


def test_source_id_conflicts_and_ambiguous_names_are_rejected():
    from cs_analytics_matching import resolve_account
    assert resolve_account('Autozone de México','3490',{'AUTOZONE':'a'},{'3115':'a'})=='a'
    assert resolve_account('Autozone de México','3490',{'AUTOZONE':'a','Otro':'b'},{'3490':'b'}) is None
    assert resolve_account('Autozone de México','3490',{'AUTOZONE':'a','Autozone de México':'b'},{}) is None
    assert resolve_account('Auto','',{'AUTOZONE':'a'},{}) is None


def test_scheduled_sync_uses_same_configured_source(app,monkeypatch):
    import zoho_analytics
    import tareas
    from blueprints import cs
    monkeypatch.setattr(zoho_analytics,'is_configured',lambda:True)
    monkeypatch.setattr(zoho_analytics,'fetch_citas',lambda **kw:dict(rows=[{'ID cita':'1'}]))
    monkeypatch.setattr(cs,'import_analytics_citas',lambda rows:dict(insertados=len(rows),errores=0))
    assert tareas.TAREAS['zoho-citas'].disponible()
    assert tareas._zoho_citas()['insertados']==1


def test_legacy_connector_preserves_actual_column_names_and_source_alias():
    from zoho_appointments_etl import transform
    rows=[{'ID cita':'162978','ID cliente':'3490','Cliente':'Autozone de México','Fecha de Inicio':'02/10/2026 15:58:19','Estatus':'Técnico Asignado'}]
    result=transform(rows,{'3115':'a','autozone':'a'})
    assert len(result)==1
    assert result[0]['account_id']=='a' and result[0]['zoho_appointment_id']=='162978'
