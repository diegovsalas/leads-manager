# seed_kams.py
"""
Agrega rol KAM al enum y crea los 4 usuarios KAM.
Run once: python3 seed_kams.py
"""
import psycopg2
from werkzeug.security import generate_password_hash

DB_URL = "postgresql://postgres.cyntwgxryfbrboehcdex:Brs99791avantex@aws-1-us-east-1.pooler.supabase.com:5432/postgres"

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

# 1. Agregar 'KAM' al enum rol_crm_enum
cur.execute("""
    DO $$ BEGIN
        ALTER TYPE rol_crm_enum ADD VALUE IF NOT EXISTS 'KAM';
    EXCEPTION
        WHEN duplicate_object THEN null;
    END $$;
""")
print("Enum rol_crm_enum actualizado con 'KAM'")

# 2. Crear usuarios KAM
KAMS = [
    {
        "nombre": "Francisco Rodriguez",
        "correo": "franciscorodriguez@grupoavantex.com",
        "password": "Avantex123",
        "rol": "KAM",
    },
    {
        "nombre": "Katia Gutierrez",
        "correo": "katiarodriguez@grupoavantex.com",
        "password": "Avantex123",
        "rol": "KAM",
    },
    {
        "nombre": "Nallely Quiroz",
        "correo": "nallelyquiroz@grupoavantex.com",
        "password": "Avantex123",
        "rol": "KAM",
    },
    {
        "nombre": "Heidi Tovar",
        "correo": "heiditovar@grupoavantex.com",
        "password": "Avantex123",
        "rol": "KAM",
    },
]

for u in KAMS:
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

# 3. Verificar
cur.execute("SELECT nombre, correo, rol FROM users_crm ORDER BY rol, nombre")
print("\nTodos los usuarios:")
for row in cur.fetchall():
    print(f"  {row[2]:12s} | {row[0]:25s} | {row[1]}")

conn.close()
print("\nListo.")
