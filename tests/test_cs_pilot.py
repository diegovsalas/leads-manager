"""El lanzador del piloto debe rechazar conexiones externas y conservar avances."""
import pytest
from piloto_cs import local_database_url, seed_pilot
from models import CSAccount, CSTask, CSWeeklyReport, CSGestion, UserCRM
from extensions import db


@pytest.mark.parametrize('url',[
    'postgresql://u:p@db.example.com/prod',
    'postgresql://u:p@localhost/db?host=db.example.com',
    'sqlite:///test.db', '', None,
])
def test_piloto_rechaza_conexiones_no_locales_y_redirecciones(url):
    with pytest.raises(ValueError):
        local_database_url(url)


def test_piloto_fuerza_base_dedicada():
    assert local_database_url('postgresql://u:p@127.0.0.1:5432/avantex_crm_local')=='postgresql://u:p@127.0.0.1:5432/avantex_crm_pilot'


def test_reiniciar_seed_no_recrea_ni_pisa_la_gestion(app):
    with app.app_context():
        first=seed_pilot()
        a=CSAccount.query.filter_by(client_id='P1-001').one()
        a.nombre='Nombre modificado durante el piloto'
        db.session.commit()
        counts=(CSAccount.query.count(),CSTask.query.count(),CSWeeklyReport.query.count(),CSGestion.query.count())
        assert seed_pilot()==first
        assert (CSAccount.query.count(),CSTask.query.count(),CSWeeklyReport.query.count(),CSGestion.query.count())==counts
        db.session.refresh(a)
        assert a.nombre=='Nombre modificado durante el piloto'
        assert UserCRM.query.count()==4
        assert CSAccount.query.filter_by(kam_id=first['kam1']).count()==3


def test_app_normal_no_registra_selector_de_perfiles(app):
    assert not any(rule.endpoint in ('pilot_home','pilot_login') for rule in app.url_map.iter_rules())
    assert app.test_client().get('/auth/google/__pilot').status_code==404
