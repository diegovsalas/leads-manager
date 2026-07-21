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
from extensions import db, socketio, limiter


# ──────────────────────────────────────────────
# Factory function — patrón recomendado con Blueprints
# ──────────────────────────────────────────────
def _run_pending_migrations(app):
    """Migraciones de columna idempotentes que corren en cada boot.
    Cada bloque verifica si el cambio ya está aplicado antes de tocar la DB.
    Si algo falla, loguea y sigue — la app igual arranca."""
    from sqlalchemy import text
    with app.app_context():
        # ─── rol_crm_enum: perfiles segmentados + Developer ───
        try:
            with db.engine.begin() as conn:
                for value in (
                    "Developer",
                    "Super Admin Aromatex",
                    "Super Admin Pestex",
                    "Super Admin Comercial",
                    "Super Admin Nexo",
                    "Gerente Comercial Aromatex",
                ):
                    conn.execute(text(
                        f"ALTER TYPE rol_crm_enum ADD VALUE IF NOT EXISTS '{value}'"
                    ))
        except Exception as e:
            app.logger.warning("[auto-migrate] rol_crm_enum scoped roles failed: %s", e)

        # ─── Developer exclusivo Diego Velazquez ───
        try:
            with db.engine.begin() as conn:
                conn.execute(text("""
                    UPDATE users_crm
                    SET rol = 'Developer'
                    WHERE lower(correo) = 'diegovelazquez@grupoavantex.com'
                """))
        except Exception as e:
            app.logger.warning("[auto-migrate] seed Diego Developer failed: %s", e)

        # ─── accounts.client_id (EMP-XXXX) ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'accounts' AND column_name = 'client_id'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding accounts.client_id...")
                    conn.execute(text("ALTER TABLE accounts ADD COLUMN client_id VARCHAR(10)"))
                    conn.execute(text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_accounts_client_id "
                        "ON accounts (client_id)"
                    ))
                    # Backfill EMP-XXXX en orden de fecha_creacion
                    rows = conn.execute(text(
                        "SELECT id FROM accounts WHERE client_id IS NULL "
                        "ORDER BY fecha_creacion ASC NULLS LAST, nombre ASC"
                    )).fetchall()
                    for i, row in enumerate(rows, start=1):
                        conn.execute(
                            text("UPDATE accounts SET client_id = :cid WHERE id = :id"),
                            {"cid": f"EMP-{i:04d}", "id": row[0]},
                        )
                    app.logger.info("[auto-migrate] backfilled %d empresas with EMP-XXXX", len(rows))
        except Exception as e:
            app.logger.warning("[auto-migrate] accounts.client_id failed (retry on next boot): %s", e)

        # ─── leads.tipo_venta (Eventual / Recurrente) ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'leads' AND column_name = 'tipo_venta'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding leads.tipo_venta...")
                    conn.execute(text("ALTER TABLE leads ADD COLUMN tipo_venta VARCHAR(40)"))
                    app.logger.info("[auto-migrate] leads.tipo_venta added.")
        except Exception as e:
            app.logger.warning("[auto-migrate] leads.tipo_venta failed (retry on next boot): %s", e)

        # ─── EtapaPipeline enum: agregar 'Presentación' ───
        try:
            # ALTER TYPE ADD VALUE no soporta IF NOT EXISTS en algunas versiones —
            # validar antes via pg_enum
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM pg_enum e
                    JOIN pg_type t ON e.enumtypid = t.oid
                    WHERE t.typname = 'etapa_pipeline_enum'
                      AND e.enumlabel = 'Presentación'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding 'Presentación' to etapa_pipeline_enum...")
                    # ALTER TYPE ADD VALUE no puede correr dentro de un block transaction
                    # de algunos drivers. Usamos autocommit aislado.
                    conn.execute(text(
                        "ALTER TYPE etapa_pipeline_enum ADD VALUE 'Presentación' AFTER '4to Contacto'"
                    ))
                    app.logger.info("[auto-migrate] 'Presentación' added to etapa_pipeline_enum.")
        except Exception as e:
            app.logger.warning("[auto-migrate] etapa_pipeline 'Presentación' failed (retry on next boot): %s", e)

        # ─── leads.telefono: quitar UNIQUE (FEAT-2026-06-29) ───
        # Un cliente puede tener N leads (recurrente + eventual + repetidas).
        # El UNIQUE bloqueaba registrar 'otra venta al mismo cliente'.
        # Reemplazamos por un índice NO único para mantener performance de búsqueda.
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'leads'::regclass
                      AND conname = 'leads_telefono_key'
                """)).first()
                if exists:
                    app.logger.info("[auto-migrate] dropping UNIQUE leads_telefono_key...")
                    conn.execute(text("ALTER TABLE leads DROP CONSTRAINT leads_telefono_key"))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_leads_telefono ON leads (telefono)"
                    ))
        except Exception as e:
            app.logger.warning("[auto-migrate] drop unique leads.telefono failed: %s", e)

        # ─── sales.opportunity_id: una Oportunidad ganada = una Venta ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'sales' AND column_name = 'opportunity_id'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding sales.opportunity_id...")
                    conn.execute(text("ALTER TABLE sales ADD COLUMN opportunity_id UUID"))
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_opportunity_id "
                    "ON sales (opportunity_id) WHERE opportunity_id IS NOT NULL"
                ))
                fk_exists = conn.execute(text("""
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'sales'::regclass
                      AND conname = 'sales_opportunity_id_fkey'
                """)).first()
                if not fk_exists:
                    conn.execute(text("""
                        UPDATE sales s
                        SET opportunity_id = NULL
                        WHERE opportunity_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM oportunidades o WHERE o.id = s.opportunity_id
                          )
                    """))
                    conn.execute(text("""
                        ALTER TABLE sales
                        ADD CONSTRAINT sales_opportunity_id_fkey
                        FOREIGN KEY (opportunity_id)
                        REFERENCES oportunidades(id)
                        ON DELETE SET NULL
                    """))
        except Exception as e:
            app.logger.warning("[auto-migrate] sales.opportunity_id failed: %s", e)

        # ─── Account/Contact FKs reales para evitar referencias huérfanas ───
        try:
            with db.engine.begin() as conn:
                for table in ("leads", "oportunidades"):
                    for col, parent in (("account_id", "accounts"), ("contact_id", "contacts")):
                        cname = f"{table}_{col}_fkey"
                        exists = conn.execute(text("""
                            SELECT 1 FROM pg_constraint
                            WHERE conrelid = to_regclass(:table)
                              AND conname = :cname
                        """), {"table": table, "cname": cname}).first()
                        if exists:
                            continue
                        conn.execute(text(f"""
                            UPDATE {table} child
                            SET {col} = NULL
                            WHERE {col} IS NOT NULL
                              AND NOT EXISTS (
                                SELECT 1 FROM {parent} p WHERE p.id = child.{col}
                              )
                        """))
                        conn.execute(text(f"""
                            ALTER TABLE {table}
                            ADD CONSTRAINT {cname}
                            FOREIGN KEY ({col})
                            REFERENCES {parent}(id)
                            ON DELETE SET NULL
                        """))
        except Exception as e:
            app.logger.warning("[auto-migrate] account/contact FKs failed: %s", e)

        # ─── metas_vendedor: meta_recurrente_mxn + meta_eventual_mxn (FEAT-2026-06-25) ───
        try:
            with db.engine.begin() as conn:
                for col in ("meta_recurrente_mxn", "meta_eventual_mxn"):
                    exists = conn.execute(text("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'metas_vendedor' AND column_name = :c
                    """), {"c": col}).first()
                    if not exists:
                        app.logger.info(f"[auto-migrate] adding metas_vendedor.{col}...")
                        conn.execute(text(
                            f"ALTER TABLE metas_vendedor ADD COLUMN {col} NUMERIC(12,2)"
                        ))
                # HOTFIX-2026-06-25: drop NOT NULL del meta_mxn legacy. El modelo
                # dice nullable=True pero la columna en prod nació NOT NULL desde
                # el create_all() original. Al hacer INSERT solo con
                # meta_recurrente_mxn/meta_eventual_mxn, meta_mxn queda NULL y la
                # constraint vieja explota con NotNullViolation.
                col_info = conn.execute(text("""
                    SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 'metas_vendedor' AND column_name = 'meta_mxn'
                """)).first()
                if col_info and col_info[0] == "NO":
                    app.logger.info("[auto-migrate] dropping NOT NULL on metas_vendedor.meta_mxn...")
                    conn.execute(text(
                        "ALTER TABLE metas_vendedor ALTER COLUMN meta_mxn DROP NOT NULL"
                    ))
        except Exception as e:
            app.logger.warning("[auto-migrate] metas_vendedor split rec/ev failed: %s", e)

        # ─── usuarios.correos_visibles_para_user_id (FEAT-2026-07-08) ───
        # Restricción de privacidad: si está seteado, SOLO ese users_crm puede
        # ver los correos de este vendedor. NULL = comportamiento actual
        # (todos los super_admin ven). Requerido por Diego para Andres Garza.
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'usuarios' AND column_name = 'correos_visibles_para_user_id'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding usuarios.correos_visibles_para_user_id...")
                    conn.execute(text(
                        "ALTER TABLE usuarios ADD COLUMN correos_visibles_para_user_id UUID"
                    ))
                    # Seed inicial: Andres Garza Martinez → Diego Velazquez
                    # (identificados por nombre para robustez si los UUIDs cambian)
                    conn.execute(text("""
                        UPDATE usuarios u
                        SET correos_visibles_para_user_id = (
                            SELECT id FROM users_crm
                            WHERE nombre ILIKE '%diego velazquez%'
                            LIMIT 1
                        )
                        WHERE u.nombre ILIKE '%andres%garza%martinez%'
                    """))
                    app.logger.info("[auto-migrate] seed: Andres Garza → Diego privacy set")
        except Exception as e:
            app.logger.warning("[auto-migrate] correos_visibles_para_user_id failed: %s", e)

        # ─── usuarios.email_signature: firma para correos enviados desde CRM ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'usuarios' AND column_name = 'email_signature'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding usuarios.email_signature...")
                    conn.execute(text("ALTER TABLE usuarios ADD COLUMN email_signature TEXT"))
        except Exception as e:
            app.logger.warning("[auto-migrate] usuarios.email_signature failed: %s", e)

        # ─── usuarios.gmail_backfilled_in_at (FEAT-2026-07-07) ───
        # Trackeo separado del backfill de recibidos (IN). El original
        # gmail_backfilled_at seguirá aplicando solo a OUT.
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'usuarios' AND column_name = 'gmail_backfilled_in_at'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding usuarios.gmail_backfilled_in_at...")
                    conn.execute(text(
                        "ALTER TABLE usuarios ADD COLUMN gmail_backfilled_in_at TIMESTAMPTZ"
                    ))
        except Exception as e:
            app.logger.warning("[auto-migrate] usuarios.gmail_backfilled_in_at failed: %s", e)

        # ─── sales_emails.direccion (FEAT-2026-07-07): 'IN' entrantes / 'OUT' salientes ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'sales_emails' AND column_name = 'direccion'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] adding sales_emails.direccion...")
                    conn.execute(text(
                        "ALTER TABLE sales_emails ADD COLUMN direccion VARCHAR(4) DEFAULT 'OUT'"
                    ))
                    # Los existentes fueron pollados de in:sent → todos OUT (correcto default)
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_sales_emails_direccion ON sales_emails (direccion)"
                    ))
        except Exception as e:
            app.logger.warning("[auto-migrate] sales_emails.direccion failed: %s", e)

        # ─── cs_accounts: Due Diligence (FEAT-2026-07-06) ───
        # Para cuentas adquiridas que aún no tienen KAM asignado (ej. Fugaci).
        # NO cuentan en KPIs actuales del dashboard hasta que se 'promocionan'.
        try:
            with db.engine.begin() as conn:
                cols_a_agregar = [
                    ("en_due_diligence",  "BOOLEAN DEFAULT FALSE"),
                    ("origen_adquisicion", "VARCHAR(80)"),
                    ("dd_metadata",       "JSONB"),
                ]
                for col, ddl in cols_a_agregar:
                    exists = conn.execute(text("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'cs_accounts' AND column_name = :c
                    """), {"c": col}).first()
                    if not exists:
                        app.logger.info(f"[auto-migrate] adding cs_accounts.{col}...")
                        conn.execute(text(f"ALTER TABLE cs_accounts ADD COLUMN {col} {ddl}"))
                # kam_id → nullable (para cuentas DD sin KAM asignado)
                col_info = conn.execute(text("""
                    SELECT is_nullable FROM information_schema.columns
                    WHERE table_name = 'cs_accounts' AND column_name = 'kam_id'
                """)).first()
                if col_info and col_info[0] == "NO":
                    app.logger.info("[auto-migrate] dropping NOT NULL on cs_accounts.kam_id...")
                    conn.execute(text(
                        "ALTER TABLE cs_accounts ALTER COLUMN kam_id DROP NOT NULL"
                    ))
                # Index para filtrar rápido por en_due_diligence
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_cs_accounts_dd "
                    "ON cs_accounts (en_due_diligence) WHERE en_due_diligence = TRUE"
                ))
        except Exception as e:
            app.logger.warning("[auto-migrate] cs_accounts DD columns failed: %s", e)

        # ─── cs_invoices.savio_invoice_id UNIQUE (SECURITY-2026-06-24) ───
        # Sin esto, una corrida con bug del sync podía duplicar filas de la
        # misma factura de Savio y romper el cálculo de facturación / MRR.
        try:
            with db.engine.begin() as conn:
                # Dedup defensivo: borra duplicados antes de crear el unique.
                # Conserva la fila con id más bajo por savio_invoice_id.
                conn.execute(text("""
                    DELETE FROM cs_invoices a
                    USING cs_invoices b
                    WHERE a.savio_invoice_id IS NOT NULL
                      AND a.savio_invoice_id = b.savio_invoice_id
                      AND a.id > b.id
                """))
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_invoices_savio_invoice_id "
                    "ON cs_invoices (savio_invoice_id) WHERE savio_invoice_id IS NOT NULL"
                ))
        except Exception as e:
            app.logger.warning("[auto-migrate] cs_invoices unique savio_invoice_id failed: %s", e)

        # ─── cs_invoices.cs_import_key UNIQUE (CSV cobros idempotente) ───
        # La carga manual de cobros no trae savio_invoice_id. Esta llave evita
        # duplicar facturas al re-subir el mismo CSV y permite upsert.
        try:
            with db.engine.begin() as conn:
                conn.execute(text(
                    "ALTER TABLE cs_invoices ADD COLUMN IF NOT EXISTS cs_import_key VARCHAR(80)"
                ))
                conn.execute(text("""
                    UPDATE cs_invoices
                    SET cs_import_key = CASE
                        WHEN NULLIF(BTRIM(folio), '') IS NOT NULL THEN
                            'csv:' || md5(concat_ws('|',
                                'folio',
                                account_id::text,
                                lower(BTRIM(coalesce(serie, ''))),
                                lower(BTRIM(folio))
                            ))
                        ELSE
                            'csv:' || md5(concat_ws('|',
                                'fallback',
                                account_id::text,
                                lower(BTRIM(coalesce(concepto, ''))),
                                coalesce(fecha_cobro::text, ''),
                                trim(to_char(coalesce(total, 0), '999999999999990.00'))
                            ))
                    END
                    WHERE cs_import_key IS NULL
                      AND savio_invoice_id IS NULL
                """))
                conn.execute(text("""
                    DELETE FROM cs_invoices a
                    USING cs_invoices b
                    WHERE a.cs_import_key IS NOT NULL
                      AND a.cs_import_key = b.cs_import_key
                      AND a.id > b.id
                """))
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_cs_invoices_import_key "
                    "ON cs_invoices (cs_import_key) WHERE cs_import_key IS NOT NULL"
                ))
        except Exception as e:
            app.logger.warning("[auto-migrate] cs_invoices import key failed: %s", e)

        # ─── kam_email_responses (2026-07-03) ───
        try:
            with db.engine.begin() as conn:
                exists = conn.execute(text("""
                    SELECT 1 FROM information_schema.tables
                    WHERE table_name = 'kam_email_responses'
                """)).first()
                if not exists:
                    app.logger.info("[auto-migrate] creating kam_email_responses...")
                    conn.execute(text("""
                        CREATE TABLE kam_email_responses (
                            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            kam_id          UUID NOT NULL REFERENCES users_crm(id) ON DELETE CASCADE,
                            account_id      UUID REFERENCES cs_accounts(id) ON DELETE SET NULL,
                            gmail_thread_id TEXT NOT NULL,
                            subject         TEXT,
                            client_email    TEXT,
                            received_at     TIMESTAMPTZ NOT NULL,
                            replied_at      TIMESTAMPTZ NOT NULL,
                            response_hours  DOUBLE PRECISION NOT NULL,
                            synced_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            CONSTRAINT uq_kam_email_response UNIQUE (kam_id, gmail_thread_id)
                        )
                    """))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_kam_email_responses_kam_id "
                        "ON kam_email_responses (kam_id)"
                    ))
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS ix_kam_email_responses_account_id "
                        "ON kam_email_responses (account_id) WHERE account_id IS NOT NULL"
                    ))
                    app.logger.info("[auto-migrate] kam_email_responses created.")
                else:
                    # Agregar account_id si la tabla existe pero la columna no
                    col_exists = conn.execute(text("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'kam_email_responses' AND column_name = 'account_id'
                    """)).first()
                    if not col_exists:
                        conn.execute(text(
                            "ALTER TABLE kam_email_responses "
                            "ADD COLUMN account_id UUID REFERENCES cs_accounts(id) ON DELETE SET NULL"
                        ))
                        conn.execute(text(
                            "CREATE INDEX IF NOT EXISTS ix_kam_email_responses_account_id "
                            "ON kam_email_responses (account_id) WHERE account_id IS NOT NULL"
                        ))
                        app.logger.info("[auto-migrate] kam_email_responses.account_id added.")
        except Exception as e:
            app.logger.warning("[auto-migrate] kam_email_responses failed: %s", e)


