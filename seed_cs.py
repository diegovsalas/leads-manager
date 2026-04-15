# seed_cs.py
"""
Migra datos de Vittaly (cs_bootstrap.json) a las tablas CS en Supabase.
Run once: python3 seed_cs.py
"""
import json
import psycopg2
import uuid

DB_URL = "postgresql://postgres.cyntwgxryfbrboehcdex:Brs99791avantex@aws-1-us-east-1.pooler.supabase.com:5432/postgres"

# Mapeo KAM nombre → correo en users_crm
KAM_EMAILS = {
    "Francisco Rodriguez": "franciscorodriguez@grupoavantex.com",
    "Katia Gutierrez": "katiarodriguez@grupoavantex.com",
    "Nallely Quiroz": "nallelyquiroz@grupoavantex.com",
    "Heidi Tovar": "heiditovar@grupoavantex.com",
}

conn = psycopg2.connect(DB_URL)
conn.autocommit = True
cur = conn.cursor()

# 1. Crear tablas CS
print("Creando tablas CS...")
cur.execute("""
CREATE TABLE IF NOT EXISTS cs_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre VARCHAR(200) NOT NULL UNIQUE,
    kam_id UUID NOT NULL REFERENCES users_crm(id),
    es_cuenta_nueva BOOLEAN DEFAULT FALSE,
    mrr NUMERIC(14,2) DEFAULT 0,
    arr_proyectado NUMERIC(14,2) DEFAULT 0,
    sucursales INTEGER DEFAULT 0,
    unidades_contratadas VARCHAR(100) DEFAULT '',
    facturacion_q1 NUMERIC(14,2) DEFAULT 0,
    pagado_q1 NUMERIC(14,2) DEFAULT 0,
    pendiente_q1 NUMERIC(14,2) DEFAULT 0,
    num_facturas_q1 INTEGER DEFAULT 0,
    nps FLOAT,
    pulso VARCHAR(20),
    eficiencia_operativa FLOAT
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES cs_accounts(id),
    folio VARCHAR(50) DEFAULT '',
    serie VARCHAR(20) DEFAULT '',
    concepto VARCHAR(300) DEFAULT '',
    uen VARCHAR(50) DEFAULT '',
    subtotal NUMERIC(14,2) DEFAULT 0,
    impuestos NUMERIC(14,2) DEFAULT 0,
    total NUMERIC(14,2) DEFAULT 0,
    pendiente NUMERIC(14,2) DEFAULT 0,
    pagado NUMERIC(14,2) DEFAULT 0,
    fecha_cobro DATE,
    fecha_vencimiento DATE,
    fecha_pago DATE,
    estatus VARCHAR(30) DEFAULT ''
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES cs_accounts(id),
    propiedad VARCHAR(300) DEFAULT '',
    direccion VARCHAR(500) DEFAULT '',
    zona VARCHAR(100) DEFAULT '',
    tecnico VARCHAR(120) DEFAULT '',
    fecha_inicio TIMESTAMPTZ,
    fecha_terminacion TIMESTAMPTZ,
    estatus VARCHAR(50) DEFAULT '',
    titulo_servicio VARCHAR(200) DEFAULT '',
    cantidad INTEGER DEFAULT 1
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_notes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES cs_accounts(id),
    autor VARCHAR(120) DEFAULT '',
    contenido TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES cs_accounts(id),
    tipo VARCHAR(50) DEFAULT 'check-in',
    descripcion TEXT NOT NULL,
    responsable VARCHAR(120) DEFAULT '',
    fecha_limite DATE,
    completada BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_onboarding_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre VARCHAR(200) NOT NULL,
    sucursales INTEGER DEFAULT 0,
    tarifa NUMERIC(14,2) DEFAULT 0,
    frecuencia VARCHAR(30) DEFAULT 'mensual',
    mrr_proyectado NUMERIC(14,2) DEFAULT 0,
    kam_id UUID REFERENCES users_crm(id)
);
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS cs_opportunities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID REFERENCES cs_accounts(id),
    prospecto_nombre VARCHAR(200) DEFAULT '',
    contacto VARCHAR(200) DEFAULT '',
    tipo VARCHAR(50) NOT NULL,
    unidad_negocio VARCHAR(30) DEFAULT '',
    descripcion TEXT DEFAULT '',
    valor_estimado NUMERIC(14,2) DEFAULT 0,
    etapa VARCHAR(30) DEFAULT 'prospeccion',
    kam_id UUID REFERENCES users_crm(id),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
""")

