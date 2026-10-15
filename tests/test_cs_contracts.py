"""Contrato por cuenta: privado, versionado y sin cifras operativas inventadas."""
import io
import json
from datetime import date
import pytest
from extensions import db
from models import CSContrato, CSGestion, CSWeeklyReport, RolCRM
from cs_contracts import analyze_csv
from cs_weekly import latest_cut
from tests import fabricas as f
from tests.test_cs_weekly import client_for

HEADER='Cliente,Servicio,Recurrencia,Precio Unitario,Detalles,Descuento,Propiedad,Monto Cotizado,Estatus,ID\n'
DATA=(HEADER+'Grupo Haber,Fumigación,Cada 2 meses,$600.00,1,0%,Tienda A,600,Agendado,HAB-001\n'+'Grupo Haber,Fumigación,Cada 2 meses,$600.00,1,0%,Tienda B,600,Cancelado,HAB-002\n').encode()


def setup(app):
    kam=f.login('Nalleli Quiroz',RolCRM.KAM)
    other=f.login('Otro KAM',RolCRM.KAM)
    a=f.cuenta_cs('Grupo Haber',kam=kam)
    b=f.cuenta_cs('Otra cuenta',kam=other)
    return a,b,client_for(app,kam),client_for(app,other)


def upload(c,a,data=DATA,filename='contrato.csv',**form):
    return c.post(f'/cs/cuentas/{a.id}/contratos',data={'archivo':(io.BytesIO(data),filename),'activo':'1',**form},content_type='multipart/form-data')


def test_csv_excluye_cancelados_y_no_inventa_un_mes():
    s=analyze_csv(DATA)
    assert (s['registros'],s['activos'],s['cancelados'],s['importe_activos'],s['cada_meses'])==(2,1,1,'600',2)
    assert 'expected' not in s and 'scheduled' not in s and 'billed' not in s
    assert 'fechas del ciclo' in s['pendiente']


@pytest.mark.parametrize('data',[
    b'',b'Archivo sin encabezados',
    (HEADER+'Grupo Haber,Fumigación,Cada 2 meses,$600.00,1,0%,A,600,Agendado,HAB-001\n'*2).encode(),
    DATA.replace(b'600,Agendado',b'NaN,Agendado'),
    DATA.replace(b'600,Agendado',b'1e999999,Agendado'),
    DATA.replace(b'Cancelado',b'Estatus inventado'),
    DATA.replace(b'Tienda B',b'Tienda B,Columna extra'),
    DATA.replace(b'Grupo Haber,Fumigaci',b'Otro cliente,Fumigaci',1),
])
def test_csv_rechaza_filas_ambiguas_o_importes_invalidos(data):
    with pytest.raises(ValueError):analyze_csv(data)


