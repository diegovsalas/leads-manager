"""Avisos privados: destinatarios, transacciones, reintentos y enlaces seguros."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from extensions import db
from models import Lead, LeadAssignmentNotice as Notice
from tests import fabricas as f
import lead_notifications as notices


@pytest.fixture
def mail(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "prueba-sin-envios-reales")
    response = Mock(ok=True)
    response.json.return_value = {"id": "correo-simulado"}
    sender = Mock(return_value=response)
    monkeypatch.setattr(notices.requests, "post", sender)
    return sender


def _seller(name="Vendedor"):
    seller = f.vendedor(name)
    account = f.login(nombre=name, usuario=seller)
    return seller, account


def _retry_now(notice):
    notice.next_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()


def test_nuevo_lead_solo_avisa_a_su_vendedor_sin_datos_de_contacto(app, mail):
    with app.app_context():
        seller, login = _seller()
        lead = f.lead(propietario=seller, empresa_nombre="Cliente & Compañía", email="privado@cliente.test")
        assert Notice.query.count() == 1
        assert notices.dispatch_pending()["enviadas"] == 1
        payload = mail.call_args.kwargs["json"]
        assert payload["to"] == [login.correo]
        assert "cc" not in payload and "bcc" not in payload
        assert lead.telefono not in str(payload) and lead.email not in str(payload)
        assert "Cliente &amp; Compañía" in payload["html"]
        assert "/?lead=" + str(lead.id) in payload["html"]
        assert Notice.query.one().status == "enviada"
        notices.dispatch_pending()
        assert mail.call_count == 1


def test_crear_sin_asignacion_no_avisa(app, mail):
    with app.app_context():
        f.lead()
        assert Notice.query.count() == 0
        notices.dispatch_pending()
        mail.assert_not_called()


def test_guardar_mismo_vendedor_no_duplica_aviso(app, mail):
    with app.app_context():
        seller, _ = _seller()
        lead = f.lead(propietario=seller)
        lead.usuario_asignado_id = str(seller.id)
        lead.notas = "Se actualizó una nota"
        db.session.commit()
        assert Notice.query.count() == 1
        notices.dispatch_pending()
        assert mail.call_count == 1


def test_rollback_no_deja_aviso_ni_envia(app, mail):
    with app.app_context():
        seller, _ = _seller()
        lead = Lead(nombre="No confirmado", telefono="+521234", usuario_asignado_id=seller.id)
        db.session.add(lead)
        db.session.flush()
        db.session.rollback()
        assert Notice.query.count() == 0
        notices.dispatch_pending()
        mail.assert_not_called()


def test_reasignacion_solo_avisa_al_nuevo_vendedor(app, mail):
    with app.app_context():
        first, _ = _seller("Primero")
        second, second_login = _seller("Segundo")
        lead = f.lead(propietario=first)
        notices.dispatch_pending()
        mail.reset_mock()
        lead.usuario_asignado_id = second.id
        db.session.commit()
        notices.dispatch_pending()
        assert mail.call_args.kwargs["json"]["to"] == [second_login.correo]
        assert "reasignado" in mail.call_args.kwargs["json"]["subject"]
        assert mail.call_count == 1


def test_reasignacion_antes_de_envio_omite_aviso_anterior(app, mail):
    with app.app_context():
        first, _ = _seller("Primero")
        second, second_login = _seller("Segundo")
        lead = f.lead(propietario=first)
        lead.usuario_asignado_id = second.id
        db.session.commit()
        notices.dispatch_pending()
        assert mail.call_count == 1
        assert mail.call_args.kwargs["json"]["to"] == [second_login.correo]
        assert Notice.query.filter_by(status="omitida").count() == 1


def test_volver_al_primer_vendedor_no_reenvia_sus_eventos_viejos(app, mail):
    with app.app_context():
        first, first_login = _seller("Primero")
        second, _ = _seller("Segundo")
        lead = f.lead(propietario=first)
        lead.usuario_asignado_id = second.id
        db.session.commit()
        lead.usuario_asignado_id = first.id
        db.session.commit()
        notices.dispatch_pending()
        assert mail.call_count == 1
        assert mail.call_args.kwargs["json"]["to"] == [first_login.correo]
        assert Notice.query.filter_by(status="omitida").count() == 2


def test_desasignacion_omite_aviso_pendiente(app, mail):
    with app.app_context():
        seller, _ = _seller()
        lead = f.lead(propietario=seller)
        lead.usuario_asignado_id = None
        db.session.commit()
        notices.dispatch_pending()
        mail.assert_not_called()
        assert Notice.query.one().status == "omitida"


def test_fallo_de_correo_no_revierte_asignacion_y_reintenta_sin_duplicar(app, mail):
    with app.app_context():
        seller, _ = _seller()
        lead = f.lead(propietario=seller)
        response = Mock(ok=False, status_code=503)
        mail.return_value = response
        notices.dispatch_pending()
        first = mail.call_args.kwargs
        db.session.refresh(lead)
        assert lead.usuario_asignado_id == seller.id
        notice = Notice.query.one()
        assert notice.status == "pendiente"
        lead.nombre = "Nombre corregido después del primer intento"
        db.session.commit()
        _retry_now(notice)
        success = Mock(ok=True)
        success.json.return_value = {"id": "confirmado"}
        mail.return_value = success
        assert notices.dispatch_pending()["enviadas"] == 1
        assert mail.call_args.kwargs["headers"]["Idempotency-Key"] == first["headers"]["Idempotency-Key"]
        assert mail.call_args.kwargs["json"] == first["json"]


def test_no_reintenta_envios_ambiguos_fuera_de_ventana_idempotente(app, mail):
    with app.app_context():
        seller, _ = _seller()
        f.lead(propietario=seller)
        notice = Notice.query.one()
        notice.first_attempt_at = datetime.now(timezone.utc) - timedelta(hours=24)
        db.session.commit()
        notices.dispatch_pending()
        mail.assert_not_called()
        db.session.refresh(notice)
        assert notice.status == "revision"


@pytest.mark.parametrize("ambiguous", [False, True])
def test_sin_correo_unico_no_adivina_destinatario(app, mail, ambiguous):
    with app.app_context():
        seller = f.vendedor()
        if ambiguous:
            f.login(usuario=seller)
            f.login(usuario=seller)
        f.lead(propietario=seller)
        notices.dispatch_pending()
        mail.assert_not_called()
        assert Notice.query.one().last_error == "correo_ausente_o_ambiguo"


def test_segunda_reserva_no_toma_un_envio_en_curso(app, mail):
    with app.app_context():
        seller, _ = _seller()
        f.lead(propietario=seller)
        assert notices._prepare()["payload"]
        assert notices._prepare() is None
        mail.assert_not_called()


def test_sin_configuracion_el_aviso_permanece_pendiente(app, monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    with app.app_context():
        seller, _ = _seller()
        f.lead(propietario=seller)
        assert notices.dispatch_pending()["enviadas"] == 0
        assert Notice.query.one().attempts == 0


def test_el_enlace_de_un_lead_ajeno_no_revela_sus_datos(app):
    with app.app_context():
        owner = f.vendedor("Dueño")
        outsider = f.vendedor("Otro")
        lead = f.lead(propietario=owner)
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = "prueba"
            session["usuario_id"] = str(outsider.id)
            session["user_rol"] = "Vendedor"
        assert client.get(f"/api/leads/{lead.id}").status_code == 404


def test_login_conserva_enlace_al_lead_y_bloquea_redireccion_externa(app):
    with app.app_context():
        _, account = _seller()
        client = app.test_client()
        lead_id = "00000000-0000-0000-0000-000000000001"
        next_page = "/?lead=" + lead_id
        response = client.post("/login", data={"correo": account.correo, "password": "x", "next": next_page})
        assert response.status_code == 302 and response.location == next_page
        client.get("/logout")
        response = client.post("/login", data={"correo": account.correo, "password": "x", "next": "https://ajeno.test/?lead="+lead_id})
        assert response.location == "/"


def test_enlace_sin_sesion_pide_login_y_conserva_destino(app):
    lead_id = "00000000-0000-0000-0000-000000000001"
    response = app.test_client().get("/?lead="+lead_id)
    assert response.status_code == 302
    assert "next=" in response.location and lead_id in response.location


def test_el_envio_se_programa_despues_del_commit_y_no_al_flush(app, monkeypatch):
    calls = []
    monkeypatch.setattr(notices, "schedule_dispatch", lambda *args, **kwargs: calls.append(True))
    with app.app_context():
        seller = f.vendedor()
        calls.clear()
        lead = Lead(nombre="Pendiente", telefono="+521234", usuario_asignado_id=seller.id)
        db.session.add(lead)
        db.session.flush()
        assert calls == []
        db.session.commit()
        assert calls == [True]


def test_cuenta_desactivada_no_recibe_reintento(app, mail):
    with app.app_context():
        seller, login = _seller()
        f.lead(propietario=seller)
        response = Mock(ok=False, status_code=503)
        mail.return_value = response
        notices.dispatch_pending()
        login.activo = False
        db.session.commit()
        _retry_now(Notice.query.one())
        mail.reset_mock()
        notices.dispatch_pending()
        mail.assert_not_called()
        assert Notice.query.one().status == "omitida"


def test_estado_solo_admin_y_no_envia_correos(app, mail):
    with app.app_context():
        client = app.test_client()
        assert client.get("/api/leads/avisos-asignacion/estado").status_code == 401
        with client.session_transaction() as session:
            session["user_id"] = "pruebas"
            session["user_rol"] = "Vendedor"
        assert client.get("/api/leads/avisos-asignacion/estado").status_code == 403
        with client.session_transaction() as session:
            session["user_rol"] = "Super Admin"
        response = client.get("/api/leads/avisos-asignacion/estado")
        assert response.status_code == 200 and response.get_json()["correo_configurado"]
        mail.assert_not_called()
