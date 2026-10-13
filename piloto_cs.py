"""Piloto persistente de CS, exclusivamente en Postgres y navegador locales.

python3 piloto_cs.py --port 5088
No se importa la aplicación hasta fijar y comprobar el entorno aislado.
"""
import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
PILOT_DB = 'avantex_crm_pilot'


def local_database_url(raw):
    parts = urlsplit(raw or '')
    if parts.scheme not in ('postgres', 'postgresql', 'postgresql+psycopg2') or parts.hostname not in ('127.0.0.1', 'localhost', '::1') or parts.query:
        raise ValueError('El piloto requiere DATABASE_URL de .env.local con servidor local.')
    return urlunsplit(('postgresql', parts.netloc, '/' + PILOT_DB, parts.query, ''))


def configure_environment(port=5088):
    from dotenv import dotenv_values
    file = ROOT / '.env.local'
    if not file.is_file():
        raise ValueError('Falta .env.local; el piloto no utiliza la conexión de .env.')
    url = local_database_url(dotenv_values(file).get('DATABASE_URL'))
    # Impide que load_dotenv incorpore las credenciales externas de .env.
    configured = set(dotenv_values(ROOT / '.env'))
    integrations = ('RESEND_', 'ZOHO_', 'SUPABASE_', 'WHATSAPP_', 'GOOGLE_', 'META_', 'SAVIO_', 'SHOPIFY_', 'BAILEYS_', 'AIRCALL_', 'APOLLO_', 'LUSHA_', 'LEMLIST_', 'ANTHROPIC_', 'OPENAI_', 'SMTP_', 'MAIL_', 'BOT_')
    for key in configured | {key for key in os.environ if key.startswith(integrations)}:
        os.environ[key] = ''
    os.environ.update(DATABASE_URL=url, SECRET_KEY=secrets.token_hex(32),
        META_VERIFY_TOKEN='cs-local-pilot', SCHEDULER_EN_PROCESO='0',
        CS_SCHEMA_AUTO_MIGRATE='1', FLASK_ENV='development',
        DOMAIN=f'http://127.0.0.1:{port}', CRM_STORAGE_LOCAL_DIR=str(ROOT / '.cs_pilot' / 'evidencias'))
    import psycopg2
    from psycopg2 import sql
    parts = urlsplit(url)
    admin = urlunsplit((parts.scheme, parts.netloc, '/postgres', parts.query, ''))
    conn = psycopg2.connect(admin, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_database WHERE datname=%s', (PILOT_DB,))
            if not cursor.fetchone():
                cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(PILOT_DB)))
    finally:
        conn.close()
    return url