def test_carga_privada_y_descarga_solo_de_la_cartera(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert c.get(f'/cs/cuentas/{a.id}/contratos').status_code==200
        assert upload(c,a).status_code==302
        doc=CSContrato.query.one();assert doc.activo and doc.resumen['activos']==1
        url=f'/cs/cuentas/{a.id}/contratos/{doc.id}/descargar'
        r=c.get(url);assert r.status_code==200 and r.data==DATA and r.headers['Cache-Control']=='private, no-store'
        assert foreign.get(url).status_code==403
        assert foreign.post(f'/cs/cuentas/{a.id}/contratos',data={}).status_code==403
        assert c.get(f'/cs/cuentas/{b.id}/contratos/{doc.id}/descargar').status_code==403
        assert b'contrato.csv' in c.get(f'/cs/cuentas/{a.id}').data
        assert CSGestion.query.filter_by(accion='Contrato cargado').count()==1


def test_versiones_y_reactivacion_no_borran_original(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a).status_code==302
        old=CSContrato.query.one()
        data=DATA.replace(b'$600.00',b'$650.00').replace(b',600,',b',650,')
        assert upload(c,a,data,filename='renovacion.csv').status_code==302
        db.session.refresh(old);assert not old.activo and old.contenido==DATA
        assert CSContrato.query.filter_by(activo=True).count()==1
        url=f'/cs/cuentas/{a.id}/contratos/{old.id}/activar'
        assert c.post(url).status_code==302
        assert c.post(url).status_code==302
        db.session.refresh(old);assert old.activo and CSContrato.query.filter_by(activo=True).count()==1
        assert CSContrato.query.count()==2
        assert upload(c,a).status_code==400
        assert CSContrato.query.count()==2


def test_error_de_cliente_o_vigencia_no_desactiva_contrato(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a).status_code==302
        assert upload(c,a,DATA.replace(b'Grupo Haber',b'Otro Grupo')).status_code==400
        assert upload(c,a,DATA+b'\n',inicio='2026-12-01',fin='2026-01-01').status_code==400
        assert CSContrato.query.count()==1 and CSContrato.query.one().activo
        assert CSGestion.query.count()==1


def test_archivos_pdf_validos_sin_suposiciones_y_extensiones_invalidas(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a,b'<script>test</script>',filename='contrato.html').status_code==400
        assert upload(c,a,b'archivo cualquiera',filename='contrato.pdf').status_code==400
        assert upload(c,a,b'%PDF-1.4\ncontenido de prueba',filename='contrato.pdf').status_code==302
        assert CSContrato.query.one().resumen=={}


def test_reporte_congela_referencia_sin_llenar_facturacion(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a).status_code==302
        url=f'/cs/account/{a.id}/semanal?corte={latest_cut()}'
        page=c.get(url);assert page.status_code==200 and b'Contrato de referencia' in page.data
        assert c.post(url,data={'payload':'{}'}).status_code==302
        r=CSWeeklyReport.query.one();assert r.payload['metrics']['expected'] is None and r.payload['metrics']['billed'] is None
        assert r.payload['contract_reference']['resumen']['importe_activos']=='600'
        metrics={key:0 for key in ('prev_expected','prev_done','prev_billed','prev_target','expected','done','scheduled','forecast','billed','target','billing_forecast')}
        assert c.post(url,data={'payload':json.dumps({'metrics':metrics}),'accion':'enviar','version':r.updated_at.isoformat()}).status_code==302
        db.session.refresh(r);old_id=r.payload['contract_reference']['id']
        assert upload(c,a,DATA.replace(b'Tienda A',b'Tienda C'),filename='contrato_nuevo.csv').status_code==302
        page=c.get(url);assert page.status_code==200 and b'contrato.csv' in page.data
        db.session.refresh(r);assert r.payload['contract_reference']['id']==old_id


def test_limite_de_archivo_y_contrato_de_otra_cuenta(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        from cs_contracts import MAX_BYTES
        assert upload(c,a,b'%PDF-'+b'x'*MAX_BYTES,filename='grande.pdf').status_code==400
        assert CSContrato.query.count()==0
        assert upload(c,a).status_code==302
        doc=CSContrato.query.one()
        admin=client_for(app,f.login())
        assert admin.get(f'/cs/cuentas/{b.id}/contratos/{doc.id}/descargar').status_code==404
        assert admin.post(f'/cs/cuentas/{b.id}/contratos/{doc.id}/activar').status_code==404
        assert doc.activo


def test_contract_without_client_or_source_ids_preserves_pending_and_recurrence(app):
    header=' Servicio ,Recurrencia,Precio Unitario,Detalles,Descuento,Propiedad,Monto Cotizado,Estatus\n'
    data=(header+'Fumigación,Cada 1 mes,604,,0%,Tienda A,604,Agendado\n'
          +'Fumigación,Cada 1 mes el tercer viernes,604,,0%,Tienda B,604,Pendiente\n'
          +'Fumigación,Cada 2 semanas,604,,0%,Tienda A,604,Activo\n'
          +'Fumigación,Cada 1 mes,604,,0%,Tienda C,604,Cancelado\n').encode()
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a,data).status_code==302
        doc=CSContrato.query.one();s=doc.resumen
        assert doc.contenido==data
        assert (s['cliente'],s['registros'],s['activos'],s['pendientes'],s['cancelados'])==('Grupo Haber',4,2,1,1)
        assert s['importe_activos']=='1208' and s['cada_meses'] is None
        assert all(r['id']=='' for r in s['filas'])
        assert s['filas'][0]['cada_meses']==1
        assert s['filas'][1]['cada_meses']==1 and s['filas'][1]['estatus']=='Pendiente'
        assert not s['filas'][1]['activo']
        assert s['filas'][2]['cada_semanas']==2 and s['filas'][2]['cada_meses'] is None
        page=c.get(f'/cs/cuentas/{a.id}/contratos')
        assert b'Sin ID de origen' in page.data and b'pendientes de confirmar' in page.data


def test_optional_client_never_overwrites_an_explicit_different_account(app):
    with app.app_context():
        a,b,c,foreign=setup(app)
        assert upload(c,a,DATA.replace(b'Grupo Haber',b'Otro cliente')).status_code==400
        assert CSContrato.query.count()==0
    assert analyze_csv(DATA.replace(b'Grupo Haber',b''),account_name='Grupo Haber')['cliente']=='Grupo Haber'
    with pytest.raises(ValueError,match='duplicado'):
        analyze_csv(DATA.replace(b'HAB-002',b'HAB-001'),account_name='Grupo Haber')
    with pytest.raises(ValueError,match='encabezados'):
        analyze_csv(DATA.replace(b'Cliente,Servicio',b'Cliente, cliente ,Servicio'),account_name='Grupo Haber')


def test_invalid_cancelled_amount_is_preserved_with_warning_never_counted_as_zero():
    data=DATA.replace(b'Tienda B,600,Cancelado',b'Tienda B,"1,500,00",Cancelado')
    s=analyze_csv(data)
    assert s['importe_activos']=='600' and s['cancelados']==1
    row=s['filas'][1]
    assert row['monto'] is None and row['monto_original']=='1,500,00'
    assert 'Fila 3' in s['avisos'][0] and 'por validar' in s['avisos'][0]
    with pytest.raises(ValueError):analyze_csv(data.replace(b'Cancelado',b'Activo'))
