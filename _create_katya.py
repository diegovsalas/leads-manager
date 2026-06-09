#!/usr/bin/env python3
"""
Crea a Katya Gómez como vendedora completa.

Hace dos inserts idempotentes:
1) `usuarios` (perfil comercial Avantex) — para que reciba leads asignados
2) `users_crm` (login) — con rol Vendedor para que solo vea sus leads en
   el kanban

Idempotente: si ya existe, no rompe. Imprime los UUIDs al final para
setear el env var META_LEADS_ASSIGNEE_USUARIO_ID en Render.

Uso:
  cd /Users/diego/Downloads/Carpetas/CRM
  python3 _create_katya.py
"""
import os
import sys
import uuid

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text
from werkzeug.security import generate_password_hash


KATYA_NOMBRE   = "Katya Gómez"
KATYA_CORREO   = "katyagomez@grupoavantex.com"
KATYA_TELEFONO = "8123688192"
PASSWORD_TEMP  = "Avantex2026"  # Katya debe cambiarla después de loguear


def main():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("ERROR: DATABASE_URL no seteada en .env")
        sys.exit(1)
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(db_url, pool_pre_ping=True)
    with engine.begin() as conn:
        # ── 1. Crear Usuario (perfil comercial) si no existe ──
        existing = conn.execute(
            text("SELECT id FROM usuarios WHERE telefono_whatsapp = :tel OR nombre = :nom"),
            {"tel": KATYA_TELEFONO, "nom": KATYA_NOMBRE},
        ).first()

        if existing:
            usuario_id = existing[0]
            print(f"✓ Usuario ya existía: {usuario_id}")
        else:
            usuario_id = uuid.uuid4()
            conn.execute(text("""
                INSERT INTO usuarios (
                    id, nombre, telefono_whatsapp, rol_comercial,
                    en_turno, especialidad_marca, zona_cobertura
                ) VALUES (
                    :id, :nombre, :tel, 'Asesor Comercial',
                    TRUE, '{Aromatex,Pestex,Weldex}', '{}'
                )
            """), {"id": str(usuario_id), "nombre": KATYA_NOMBRE, "tel": KATYA_TELEFONO})
            print(f"✓ Usuario creado: {usuario_id}")

        # ── 2. Crear UserCRM (login) si no existe ──
        existing_login = conn.execute(
            text("SELECT id, rol FROM users_crm WHERE correo = :c"),
            {"c": KATYA_CORREO},
        ).first()

        if existing_login:
            # Actualizar el link a su usuario_id y forzar rol Vendedor por las dudas
            conn.execute(text("""
                UPDATE users_crm
                   SET usuario_id = :uid, rol = 'Vendedor', activo = TRUE
                 WHERE correo = :c
            """), {"uid": str(usuario_id), "c": KATYA_CORREO})
            print(f"✓ UserCRM actualizado (link → {usuario_id}, rol → Vendedor)")
        else:
            password_hash = generate_password_hash(PASSWORD_TEMP)
            conn.execute(text("""
                INSERT INTO users_crm (
                    nombre, correo, password_hash, rol, activo, usuario_id
                ) VALUES (
                    :nombre, :correo, :pwd, 'Vendedor', TRUE, :uid
                )
            """), {
                "nombre": KATYA_NOMBRE,
                "correo": KATYA_CORREO,
                "pwd": password_hash,
                "uid": str(usuario_id),
            })
            print(f"✓ UserCRM creado: {KATYA_CORREO} (password: {PASSWORD_TEMP})")

    print()
    print("══════════════════════════════════════════════════════════════")
    print("LISTO. Próximos pasos:")
    print()
    print(f"1. En Render → Environment → agregar:")
    print(f"   META_LEADS_ASSIGNEE_USUARIO_ID = {usuario_id}")
    print()
    print(f"2. Katya entra a: https://leads-manager-avantex.onrender.com/login")
    print(f"   Usuario: {KATYA_CORREO}")
    print(f"   Password: {PASSWORD_TEMP}")
    print(f"   (debería cambiarla después)")
    print("══════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