print("Tablas CS creadas.")

# 2. Buscar KAM ids
kam_ids = {}
for nombre, correo in KAM_EMAILS.items():
    cur.execute("SELECT id FROM users_crm WHERE correo = %s", (correo,))
    row = cur.fetchone()
    if row:
        kam_ids[nombre] = str(row[0])
        print(f"  KAM: {nombre} → {row[0]}")
    else:
        print(f"  ⚠ KAM no encontrado: {nombre} ({correo})")

# 3. Cargar JSON
with open("/Users/diego/Desktop/Vittaly/cs_bootstrap.json", encoding="utf-8") as f:
    data = json.load(f)

# 4. Insertar cuentas
print("\nInsertando cuentas...")
account_ids = {}
for c in data["cuentas"]:
    nombre = c["cliente"].replace(" (NUEVO)", "")
    kam_nombre = c["kam"]
    kam_id = kam_ids.get(kam_nombre)
    if not kam_id:
        print(f"  ⚠ Saltando {nombre} — KAM '{kam_nombre}' no encontrado")
        continue

    # Check if exists
    cur.execute("SELECT id FROM cs_accounts WHERE nombre = %s", (nombre,))
    if cur.fetchone():
        print(f"  Ya existe: {nombre}")
        cur.execute("SELECT id FROM cs_accounts WHERE nombre = %s", (nombre,))
        account_ids[nombre] = str(cur.fetchone()[0])
        continue

    acc_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO cs_accounts (id, nombre, kam_id, es_cuenta_nueva, mrr, arr_proyectado,
            sucursales, unidades_contratadas, facturacion_q1, pagado_q1, pendiente_q1, num_facturas_q1)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        acc_id, nombre, kam_id, c.get("es_cuenta_nueva", False),
        c["mrr"], c["arr_proyectado"], c["sucursales"],
        ",".join(c.get("unidades_contratadas", [])),
        c["facturacion_q1_2026"], c["pagado_q1"], c["pendiente_q1"],
        c["num_facturas_q1"],
    ))
    account_ids[nombre] = acc_id
    print(f"  Creada: {nombre} (MRR ${c['mrr']:,.0f})")

# 5. Pipeline de onboarding
print("\nInsertando pipeline de onboarding...")
for cn in data.get("cuentas_nuevas_futuras", []):
    cur.execute("SELECT id FROM cs_onboarding_accounts WHERE nombre = %s", (cn["cliente"],))
    if cur.fetchone():
        continue
    cur.execute("""
        INSERT INTO cs_onboarding_accounts (nombre, sucursales, tarifa, frecuencia, mrr_proyectado)
        VALUES (%s, %s, %s, %s, %s)
    """, (cn["cliente"], cn["sucursales"], cn["tarifa"], cn["frecuencia"], cn["mrr_proyectado"]))
    print(f"  Pipeline: {cn['cliente']}")

