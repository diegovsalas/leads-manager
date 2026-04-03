# avantex_crm.py
import eventlet
eventlet.monkey_patch()

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
    app.config["META_VERIFY_TOKEN"] = os.getenv("META_VERIFY_TOKEN", "mi_token_secreto")

    # ── Extensiones ────────────────────────────
    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="eventlet")

    # ── Blueprints ─────────────────────────────
    from blueprints.auth       import auth_bp
    from blueprints.webhooks   import webhooks_bp
    from blueprints.leads      import leads_bp
    from blueprints.chat       import chat_bp
    from blueprints.dashboard  import dashboard_bp
    from blueprints.proyecto   import proyecto_bp
    from blueprints.vendedores import vendedores_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(webhooks_bp,   url_prefix="/webhook")
    app.register_blueprint(leads_bp,      url_prefix="/api/leads")
    app.register_blueprint(chat_bp,       url_prefix="/api/chat")
    app.register_blueprint(dashboard_bp,  url_prefix="/api/dashboard")
    app.register_blueprint(proyecto_bp,   url_prefix="/api/proyecto")
    app.register_blueprint(vendedores_bp, url_prefix="/api/vendedores")

    # ── Proteger todas las rutas excepto login y webhooks ──
    @app.before_request
    def require_login():
        allowed = ("/login", "/webhook/", "/static/")
        if any(request.path.startswith(p) for p in allowed):
            return
        if not session.get("user_id"):
            return redirect(url_for("auth.login_page"))

    # ── Ruta principal (vista Kanban + Chat) ───
    from flask import render_template
    from models import EtapaPipeline, Lead

    # Colores para cada etapa del Kanban
    COLORES_ETAPA = {
        EtapaPipeline.NUEVO_LEAD:              "#6366f1",
        EtapaPipeline.CALIFICANDO:             "#0ea5e9",
        EtapaPipeline.PRESENTACION_COTIZACION: "#f59e0b",
        EtapaPipeline.SEGUIMIENTO:             "#8b5cf6",
        EtapaPipeline.CIERRE_GANADO:           "#22c55e",
        EtapaPipeline.CIERRE_PERDIDO:          "#ef4444",
    }

    @app.route("/")
    def index():
        pipeline = {}
        for etapa in EtapaPipeline:
            leads = (
                Lead.query
                .filter_by(etapa_pipeline=etapa)
                .order_by(Lead.fecha_actualizacion.desc())
                .all()
            )
            pipeline[etapa.value] = {
                "etapa_enum":  etapa,
                "etapa_nombre": etapa.value,
                "color":       COLORES_ETAPA.get(etapa, "#6b7280"),
                "leads":       leads,
            }
        return render_template(
            "pipeline/index.html",
            pipeline=pipeline,
            etapas=list(EtapaPipeline),
            user_nombre=session.get("user_nombre", ""),
            user_rol=session.get("user_rol", ""),
        )

    # ── Crear tablas en primera ejecución ──────
    with app.app_context():
        db.create_all()

    return app


# ──────────────────────────────────────────────
# Punto de entrada
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app = create_app()
    # Producción: gunicorn -k eventlet -w 1 "avantex_crm:create_app()"
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
