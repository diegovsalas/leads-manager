# avantex_crm.py
from gevent import monkey
monkey.patch_all()

# psycopg2 es bloqueante por default y mata al worker bajo gevent.
# psycogreen hace que sus queries cedan al loop. CRÍTICO antes de
# importar SQLAlchemy/psycopg2.
from psycogreen.gevent import patch_psycopg
patch_psycopg()

import os
from dotenv import load_dotenv

load_dotenv()

from datetime import timedelta
from flask import Flask, session, redirect, url_for, request
from extensions import db, socketio


# ──────────────────────────────────────────────
# Factory function — patrón recomendado con Blueprints
# ──────────────────────────────────────────────
def create_app():
    app = Flask(__name__)

    # ── Configuración ──────────────────────────
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-key")

    # Supabase/Render usan postgres:// pero SQLAlchemy requiere postgresql://
    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/avantex_crm")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    # Variables de Meta/WhatsApp accesibles en toda la app
    app.config["WHATSAPP_TOKEN"]    = os.getenv("WHATSAPP_TOKEN", "")
    app.config["WHATSAPP_PHONE_ID"] = os.getenv("WHATSAPP_PHONE_ID", "")
    app.config["META_VERIFY_TOKEN"] = os.getenv("META_VERIFY_TOKEN", "avantex-verify-2026")

    # ── Extensiones ────────────────────────────
    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent")

    # ── Blueprints ─────────────────────────────
    from blueprints.auth       import auth_bp
    from blueprints.webhooks   import webhooks_bp
    from blueprints.leads      import leads_bp
    from blueprints.chat       import chat_bp
    from blueprints.dashboard  import dashboard_bp
    from blueprints.proyecto   import proyecto_bp
    from blueprints.vendedores import vendedores_bp
    from blueprints.metas         import metas_bp
    from blueprints.cotizaciones  import cotizaciones_bp
    from blueprints.apikeys       import apikeys_bp
    from blueprints.api_v1        import api_v1_bp
    from blueprints.api_v2        import api_v2_bp
    from blueprints.cs            import cs_bp
    from blueprints.encuesta      import encuesta_bp
    from blueprints.savio         import savio_bp
    from blueprints.sdr           import sdr_bp
    from blueprints.sdr_directivo import sdr_directivo_bp, lemlist_webhook_bp
    from blueprints.sales         import sales_bp, clients_bp
    from blueprints.costs         import costs_bp
    from blueprints.aircall       import aircall_bp
    from blueprints.zoho          import zoho_bp
    from blueprints.cs_extras     import touchpoints_bp, weekly_kpis_bp, assignments_bp
    from blueprints.scip          import scip_bp
    from blueprints.chatbot       import chatbot_bp
    from blueprints.oportunidades import oportunidades_bp
    from blueprints.accounts      import accounts_bp, contacts_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(webhooks_bp,      url_prefix="/webhook")
    app.register_blueprint(leads_bp,         url_prefix="/api/leads")
    app.register_blueprint(chat_bp,          url_prefix="/api/chat")
    app.register_blueprint(dashboard_bp,     url_prefix="/api/dashboard")
    app.register_blueprint(proyecto_bp,      url_prefix="/api/proyecto")
    app.register_blueprint(vendedores_bp,    url_prefix="/api/vendedores")
    app.register_blueprint(metas_bp,         url_prefix="/api/metas")
    app.register_blueprint(cotizaciones_bp,  url_prefix="/api/cotizaciones")
    app.register_blueprint(apikeys_bp,       url_prefix="/api/keys")
    app.register_blueprint(api_v1_bp,        url_prefix="/api/v1")
    app.register_blueprint(api_v2_bp,       url_prefix="/api/v2")
    app.register_blueprint(cs_bp,            url_prefix="/cs")
    app.register_blueprint(encuesta_bp,      url_prefix="/encuesta")
    app.register_blueprint(savio_bp,         url_prefix="/api/savio")
    app.register_blueprint(sdr_bp,           url_prefix="/api/sdr")
    app.register_blueprint(sdr_directivo_bp, url_prefix="/api/sdr-directivo")
    app.register_blueprint(lemlist_webhook_bp, url_prefix="/api/webhooks")
    app.register_blueprint(sales_bp,         url_prefix="/api/sales")
    app.register_blueprint(clients_bp,       url_prefix="/api/clients")
    app.register_blueprint(costs_bp,         url_prefix="/api/costs")
    app.register_blueprint(aircall_bp,       url_prefix="/api/aircall")
    app.register_blueprint(zoho_bp,          url_prefix="/api/zoho")
    app.register_blueprint(touchpoints_bp,   url_prefix="/api/touchpoints")
    app.register_blueprint(weekly_kpis_bp,   url_prefix="/api/weekly-kpis")
    app.register_blueprint(assignments_bp,   url_prefix="/api/assignments")
    app.register_blueprint(scip_bp,          url_prefix="/api/scip")
    app.register_blueprint(chatbot_bp,       url_prefix="/api/chatbot")
    app.register_blueprint(oportunidades_bp, url_prefix="/api/oportunidades")
    app.register_blueprint(accounts_bp,      url_prefix="/api/accounts")
    app.register_blueprint(contacts_bp,      url_prefix="/api/contacts")

    # Serve React app at /app/
    @app.route("/app/")
    @app.route("/app/<path:path>")
    def serve_react(path=""):
        from flask import send_from_directory
        static_dir = os.path.join(app.root_path, "static", "app")
        if path and os.path.exists(os.path.join(static_dir, path)):
            return send_from_directory(static_dir, path)
        return send_from_directory(static_dir, "index.html")

    # ── Proteger todas las rutas excepto login y webhooks ──
    @app.before_request
    def require_login():
        allowed = ("/login", "/auth/google", "/webhook/", "/static/", "/api/v1/", "/encuesta/")
        if any(request.path.startswith(p) for p in allowed):
            return
        if not session.get("user_id"):
            return redirect(url_for("auth.login_page"))
        # KAMs solo pueden acceder a /cs/ y /logout
        if session.get("user_rol", "").upper() == "KAM":
            if not request.path.startswith("/cs/") and request.path != "/logout":
                return redirect("/cs/")

    # ── Ruta principal (vista Kanban + Chat) ───
    from flask import render_template
    from models import EtapaPipeline, Lead

    # Colores para cada etapa del Kanban
    COLORES_ETAPA = {
        EtapaPipeline.NUEVO_LEAD:     "#6366f1",
        EtapaPipeline.CONTACTO_1:     "#9333ea",
        EtapaPipeline.CONTACTO_2:     "#9333ea",
        EtapaPipeline.CONTACTO_3:     "#9333ea",
        EtapaPipeline.CONTACTO_4:     "#9333ea",
        EtapaPipeline.COTIZACION:     "#2563eb",
        EtapaPipeline.DEMO:           "#0891b2",
        EtapaPipeline.NEGOCIACION:    "#d97706",
        EtapaPipeline.CIERRE_GANADO:  "#16a34a",
        EtapaPipeline.CIERRE_PERDIDO: "#dc2626",
    }

    # Agrupación tipo Zoho: cada etapa pertenece a una fase del funnel.
    # Se muestra como label arriba del header de cada columna.
    GRUPO_ETAPA = {
        EtapaPipeline.NUEVO_LEAD:     ("Nuevos",       "#6366f1"),
        EtapaPipeline.CONTACTO_1:     ("En contacto",  "#9333ea"),
        EtapaPipeline.CONTACTO_2:     ("En contacto",  "#9333ea"),
        EtapaPipeline.CONTACTO_3:     ("En contacto",  "#9333ea"),
        EtapaPipeline.CONTACTO_4:     ("En contacto",  "#9333ea"),
        EtapaPipeline.COTIZACION:     ("Negociando",   "#d97706"),
        EtapaPipeline.DEMO:           ("Negociando",   "#d97706"),
        EtapaPipeline.NEGOCIACION:    ("Negociando",   "#d97706"),
        EtapaPipeline.CIERRE_GANADO:  ("Cerrado",      "#16a34a"),
        EtapaPipeline.CIERRE_PERDIDO: ("Cerrado",      "#dc2626"),
    }

    @app.route("/")
    def index():
        from sqlalchemy.orm import joinedload
        q = Lead.query.options(joinedload(Lead.usuario_asignado))

        # Vendedores solo ven sus leads, Super Admin ve todo
        user_rol = session.get("user_rol", "")
        if user_rol.upper() == "VENDEDOR":
            usuario_id = session.get("usuario_id")
            if usuario_id:
                q = q.filter(Lead.usuario_asignado_id == usuario_id)

        all_leads = q.order_by(Lead.fecha_actualizacion.desc()).all()
        leads_by_etapa = {}
        for lead in all_leads:
            leads_by_etapa.setdefault(lead.etapa_pipeline, []).append(lead)

        pipeline = {}
        for etapa in EtapaPipeline:
            grupo_nombre, grupo_color = GRUPO_ETAPA.get(etapa, ("", "#6b7280"))
            pipeline[etapa.value] = {
                "etapa_enum":   etapa,
                "etapa_nombre": etapa.value,
                "color":        COLORES_ETAPA.get(etapa, "#6b7280"),
                "grupo":        grupo_nombre,
                "grupo_color":  grupo_color,
                "leads":        leads_by_etapa.get(etapa, []),
            }
        from meta_conversions import get_pixel_ids
        return render_template(
            "pipeline/index.html",
            pipeline=pipeline,
            etapas=list(EtapaPipeline),
            user_nombre=session.get("user_nombre", ""),
            user_rol=session.get("user_rol", ""),
            usuario_id=session.get("usuario_id", ""),
            meta_pixels=get_pixel_ids(),
        )

    # ── Crear tablas en primera ejecución ──────
    with app.app_context():
        db.create_all()
        # Agregar nuevos valores al enum origen_lead si no existen
        for val in ("Upselling", "Cross-selling"):
            try:
                db.session.execute(db.text(f"ALTER TYPE origen_lead_enum ADD VALUE IF NOT EXISTS '{val}'"))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # precio_unitario en cs_appointments (para datos de Zoho Analytics)
        try:
            db.session.execute(db.text(
                "ALTER TABLE cs_appointments ADD COLUMN IF NOT EXISTS precio_unitario NUMERIC(12,2)"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # ── Cadencia automatica (cada 15 minutos) ──
    _start_scheduler(app)

    return app


def _start_scheduler(app):
    """Inicia APScheduler para cadencia (15 min) y notificaciones (9am CST diario)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        def _run_cadencia():
            with app.app_context():
                from cadencia import check_cadencia
                check_cadencia()

        def _run_notificaciones():
            with app.app_context():
                from notificaciones import enviar_notificaciones_diarias
                enviar_notificaciones_diarias()

        def _run_backup():
            with app.app_context():
                from backups import ejecutar_backup
                ejecutar_backup()

        def _run_savio_invoices_payments():
            with app.app_context():
                import savio_sync
                try:
                    savio_sync.sync_invoices()
                    savio_sync.sync_payments()
                except Exception as e:
                    app.logger.warning(f"savio hourly: {e}")

        def _run_savio_customers_subs():
            with app.app_context():
                import savio_sync
                try:
                    savio_sync.sync_subscriptions()
                    savio_sync.sync_customers()
                    savio_sync.bridge_savio_to_cs_mrr()
                    savio_sync.sync_savio_to_cs_invoices()
                except Exception as e:
                    app.logger.warning(f"savio 6h: {e}")

        def _run_savio_boot():
            """Sync ligero inicial 60s post-boot. Solo customers (rápido).
            invoices+payments salen del job horario; subscriptions del 6h."""
            with app.app_context():
                import savio_sync
                try:
                    savio_sync.sync_customers()
                except Exception as e:
                    app.logger.warning(f"savio boot sync: {e}")

        def _run_sdr_engine_for_unit(unit: str):
            with app.app_context():
                import sdr_directivo_engine as engine
                try:
                    engine.engine_run_daily_batch(unit=unit)
                except Exception as e:
                    app.logger.warning(f"sdr engine ({unit}): {e}")

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(_run_cadencia, "interval", minutes=15, id="cadencia_followup")
        # Notificaciones diarias a las 9:00 AM CST (UTC-6 = 15:00 UTC)
        scheduler.add_job(
            _run_notificaciones, "cron",
            hour=15, minute=0,  # 15:00 UTC = 9:00 AM CST
            id="notificaciones_diarias",
        )
        # Backup diario a las 3:00 AM CST (09:00 UTC)
        scheduler.add_job(
            _run_backup, "cron",
            hour=9, minute=0,  # 09:00 UTC = 3:00 AM CST
            id="backup_diario",
        )
        # Savio: solo si la API key está configurada
        if os.getenv("SAVIO_API_KEY"):
            from datetime import datetime, timedelta
            scheduler.add_job(
                _run_savio_boot, "date",
                run_date=datetime.now() + timedelta(seconds=60),
                id="savio_boot_sync",
            )
            scheduler.add_job(_run_savio_invoices_payments, "interval", hours=1, id="savio_hourly")
            scheduler.add_job(_run_savio_customers_subs, "interval", hours=6, id="savio_6h")
            app.logger.info("Savio scheduler activo (boot+30s, hourly inv+pay, 6h cust+subs)")
        else:
            app.logger.info("SAVIO_API_KEY no configurada — scheduler Savio desactivado")

        # SDR Directivo Engine: cron diario por unidad. Lee cron_hour/cron_minute
        # de sdr_dir_engine_config en cada boot. Solo arma jobs para unidades con config.
        try:
            with app.app_context():
                from models import SdrDirEngineConfig
                for cfg in SdrDirEngineConfig.query.all():
                    scheduler.add_job(
                        _run_sdr_engine_for_unit, "cron",
                        hour=cfg.cron_hour or 9,
                        minute=cfg.cron_minute or 0,
                        args=[cfg.unit],
                        id=f"sdr_engine_{cfg.unit}",
                        replace_existing=True,
                    )
                    app.logger.info(
                        f"SDR engine cron registrado: {cfg.unit} @ "
                        f"{cfg.cron_hour:02d}:{cfg.cron_minute:02d} UTC"
                    )
        except Exception as e:
            app.logger.warning(f"SDR engine scheduler setup: {e}")

        # Meta Lead Ads polling (cada 5 min) — alternativa al webhook mientras la App no está publicada
        if os.getenv("META_PAGE_TOKEN"):
            def _run_meta_polling():
                with app.app_context():
                    from meta_lead_polling import poll_and_create_leads
                    result = poll_and_create_leads()
                    if result.get("leads_created", 0) > 0:
                        app.logger.info(f"Meta polling: {result}")

            scheduler.add_job(_run_meta_polling, "interval", minutes=5, id="meta_lead_polling")
            app.logger.info("Meta Lead Ads polling activo (cada 5 min)")

        # LinkedIn Lead Gen Forms polling (cada 5 min)
        if os.getenv("LINKEDIN_ACCESS_TOKEN"):
            def _run_linkedin_polling():
                with app.app_context():
                    from linkedin_lead_polling import poll_and_create_leads
                    result = poll_and_create_leads()
                    if result.get("leads_created", 0) > 0:
                        app.logger.info(f"LinkedIn polling: {result}")

            scheduler.add_job(_run_linkedin_polling, "interval", minutes=5, id="linkedin_lead_polling")
            app.logger.info("LinkedIn Lead Gen polling activo (cada 5 min)")

        scheduler.start()
        app.logger.info("Scheduler iniciado: cadencia (15 min) + notificaciones (9am) + backup (3am)")
    except Exception as e:
        app.logger.warning(f"No se pudo iniciar scheduler: {e}")


# ──────────────────────────────────────────────
# Punto de entrada
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app = create_app()
    # Producción: gunicorn -k gevent -w 1 "avantex_crm:create_app()"
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