def create_app():
    app = Flask(__name__)

    # ── Configuración ──────────────────────────
    secret_key = os.getenv("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY no está configurada. Sin ella, Flask firmaría las "
            "cookies de sesión con un valor público conocido (visible en el "
            "código fuente), permitiendo forjar sesiones de cualquier rol. "
            "Define SECRET_KEY en las variables de entorno antes de arrancar."
        )
    app.config["SECRET_KEY"] = secret_key

    # Supabase/Render usan postgres:// pero SQLAlchemy requiere postgresql://
    db_url = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/avantex_crm")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SECURITY-2026-06-24: cookies endurecidas
    # - HttpOnly: cookie no accesible via document.cookie (mitiga XSS exfil)
    # - Secure: cookie solo en HTTPS (Render siempre es HTTPS)
    # - SameSite=Lax: CSRF protection
    # - 24h: si nos roban una cookie y nadie la usa, el daño termina en 1 día
    #   (antes 7d). Refresh-each-request asegura que un vendedor activo nunca
    #   pierda la sesión en mitad del día laboral. PATCH-2026-06-25: subido de
    #   8h a 24h porque vendedores en sesiones cortas perdían el login.
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=24)
    app.config["SESSION_COOKIE_HTTPONLY"]    = True
    app.config["SESSION_COOKIE_SECURE"]      = os.getenv("FLASK_ENV") != "development"
    app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"
    app.config["SESSION_REFRESH_EACH_REQUEST"] = True  # extiende cookie en cada hit

    # Variables de Meta/WhatsApp accesibles en toda la app
    app.config["WHATSAPP_TOKEN"]    = os.getenv("WHATSAPP_TOKEN", "")
    app.config["WHATSAPP_PHONE_ID"] = os.getenv("WHATSAPP_PHONE_ID", "")
    meta_verify_token = os.getenv("META_VERIFY_TOKEN")
    if not meta_verify_token:
        raise RuntimeError(
            "META_VERIFY_TOKEN no está configurada. Sin ella, la verificación "
            "del webhook de Meta usaría un valor público conocido (visible en "
            "el código fuente). Define META_VERIFY_TOKEN en las variables de "
            "entorno antes de arrancar."
        )
    app.config["META_VERIFY_TOKEN"] = meta_verify_token
    # SECURITY-2026-07-14: firma HMAC del payload del webhook de Meta.
    # Sin esto, cualquiera que descubra la URL /webhook/meta puede inyectar
    # leads falsos con un simple POST. Ver blueprints/webhooks.py.
    app.config["META_APP_SECRET"] = os.getenv("META_APP_SECRET", "")

    # ── Extensiones ────────────────────────────
    db.init_app(app)
    socketio.init_app(app, cors_allowed_origins="*", async_mode="gevent")
    limiter.init_app(app)

    # ── Auto-migrations (idempotente, corre en cada boot) ──
    _run_pending_migrations(app)

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
    from blueprints.tickets       import tickets_bp
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
    from blueprints.meta_campaigns import meta_campaigns_bp
    from blueprints.sales_emails    import sales_emails_bp
    from blueprints.chat_ai         import chat_ai_bp
    from blueprints.due_diligence   import dd_bp

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
    app.register_blueprint(tickets_bp,       url_prefix="/soporte")
    # FEAT-2026-07-06 (Fugaci Due Diligence): sin url_prefix porque expone
    # tanto rutas admin bajo /cs/due-diligence como públicas /dd-encuesta/
    app.register_blueprint(dd_bp)
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
    app.register_blueprint(meta_campaigns_bp, url_prefix="/api/meta-campaigns")
    app.register_blueprint(sales_emails_bp,   url_prefix="/api/sales-emails")
    app.register_blueprint(chat_ai_bp,        url_prefix="/api/chat-ai")

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
        allowed = ("/login", "/auth/google", "/webhook/", "/static/", "/api/v1/",
                   "/encuesta/", "/dd-encuesta/", "/soporte/")  # público sin login
        if any(request.path.startswith(p) for p in allowed):
            return
        if not session.get("user_id"):
            # PATCH-2026-06-25: rutas /api/* devuelven JSON 401, no HTML redirect.
            # Antes el fetch del frontend recibía el HTML de /login, intentaba
            # parsearlo como JSON, fallaba, y mostraba mensajes confusos como
            # "lista de Industria/Tamaño no se cargó".
            if request.path.startswith("/api/"):
                from flask import jsonify
                return jsonify({"error": "Sesión expirada", "session_expired": True}), 401
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
        EtapaPipeline.PRESENTACION:   "#ea580c",
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
        EtapaPipeline.PRESENTACION:   ("Negociando",   "#d97706"),
        EtapaPipeline.COTIZACION:     ("Negociando",   "#d97706"),
        EtapaPipeline.DEMO:           ("Negociando",   "#d97706"),
        EtapaPipeline.NEGOCIACION:    ("Negociando",   "#d97706"),
        EtapaPipeline.CIERRE_GANADO:  ("Cerrado",      "#16a34a"),
        EtapaPipeline.CIERRE_PERDIDO: ("Cerrado",      "#dc2626"),
    }

    @app.route("/")
    def index():
        from sqlalchemy.orm import joinedload
        from models import Oportunidad, EtapaOportunidad, Usuario
        q = Lead.query.options(joinedload(Lead.usuario_asignado))

        # Vendedores solo ven sus leads, perfiles admin/gerencia se filtran
        # por UN más abajo según su alcance.
        user_rol = session.get("user_rol", "")
        if user_rol.upper() == "VENDEDOR":
            usuario_id = session.get("usuario_id")
            if usuario_id:
                q = q.filter(Lead.usuario_asignado_id == usuario_id)

        # Admin filter por vendedor explícito vía query string ?vendedor=<uuid>
        filtro_vendedor = (request.args.get("vendedor") or "").strip()
        if filtro_vendedor and user_rol.upper() != "VENDEDOR":
            if filtro_vendedor == "sin_asignar":
                q = q.filter(Lead.usuario_asignado_id.is_(None))
            else:
                q = q.filter(Lead.usuario_asignado_id == filtro_vendedor)

        # FEAT-2026-06-29: aplicar filtro UN al SSR del kanban inicial.
        # Sin esto, recargar la página mostraba TODOS los leads aunque el
        # selector UN del sidebar dijera 'Pestex'.
        from un_filter import filtrar_leads_por_un
        from blueprints.auth import effective_un_from_request
        effective_un = effective_un_from_request(request.args.get("un"))
        q = filtrar_leads_por_un(q, Lead, effective_un)

        # Lista de vendedores para el dropdown (solo admins lo usan)
        vendedores_list = []
        if user_rol.upper() != "VENDEDOR":
            vendedores_list = Usuario.query.order_by(Usuario.nombre.asc()).all()

        all_leads = q.order_by(Lead.fecha_actualizacion.desc()).all()

        # Pre-fetch oppo más reciente por lead para mostrar badge 💰 + info
        oppos_by_lead = {}
        for op in (Oportunidad.query
                   .filter(Oportunidad.lead_id.isnot(None))
                   .order_by(Oportunidad.fecha_creacion.desc()).all()):
            if op.lead_id not in oppos_by_lead:
                oppos_by_lead[op.lead_id] = op  # primer match = más reciente
        for lead in all_leads:
            lead.oppo = oppos_by_lead.get(lead.id)  # atributo transient
            lead.has_oppo = lead.oppo is not None
            # Necesita calificación ICP si falta industria o tamaño
            lead.needs_icp = not (lead.tipo_industria and lead.tamano_empresa)

        # Pre-fetch oportunidades HUÉRFANAS (sin lead_id) para mostrarlas en el pipe
        # mapeadas a la columna de Lead equivalente.
        OPPO_TO_PIPE = {
            EtapaOportunidad.CALIFICACION:   EtapaPipeline.COTIZACION,
            EtapaOportunidad.ANALISIS:       EtapaPipeline.COTIZACION,
            EtapaOportunidad.PROPUESTA:      EtapaPipeline.DEMO,
            EtapaOportunidad.NEGOCIACION:    EtapaPipeline.NEGOCIACION,
            EtapaOportunidad.CIERRE_GANADO:  EtapaPipeline.CIERRE_GANADO,
            EtapaOportunidad.CIERRE_PERDIDO: EtapaPipeline.CIERRE_PERDIDO,
        }
        oppo_q = Oportunidad.query.filter(Oportunidad.lead_id.is_(None))
        if user_rol.upper() == "VENDEDOR" and session.get("usuario_id"):
            oppo_q = oppo_q.filter(Oportunidad.propietario_id == session.get("usuario_id"))
        else:
            from un_filter import normalizar_un
            canon = normalizar_un(effective_un)
            if canon:
                aliases = {
                    "Aromatex": ("aromatex", "aromatex home", "aromatex_home", "aromatexhome"),
                    "Pestex": ("pestex",),
                    "Weldex": ("weldex",),
                    "Nexo": ("nexo",),
                }.get(canon, ())
                oppo_q = oppo_q.filter(db.or_(
                    Oportunidad.marca_interes.is_(None),
                    Oportunidad.marca_interes == "",
                    db.func.lower(Oportunidad.marca_interes).in_(aliases),
                ))
        orphan_oppos = oppo_q.order_by(Oportunidad.fecha_actualizacion.desc()).all()
        oppos_by_pipe_etapa = {}
        for op in orphan_oppos:
            if op.etapa:
                mapped = OPPO_TO_PIPE.get(op.etapa)
                if mapped:
                    oppos_by_pipe_etapa.setdefault(mapped, []).append(op)

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
                "oppos":        oppos_by_pipe_etapa.get(etapa, []),  # oppos huérfanos mapeados
            }
        from meta_conversions import get_pixel_ids
        # FEAT-2026-06-29: pasar especialidad del vendedor para el default
        # del filtro UN
        mi_especialidad = []
        if session.get("usuario_id"):
            from models import Usuario as _U
            try:
                _u = db.session.get(_U, session["usuario_id"])
                if _u and _u.especialidad_marca:
                    mi_especialidad = list(_u.especialidad_marca)
            except Exception:
                pass
        return render_template(
            "pipeline/index.html",
            pipeline=pipeline,
            etapas=list(EtapaPipeline),
            user_nombre=session.get("user_nombre", ""),
            user_rol=session.get("user_rol", ""),
            usuario_id=session.get("usuario_id", ""),
            mi_especialidad=mi_especialidad,
            meta_pixels=get_pixel_ids(),
            vendedores_list=vendedores_list,
            filtro_vendedor=filtro_vendedor,
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
        # FEAT-2026-07-21: ticket_token en cs_accounts (portal público de tickets)
        try:
            db.session.execute(db.text(
                "ALTER TABLE cs_accounts ADD COLUMN IF NOT EXISTS ticket_token VARCHAR(32)"
            ))
            db.session.execute(db.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_cs_accounts_ticket_token "
                "ON cs_accounts (ticket_token) WHERE ticket_token IS NOT NULL"
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()
        # FEAT-2026-07-21: folio consecutivo en cs_incidencias (portal de tickets)
        try:
            db.session.execute(db.text(
                "ALTER TABLE cs_incidencias ADD COLUMN IF NOT EXISTS folio VARCHAR(20)"
            ))
            db.session.execute(db.text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_cs_incidencias_folio "
                "ON cs_incidencias (folio) WHERE folio IS NOT NULL"
            ))
            # Backfill TK-XXXX en orden de creación para las incidencias que
            # ya existían antes de este feature.
            rows = db.session.execute(db.text(
                "SELECT id FROM cs_incidencias WHERE folio IS NULL ORDER BY created_at ASC"
            )).fetchall()
            for i, row in enumerate(rows, start=1):
                db.session.execute(
                    db.text("UPDATE cs_incidencias SET folio = :f WHERE id = :id"),
                    {"f": f"TK-{i:04d}", "id": row[0]},
                )
            if rows:
                app.logger.info("[auto-migrate] backfilled %d incidencias with TK-XXXX", len(rows))
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
                try:
                    from notificaciones import enviar_notificaciones_diarias
                    enviar_notificaciones_diarias()
                except Exception as e:
                    app.logger.warning(f"notificaciones diarias: {e}")

        def _run_backup():
            with app.app_context():
                try:
                    from backups import ejecutar_backup
                    ejecutar_backup()
                except Exception as e:
                    app.logger.warning(f"backup diario: {e}")

        def _run_savio_invoices_payments():
            with app.app_context():
                import savio_sync
                try:
                    savio_sync.sync_invoices()
                    savio_sync.sync_payments()
                    # Bridge Savio → CSInvoice cada hora para que el dashboard CS
                    # vea pagos/facturas nuevas sin esperar al job de 6h.
                    savio_sync.sync_savio_to_cs_invoices()
                except Exception as e:
                    app.logger.warning(f"savio hourly: {e}")

        def _run_savio_customers_subs():
            with app.app_context():
                import savio_sync
                try:
                    savio_sync.sync_subscriptions()
                    savio_sync.sync_customers()
                    savio_sync.bridge_savio_to_cs_mrr()
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
        # Cadencia automática PAUSADA mientras se afina la automatización de campañas.
        # Reactivar descomentando la línea siguiente cuando los mappings campaign→marca/zona estén listos.
        # scheduler.add_job(_run_cadencia, "interval", minutes=15, id="cadencia_followup")
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

        # Gmail monitoring de vendedores (cada 5 min) + purge histórico diario.
        # Solo se activa si GMAIL_SERVICE_ACCOUNT_JSON está set en env.
        if os.getenv("GMAIL_SERVICE_ACCOUNT_JSON"):
            def _run_gmail_poll():
                with app.app_context():
                    try:
                        import gmail_monitor
                        result = gmail_monitor.poll_all()
                        if result.get("total_saved", 0) > 0:
                            app.logger.info(f"Gmail polling: {result.get('total_saved')} correos nuevos")
                    except Exception as e:
                        app.logger.warning(f"gmail polling: {e}")

            def _run_gmail_purge():
                with app.app_context():
                    try:
                        import gmail_monitor
                        gmail_monitor.purge_old()
                    except Exception as e:
                        app.logger.warning(f"gmail purge: {e}")

            def _run_kam_response_poll():
                with app.app_context():
                    try:
                        import gmail_monitor
                        result = gmail_monitor.poll_kam_responses()
                        saved = result.get("total_saved", 0)
                        updated = result.get("total_updated", 0)
                        if saved + updated > 0:
                            app.logger.info(f"KAM email responses: {saved} nuevos, {updated} actualizados")
                    except Exception as e:
                        app.logger.warning(f"kam response polling: {e}")

            scheduler.add_job(_run_gmail_poll,       "interval", minutes=5,  id="gmail_poll")
            scheduler.add_job(_run_kam_response_poll, "interval", minutes=60, id="kam_response_poll")
            # Purge diario a las 4am CST (10am UTC) — fuera de horario laboral
            scheduler.add_job(_run_gmail_purge, "cron", hour=10, minute=0, id="gmail_purge")
            app.logger.info("Gmail monitoring activo (poll 5 min + purge diario + KAM responses cada hora)")

        # FEAT-2026-07-03: Zoho Analytics → cs_appointments ETL (diario 4:30am CST)
        # Solo si están configuradas las 8 env vars necesarias.
        _zoho_vars = ("ZOHO_CLIENT_ID","ZOHO_CLIENT_SECRET","ZOHO_REFRESH_TOKEN",
                      "ZOHO_USER_EMAIL","ZOHO_WORKSPACE","ZOHO_TABLE",
                      "SUPABASE_URL","SUPABASE_SERVICE_KEY")
        if all(os.getenv(k) for k in _zoho_vars):
            def _run_zoho_appointments_etl():
                with app.app_context():
                    try:
                        import zoho_appointments_etl as etl
                        result = etl.run()
                        app.logger.info(f"Zoho ETL: {result}")
                    except Exception as e:
                        app.logger.warning(f"zoho appointments etl: {e}")

            scheduler.add_job(_run_zoho_appointments_etl, "cron",
                              hour=10, minute=30, id="zoho_appts_etl")  # 4:30am CST
            app.logger.info("Zoho Analytics ETL activo (diario 4:30am CST)")
        else:
            _faltan = [k for k in _zoho_vars if not os.getenv(k)]
            app.logger.info(f"Zoho ETL desactivado — faltan env vars: {_faltan}")

        # Meta Lead Ads polling (cada 5 min) — alternativa al webhook mientras la App no está publicada
        if os.getenv("META_PAGE_TOKEN"):
            def _run_meta_polling():
                with app.app_context():
                    try:
                        from meta_lead_polling import poll_and_create_leads
                        result = poll_and_create_leads()
                        if result.get("leads_created", 0) > 0:
                            app.logger.info(f"Meta polling: {result}")
                    except Exception as e:
                        app.logger.warning(f"meta polling: {e}")

            scheduler.add_job(_run_meta_polling, "interval", minutes=5, id="meta_lead_polling")
            app.logger.info("Meta Lead Ads polling activo (cada 5 min)")

        # LinkedIn Lead Gen Forms polling (cada 5 min)
        if os.getenv("LINKEDIN_ACCESS_TOKEN"):
            def _run_linkedin_polling():
                with app.app_context():
                    try:
                        from linkedin_lead_polling import poll_and_create_leads
                        result = poll_and_create_leads()
                        if result.get("leads_created", 0) > 0:
                            app.logger.info(f"LinkedIn polling: {result}")
                    except Exception as e:
                        app.logger.warning(f"linkedin polling: {e}")

            scheduler.add_job(_run_linkedin_polling, "interval", minutes=5, id="linkedin_lead_polling")
            app.logger.info("LinkedIn Lead Gen polling activo (cada 5 min)")

        scheduler.start()
        app.logger.info("Scheduler iniciado: cadencia PAUSADA + notificaciones (9am) + backup (3am)")
    except Exception as e:
        app.logger.warning(f"No se pudo iniciar scheduler: {e}")


# ──────────────────────────────────────────────
# Punto de entrada
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app = create_app()
    # Producción: gunicorn -k gevent -w 1 "avantex_crm:create_app()"
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
