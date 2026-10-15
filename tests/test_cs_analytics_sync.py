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
    assert resolve_account('Grupo Batia','1305',{'Batia':'a'},{'1305':'a'})=='a'
    assert resolve_account('Grupo Diagnostico Aries - GDA','1274',{'GDA':'a'},{'1274':'a'})=='a'
    assert resolve_account('Tiendas Soriana','4201',{'Soriana':'a'},{'4201':'a'})=='a'
    assert resolve_account('Grupo Batia','',{'Batia':'a'},{}) is None
    assert resolve_account('Grupo Batia','1305',{'Batia':'a','Grupo Batia':'b'},{'1305':'a'}) is None


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


def test_general_sync_collapses_repeated_ids_and_isolates_conflicts(app, monkeypatch):
    from blueprints import cs
    with app.app_context():
        admin = f.login('Admin', RolCRM.DEVELOPER)
        a = f.cuenta_cs('Cliente A', kam=admin)
        b = f.cuenta_cs('Cliente B', kam=admin)
        db.session.commit()
        db.session.execute(db.text('CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_appointments_zoho_id ON cs_appointments (zoho_appointment_id) WHERE zoho_appointment_id IS NOT NULL'))
        db.session.commit()
        monkeypatch.setattr(cs, '_HAS_ZOHO_APPT_COL', True)
        row = {'ID cita': '1', 'Cliente': 'Cliente A', 'Estatus': 'Terminada'}
        rows = [dict(row, Equipos=str(i)) for i in range(2001)]
        rows += [{'ID cita': '2', 'Cliente': 'Cliente B', 'Estatus': 'Agendada'},
                 {'ID cita': '3', 'Cliente': 'Cliente B', 'Estatus': 'Terminada'},
                 {'ID cita': '3', 'Cliente': 'Cliente B', 'Estatus': 'Cancelada'}]
        for _ in range(2):
            stats = cs.import_analytics_citas(rows)
            assert stats['insertados'] == 2
            assert stats['errores'] == 2
            assert CSAppointment.query.count() == 2
            assert CSAppointment.query.filter_by(zoho_appointment_id='1').one().account_id == a.id
            assert CSAppointment.query.filter_by(zoho_appointment_id='2').one().account_id == b.id


def test_unique_rows_preserves_missing_id_errors_and_rejects_conflicting_quantity():
    from cs_analytics_matching import unique_appointment_rows
    rows = [{}, {}, {'ID cita': '7', 'Cantidad': '1'}, {'ID cita': '7', 'Cantidad': '2'}]
    unique, duplicates, conflicts = unique_appointment_rows(rows)
    assert unique == [{}, {}]
    assert duplicates == 0 and conflicts == 2


def test_confirmed_aliases_are_persistent_and_unambiguous(app, monkeypatch):
    from blueprints import cs
    from cs_analytics_matching import resolve_account
    names={'Holding':'a','Otro':'b'}
    aliases={'a':[{'name':'Holding','match':'contains'}]}
    assert resolve_account('Seguros Holding','999',names,{},aliases)=='a'
    assert resolve_account('Holding Bancario','998',names,{},aliases)=='a'
    assert resolve_account('Otro Holding','999',names,{},aliases) is None
    assert resolve_account('Seguros Holding','999',names,{},dict(aliases,b=aliases['a'])) is None
    exact={'a':[{'name':'Sucursal Sonora','client_id':'2046'}]}
    assert resolve_account('Sucursal Sonora','2046',names,{},exact)=='a'
    assert resolve_account('Sucursal Sonora','2047',names,{},exact) is None
    with app.app_context():
        admin=f.login('Admin',RolCRM.DEVELOPER)
        account=f.cuenta_cs('Holding',kam=admin)
        account.analytics_aliases=aliases['a']
        db.session.commit()
        db.session.execute(db.text('CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_appointments_zoho_id ON cs_appointments (zoho_appointment_id) WHERE zoho_appointment_id IS NOT NULL'))
        db.session.commit()
        monkeypatch.setattr(cs,'_HAS_ZOHO_APPT_COL',True)
        stats=cs.import_analytics_citas([{'ID cita':'10','Cliente':'Seguros Holding','ID cliente':'999'},
                                       {'ID cita':'11','Cliente':'Banca Holding','ID cliente':'998'}])
        assert stats['insertados']==2 and stats['no_match']==0
        assert CSAppointment.query.filter_by(account_id=account.id).count()==2
