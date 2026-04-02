# seed_users.py
"""
Creates users_crm table and inserts initial users.
Run once: python3 seed_users.py
"""
import psycopg2
from werkzeug.security import generate_password_hash

DB_URL = "postgresql://postgres.cyntwgxryfbrboehcdex:Brs99791avantex@aws-1-us-east-1.pooler.supabase.com:5432/postgres"

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

# Create enum (ignore if exists)
cur.execute("""
DO $$ BEGIN
    CREATE TYPE rol_crm_enum AS ENUM ('Super Admin', 'Admin', 'Viewer');
EXCEPTION
    WHEN duplicate_object THEN null;
END $$;
""")

# Create table
cur.execute("""
CREATE TABLE IF NOT EXISTS users_crm (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    nombre            VARCHAR(150)      NOT NULL,
    correo            VARCHAR(200)      NOT NULL UNIQUE,
    password_hash     VARCHAR(256)      NOT NULL,
    rol               rol_crm_enum      NOT NULL DEFAULT 'Viewer',
    activo            BOOLEAN           NOT NULL DEFAULT TRUE,
    foto_url          VARCHAR(500),
    fecha_creacion    TIMESTAMPTZ       NOT NULL DEFAULT NOW()
);
""")
cur.execute("CREATE INDEX IF NOT EXISTS idx_users_crm_correo ON users_crm (correo);")
print("Tabla users_crm lista")

# Users to create
USERS = [
    {
        "nombre": "Diego Velazquez",
        "correo": "diegovelazquez@grupoavantex.com",
        "password": "Avantex123",
        "rol": "Super Admin",
    },
    {
        "nombre": "Alejandro Gil",
        "correo": "alejandrogil@grupoavantex.com",
        "password": "Avantex123",
        "rol": "Super Admin",
    },
    {
        "nombre": "Andrea Rodriguez",
        "correo": "andrearodriguez@grupoavantex.com",
        "password": "Avantex123",
        "rol": "Super Admin",
    },
]

for u in USERS:
    # Check if exists
    cur.execute("SELECT id FROM users_crm WHERE correo = %s", (u["correo"],))
    if cur.fetchone():
        print(f"  Ya existe: {u['correo']}")
        continue

    pw_hash = generate_password_hash(u["password"])
    cur.execute(
        "INSERT INTO users_crm (nombre, correo, password_hash, rol) VALUES (%s, %s, %s, %s)",
        (u["nombre"], u["correo"], pw_hash, u["rol"]),
    )
    print(f"  Creado: {u['nombre']} ({u['correo']}) — {u['rol']}")

# Verify
cur.execute("SELECT nombre, correo, rol FROM users_crm ORDER BY nombre")
print("\nUsuarios en BD:")
for row in cur.fetchall():
    print(f"  {row[0]} | {row[1]} | {row[2]}")

conn.close()