def seed_pilot():
    from datetime import datetime, timedelta
    from extensions import db
    from models import UserCRM, RolCRM, CSAccount, CSContacto, CSIncidencia, CSWeeklyReport, CSGestion
    from cs_daily import new_task, weekly_tasks
    from cs_weekly import MX, latest_cut, validate_payload
    users = {}
    for key, name, role in [('kam1','Francisco Rodriguez · piloto',RolCRM.KAM),('kam2','Katia Gutierrez · piloto',RolCRM.KAM),('nalleli','Nalleli Quiroz · piloto',RolCRM.KAM),('direccion','Dirección · piloto',RolCRM.SUPER_ADMIN)]:
        email = key + '@cs-pilot.invalid'
        user = UserCRM.query.filter_by(correo=email).first()
        if not user:
            user = UserCRM(nombre=name,correo=email,rol=role,activo=True)
            user.set_password(secrets.token_hex(24));db.session.add(user);db.session.flush()
        elif user.nombre in ('KAM 1 · piloto','KAM 2 · piloto'):
            user.nombre = name
        users[key] = user
    today, cut = datetime.now(MX).date(), latest_cut()
    for key in ('kam1','kam2'):
        kam = users[key]
        for n, title in enumerate(('Retail de demostración','Hoteles de demostración','Corporativo de demostración'),1):
            client_id = f'P{key[-1]}-{n:03}'
            if CSAccount.query.filter_by(client_id=client_id).first():
                continue  # Reiniciar conserva la gestión hecha por los participantes.
            account = CSAccount(client_id=client_id,nombre=f'{title} · {key[-1]}',kam_id=kam.id,es_aaa=n<3,unidades_contratadas='AROMATEX')
            db.session.add(account);db.session.flush()
            db.session.add(CSContacto(account_id=account.id,nombre='Contacto de demostración',correo=f'contacto{key[-1]}{n}@example.invalid',is_owner=True))
            due = today + timedelta(days=(-2,0,3)[n-1])
            task = new_task(account,dict(descripcion='Confirmar calendario con el cliente',fecha_limite=due.isoformat()),kam.nombre,str(kam.id))
            # Fixture histórica para que el corte de ejemplo incluya compromisos.
            task.created_at = datetime.combine(cut-timedelta(days=4),datetime.min.time(),MX)
            db.session.flush()
            CSGestion.query.filter_by(task_id=task.id).one().created_at=task.created_at
            if n==1:
                new_task(account,dict(descripcion='Resolver autorización de acceso',source='blockers',apoyo='Operaciones: confirmar acceso a sucursal',fecha_limite=today.isoformat()),kam.nombre,str(kam.id))
                db.session.add(CSIncidencia(account_id=account.id,tipo='Visita pendiente',detalle='Caso ficticio para practicar seguimiento.',responsable=kam.nombre,fecha_incidencia=today-timedelta(days=3),fecha_compromiso=today-timedelta(days=1)))
                metrics=dict(prev_expected=8,prev_done=8,prev_billed=20000,prev_target=20000,expected=8,done=4,scheduled=4,forecast=8,billed=10000,target=20000,billing_forecast=20000)
                payload=validate_payload({'metrics':metrics});payload.update(weekly_tasks(account.id,cut))
                db.session.add(CSWeeklyReport(account_id=account.id,cut_date=cut,payload=payload,updated_by='Datos de demostración'))
    db.session.commit()
    return {key:str(user.id) for key,user in users.items()}


def build_app(port=5088):
    configure_environment(port)
    from avantex_crm import create_app
    from extensions import db
    from flask import session, request, redirect, render_template, abort
    from models import UserCRM
    app = create_app()
    app.config['LEAD_ASSIGNMENT_EMAIL_DISPATCH'] = False
    with app.app_context():
        db.create_all()
        users = seed_pilot()
    allowed = {'cs.dashboard','cs.seguimiento_semanal','cs.reporte_semanal','cs.revisar_reporte_semanal','cs.cuentas_aaa',
        'cs.incidencias_view','cs.incidencias_por_cliente','cs.crear_incidencia','cs.cambiar_status_incidencia','cs.seguimiento_incidencia',
        'cs.account_detail','cs.account_tab','static','pilot_home','pilot_login'}

    def pilot_entry():
        if request.remote_addr not in ('127.0.0.1','::1'):
            abort(403)
        if request.path.startswith('/cs/') and not session.get('user_id'):
            return redirect('/auth/google/__pilot')
    app.before_request_funcs.setdefault(None,[]).insert(0,pilot_entry)

    @app.before_request
    def isolate_pilot():
        if request.remote_addr not in ('127.0.0.1','::1'):
            abort(403)
        if request.endpoint not in allowed and not (request.endpoint or '').startswith('cs_daily.'):
            return 'Esta función no forma parte del piloto local. Vuelve a Hoy o cambia de perfil.',403

    @app.context_processor
    def pilot_context():
        return {'cs_pilot':True}

    @app.route('/auth/google/__pilot')
    def pilot_home():
        return render_template('cs/cs_piloto.html', profiles={key:db.session.get(UserCRM,uid).nombre.replace(' · piloto','') for key,uid in users.items()})

    @app.route('/auth/google/__pilot/entrar/<profile>')
    def pilot_login(profile):
        if profile not in users:
            abort(404)
        user = db.session.get(UserCRM,users[profile])
        session.clear()
        session.update(user_id=str(user.id),user_rol=user.rol.value,user_nombre=user.nombre)
        return redirect('/cs/')
    return app


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=5088)
    args=parser.parse_args()
    os.chdir(ROOT)
    app=build_app(args.port)
    print(f'Piloto local: http://127.0.0.1:{args.port}/auth/google/__pilot',flush=True)
    app.run(host='127.0.0.1',port=args.port,use_reloader=False,debug=False)
