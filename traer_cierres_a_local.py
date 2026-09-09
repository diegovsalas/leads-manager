#!/usr/bin/env python3
"""
Copia de PRODUCCIÓN a la base LOCAL solo lo necesario para comparar montos
de cierre mensual: vendedores, leads cerrados y sus ventas.

  producción → SOLO LECTURA (sesión READ ONLY: el servidor rechaza escrituras)
  local      → se reemplazan leads y sales; los usuarios de login se conservan

No copia cuentas, contactos, incidencias, facturas, correos ni nada de CS.
Los FKs a tablas que no se copian (account_id, contact_id) se dejan en NULL:
para comparar montos por mes no hacen falta y arrastrarlos obligaría a copiar
media base.

Uso:
    python3 traer_cierres_a_local.py

Lee la URL de producción de .env y la local de .env.local. Aborta si las dos
apuntan al mismo lado.
"""
import sys

import psycopg2
import psycopg2.extras

# Solo lo que se necesita para el reporte mensual de cierres.
COLS_USUARIOS = ["id", "nombre", "rol_comercial", "especialidad_marca",
                 "en_turno", "perfil_multi_un"]
COLS_LEADS = ["id", "telefono", "email", "nombre", "origen", "marca_interes",
              "marcas_interes", "etapa_pipeline", "valor_estimado",
              "cantidad_productos", "precio_unitario", "tipo_venta",
              "tipo_cliente", "factura_monto", "factura_fecha",
              "factura_registrada_at", "fecha_creacion", "fecha_actualizacion",
              "fecha_cierre", "usuario_asignado_id", "empresa_nombre",
              # NOT NULL sin default en el esquema: si no se copian, el INSERT
              # local falla. Se detectaron consultando information_schema.
              "en_nurturing", "respondio_ultimo_contacto"]
COLS_SALES = ["id", "lead_id", "user_id", "unit", "sale_type", "sale_category",
              "monthly_amount", "total_amount", "commission_amount",
              "commission_status", "status", "closed_at", "created_at"]


def _url(archivo, etiqueta):
    try:
        for linea in open(archivo):
            if linea.startswith("DATABASE_URL="):
                return linea.split("=", 1)[1].strip()
    except FileNotFoundError:
        sys.exit(f"No encuentro {archivo}")
    sys.exit(f"{archivo} no define DATABASE_URL ({etiqueta})")


def _traer(cur_prod, tabla, cols, where=""):
    cur_prod.execute(f'SELECT {",".join(cols)} FROM {tabla} {where}')
    return cur_prod.fetchall()


def main():
    prod = _url(".env", "producción")
    local = _url(".env.local", "local")
    if "supabase" not in prod.lower():
        sys.exit("ABORTADO: .env no parece apuntar a producción.")
    if "supabase" in local.lower():
        sys.exit("ABORTADO: .env.local apunta a Supabase. Eso escribiría en producción.")

    print(f"origen  {prod.split('@')[-1].split('/')[0]}  (solo lectura)")
    print(f"destino {local.split('@')[-1].split('/')[0]}\n")

    cp = psycopg2.connect(prod)
    cp.set_session(readonly=True, autocommit=True)   # el servidor rechaza escrituras
    kp = cp.cursor()

    usuarios = _traer(kp, "usuarios", COLS_USUARIOS)
    leads = _traer(kp, "leads", COLS_LEADS,
                   "WHERE etapa_pipeline IN ('Cerrado Ganado','Cerrado Perdido')")
    ids = {l[0] for l in leads}
    ventas = [v for v in _traer(kp, "sales", COLS_SALES) if v[1] is None or v[1] in ids]
    kp.close(); cp.close()
    print(f"leído de producción: {len(usuarios)} vendedores, {len(leads)} leads "
          f"cerrados, {len(ventas)} ventas")

    cl = psycopg2.connect(local); kl = cl.cursor()
    # Se borra lo que hay para que la comparación no mezcle datos de prueba.
    # sale_participaciones y lead_atribuciones cuelgan de estos con FK.
    kl.execute("DELETE FROM sale_participaciones")
    kl.execute("DELETE FROM lead_atribuciones")
    kl.execute("DELETE FROM sales")
    kl.execute("DELETE FROM cierre_evidencias")
    kl.execute("DELETE FROM leads")

    def _upsert(tabla, cols, filas, conflicto="(id) DO NOTHING"):
        if not filas:
            return
        psycopg2.extras.execute_values(
            kl, f'INSERT INTO {tabla} ({",".join(cols)}) VALUES %s '
                f'ON CONFLICT {conflicto}', filas, page_size=500)

    _upsert("usuarios", COLS_USUARIOS, usuarios)
    _upsert("leads", COLS_LEADS, leads)
    _upsert("sales", COLS_SALES, ventas)
    cl.commit()

    kl.execute("SELECT count(*) FROM leads"); n_l = kl.fetchone()[0]
    kl.execute("SELECT count(*) FROM sales"); n_s = kl.fetchone()[0]
    kl.execute("SELECT count(*) FROM leads WHERE fecha_cierre IS NULL")
    sin_fecha = kl.fetchone()[0]
    kl.close(); cl.close()

    print(f"escrito en local:    {n_l} leads, {n_s} ventas")
    if sin_fecha:
        print(f"\n  ⚠  {sin_fecha} lead(s) cerrados sin fecha_cierre en producción.")
    print("\nListo. Ahora:  python3 comparar_cierres_por_mes.py --vendedores")


if __name__ == "__main__":
    main()
