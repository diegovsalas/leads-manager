"""
Setup script for proyecto_items table.
Run once: python setup_proyecto.py

Creates the table in Supabase, seeds initial data, and appends GEMINI_API_KEY to .env.
"""
import uuid
import psycopg2
import os

DB_URL = "postgresql://postgres.cyntwgxryfbrboehcdex:Brs99791avantex@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
GEMINI_KEY = "AIzaSyCFaK3kJPOXYYlSsLMv8usBCQ_DwN4rw6Y"


def create_table(cur):
    cur.execute("""
    DO $$ BEGIN
        CREATE TYPE tipo_proyecto_enum AS ENUM ('avance', 'idea', 'nota');
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
    """)

    cur.execute("DROP TABLE IF EXISTS proyecto_items CASCADE;")

    cur.execute("""
    CREATE TABLE proyecto_items (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        tipo tipo_proyecto_enum NOT NULL,
        titulo VARCHAR(300) NOT NULL,
        descripcion TEXT,
        autor VARCHAR(150) NOT NULL,
        prioridad VARCHAR(50),
        votos INTEGER NOT NULL DEFAULT 0,
        prompt_dev TEXT,
        fecha_creacion TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    print("Table proyecto_items created.")


def seed_data(cur):
    items = [
        # Avances
        (uuid.uuid4(), 'avance', 'Deploy en Render + Supabase',
         'App live con BD conectada. Seed de vendedores ejecutado.', 'Diego Velazquez', None),
        (uuid.uuid4(), 'avance', 'UI Notion style completa',
         'Dashboard, Pipeline, Chat, Leads, Proyecto. Filtros por UN y mes.', 'Diego Velazquez', None),
        (uuid.uuid4(), 'avance', 'Modelos BD + Round-Robin + Schema SQL',
         '5 tablas + funcion asignar_lead_comercial() + vista embudo.', 'Diego Velazquez', None),
        (uuid.uuid4(), 'avance', 'Estructura Flask + Blueprints',
         'webhooks, leads, chat, dashboard. Factory pattern.', 'Diego Velazquez', None),
        (uuid.uuid4(), 'avance', 'Sistema de login implementado',
         '3 usuarios Super Admin, auth con sessions, pagina login estilo Notion.', 'Diego Velazquez', None),
        # Ideas - Alta
        (uuid.uuid4(), 'idea', 'Bot interno WhatsApp',
         'Vendedores gestionan leads desde WA: mis leads, nota 5, cerrar 3.', 'Diego Velazquez', 'Alta'),
        (uuid.uuid4(), 'idea', 'Notificaciones al vendedor',
         'WA al vendedor asignado cuando llega un lead nuevo.', 'Diego Velazquez', 'Alta'),
        # Ideas - Media
        (uuid.uuid4(), 'idea', 'Dashboard por vendedor',
         'Cada asesor ve solo sus leads y metricas.', 'Diego Velazquez', 'Media'),
        (uuid.uuid4(), 'idea', 'Scoring automatico de leads',
         'Puntuacion basada en valor, interacciones, tiempo de respuesta.', 'Diego Velazquez', 'Media'),
        # Ideas - Baja
        (uuid.uuid4(), 'idea', 'Exportar PDF/Excel',
         'Reporte mensual con embudo, revenue, ROI.', 'Diego Velazquez', 'Baja'),
    ]

    for item in items:
        row = (str(item[0]),) + item[1:]
        cur.execute(
            "INSERT INTO proyecto_items (id, tipo, titulo, descripcion, autor, prioridad) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            row,
        )
    print(f"Seeded {len(items)} items.")


def update_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    # Check if GEMINI_API_KEY already exists
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            content = f.read()
        if "GEMINI_API_KEY" in content:
            print("GEMINI_API_KEY already in .env, skipping.")
            return
    # Append
    with open(env_path, "a") as f:
        f.write(f"\nGEMINI_API_KEY={GEMINI_KEY}\n")
    print("GEMINI_API_KEY appended to .env")


if __name__ == "__main__":
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()

    create_table(cur)
    seed_data(cur)

    cur.close()
    conn.close()

    update_env()
    print("Done!")