# 6. Migrar facturas y citas desde SQLite
try:
    import sqlite3
    import pandas as pd

    sqlite_path = "/Users/diego/Desktop/Vittaly/instance/database.db"
    sqlite_conn = sqlite3.connect(sqlite_path)

    # Facturas
    print("\nMigrando facturas desde SQLite...")
    df_inv = pd.read_sql("SELECT * FROM invoices", sqlite_conn)

    # Mapeo old account_id → nombre
    df_accs = pd.read_sql("SELECT id, nombre FROM accounts", sqlite_conn)
    old_acc_map = dict(zip(df_accs["id"], df_accs["nombre"]))

    inv_count = 0
    for _, row in df_inv.iterrows():
        acc_nombre = old_acc_map.get(row["account_id"])
        if not acc_nombre or acc_nombre not in account_ids:
            continue
        new_acc_id = account_ids[acc_nombre]
        cur.execute("""
            INSERT INTO cs_invoices (account_id, folio, serie, concepto, uen,
                subtotal, impuestos, total, pendiente, pagado,
                fecha_cobro, fecha_vencimiento, fecha_pago, estatus)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            new_acc_id, str(row.get("folio", "")), str(row.get("serie", "")),
            str(row.get("concepto", "")), str(row.get("uen", "")),
            float(row.get("subtotal", 0) or 0), float(row.get("impuestos", 0) or 0),
            float(row.get("total", 0) or 0), float(row.get("pendiente", 0) or 0),
            float(row.get("pagado", 0) or 0),
            row.get("fecha_cobro") if pd.notna(row.get("fecha_cobro")) else None,
            row.get("fecha_vencimiento") if pd.notna(row.get("fecha_vencimiento")) else None,
            row.get("fecha_pago") if pd.notna(row.get("fecha_pago")) else None,
            str(row.get("estatus", "")),
        ))
        inv_count += 1
    print(f"  {inv_count} facturas migradas")

    # Citas
    print("Migrando citas desde SQLite...")
    df_apt = pd.read_sql("SELECT * FROM appointments", sqlite_conn)
    apt_count = 0
    for _, row in df_apt.iterrows():
        acc_nombre = old_acc_map.get(row["account_id"])
        if not acc_nombre or acc_nombre not in account_ids:
            continue
        new_acc_id = account_ids[acc_nombre]
        cur.execute("""
            INSERT INTO cs_appointments (account_id, propiedad, direccion, zona, tecnico,
                fecha_inicio, fecha_terminacion, estatus, titulo_servicio, cantidad)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            new_acc_id, str(row.get("propiedad", "")), str(row.get("direccion", "")),
            str(row.get("zona", "")), str(row.get("tecnico", "")),
            row.get("fecha_inicio") if pd.notna(row.get("fecha_inicio")) else None,
            row.get("fecha_terminacion") if pd.notna(row.get("fecha_terminacion")) else None,
            str(row.get("estatus", "")), str(row.get("titulo_servicio", "")),
            int(row.get("cantidad", 1) or 1),
        ))
        apt_count += 1
    print(f"  {apt_count} citas migradas")

    # Notas
    df_notes = pd.read_sql("SELECT * FROM notes", sqlite_conn)
    note_count = 0
    for _, row in df_notes.iterrows():
        acc_nombre = old_acc_map.get(row["account_id"])
        if not acc_nombre or acc_nombre not in account_ids:
            continue
        cur.execute("""
            INSERT INTO cs_notes (account_id, autor, contenido)
            VALUES (%s, %s, %s)
        """, (account_ids[acc_nombre], str(row.get("autor", "")), str(row.get("contenido", ""))))
        note_count += 1
    print(f"  {note_count} notas migradas")

    # Tareas
    df_tasks = pd.read_sql("SELECT * FROM tasks", sqlite_conn)
    task_count = 0
    for _, row in df_tasks.iterrows():
        acc_nombre = old_acc_map.get(row["account_id"])
        if not acc_nombre or acc_nombre not in account_ids:
            continue
        cur.execute("""
            INSERT INTO cs_tasks (account_id, tipo, descripcion, responsable, fecha_limite, completada)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            account_ids[acc_nombre], str(row.get("tipo", "check-in")),
            str(row.get("descripcion", "")), str(row.get("responsable", "")),
            row.get("fecha_limite") if pd.notna(row.get("fecha_limite")) else None,
            bool(row.get("completada", False)),
        ))
        task_count += 1
    print(f"  {task_count} tareas migradas")

    sqlite_conn.close()
except Exception as e:
    print(f"\n⚠ No se pudieron migrar datos de SQLite: {e}")
    print("  Las tablas están creadas — los datos se pueden cargar después.")

# Resumen
cur.execute("SELECT COUNT(*) FROM cs_accounts")
print(f"\nResumen:")
print(f"  Cuentas: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM cs_invoices")
print(f"  Facturas: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM cs_appointments")
print(f"  Citas: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM cs_notes")
print(f"  Notas: {cur.fetchone()[0]}")
cur.execute("SELECT COUNT(*) FROM cs_tasks")
print(f"  Tareas: {cur.fetchone()[0]}")

conn.close()
print("\nSeed CS completado.")
