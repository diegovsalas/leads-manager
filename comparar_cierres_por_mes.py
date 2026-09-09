#!/usr/bin/env python3
"""
Compara el reporte mensual de ventas ANTES y DESPUÉS del arreglo.

Antes, Metas y Dashboard medían el mes con `fecha_creacion` —cuándo ENTRÓ el
lead— en vez de cuándo se cerró la venta. Un trato que entraba en julio y
cerraba en septiembre se contaba en julio. Este script muestra, mes por mes,
cuánto se mueve cada cifra.

    python3 comparar_cierres_por_mes.py               # base local
    python3 comparar_cierres_por_mes.py --vendedores  # desglose por vendedor
    python3 comparar_cierres_por_mes.py --produccion  # contra Supabase

NO importa create_app a propósito: create_app() corre las migraciones
automáticas y db.create_all(), o sea ESCRIBE en la base. Un script de
comparación no tiene por qué poder tocar el esquema. Aquí la conexión es
directa y la sesión se abre en READ ONLY, así que el servidor rechaza
cualquier escritura aunque el código intentara una.

Contra producción hay que pasar --produccion. Sin esa bandera el script
aborta si detecta Supabase en DATABASE_URL: leer producción por accidente,
al cargar .env sin querer, es justo el error que este bloqueo evita.
"""
import os
import sys
from collections import defaultdict

import psycopg2

POR_VENDEDOR = "--vendedores" in sys.argv
CONFIRMA_PROD = "--produccion" in sys.argv

# El valor de un lead ganado, con la misma cascada que usan Metas y Dashboard.
VALOR = ("COALESCE(l.factura_monto, l.cantidad_productos * l.precio_unitario, "
         "l.valor_estimado, 0)")


def _conexion():
    url = os.getenv("DATABASE_URL", "")
    if not url:
        for linea in open(".env.local"):
            if linea.startswith("DATABASE_URL="):
                url = linea.split("=", 1)[1].strip()
                break
    if not url:
        sys.exit("Falta DATABASE_URL.")
    es_prod = "supabase" in url.lower()
    if es_prod and not CONFIRMA_PROD:
        sys.exit("ABORTADO: DATABASE_URL apunta a producción.\n"
                 "Si es a propósito, vuelve a correrlo con --produccion.")
    print(f"base: {url.split('@')[-1].split('/')[0]}"
          f"{'   (PRODUCCIÓN, solo lectura)' if es_prod else ''}\n")
    con = psycopg2.connect(url)
    con.set_session(readonly=True, autocommit=True)   # el servidor rechaza escrituras
    return con


def _por_mes(cur, campo):
    cur.execute(f"""
        SELECT to_char(l.{campo}, 'YYYY-MM'), COUNT(*), COALESCE(SUM({VALOR}), 0)
        FROM leads l
        WHERE l.etapa_pipeline = 'Cerrado Ganado' AND l.{campo} IS NOT NULL
        GROUP BY 1
    """)
    return {m: (n, float(v)) for m, n, v in cur.fetchall()}


def _por_vendedor(cur, campo):
    cur.execute(f"""
        SELECT to_char(l.{campo}, 'YYYY-MM'),
               COALESCE(u.nombre, 'Sin asignar'),
               COALESCE(SUM({VALOR}), 0)
        FROM leads l
        LEFT JOIN usuarios u ON u.id = l.usuario_asignado_id
        WHERE l.etapa_pipeline = 'Cerrado Ganado' AND l.{campo} IS NOT NULL
        GROUP BY 1, 2
    """)
    d = defaultdict(dict)
    for mes, vend, monto in cur.fetchall():
        d[mes][vend] = float(monto)
    return d


def main():
    con = _conexion()
    cur = con.cursor()

    cur.execute("""SELECT 1 FROM information_schema.columns
                   WHERE table_name='leads' AND column_name='fecha_cierre'""")
    if not cur.fetchone():
        sys.exit("Esta base todavía no tiene leads.fecha_cierre: "
                 "arranca la app una vez para que corra la migración.")

    cur.execute("""SELECT COUNT(*) FROM leads
                   WHERE etapa_pipeline='Cerrado Ganado' AND fecha_cierre IS NULL""")
    huerfanos = cur.fetchone()[0]

    antes, ahora = _por_mes(cur, "fecha_creacion"), _por_mes(cur, "fecha_cierre")
    meses = sorted(set(antes) | set(ahora))
    if not meses:
        print("No hay leads en Cerrado Ganado.")
        return

    print(f"{'mes':<9} {'ANTES (por creación)':>26} {'AHORA (por cierre)':>26} {'diferencia':>16}")
    print("─" * 82)
    tot_a = tot_d = 0.0
    for m in meses:
        na, ma = antes.get(m, (0, 0.0))
        nd, md = ahora.get(m, (0, 0.0))
        delta = md - ma
        tot_a, tot_d = tot_a + ma, tot_d + md
        marca = "  " if abs(delta) < 0.5 else ("↑" if delta > 0 else "↓")
        print(f"{m:<9} {na:>4} · ${ma:>18,.0f} {nd:>4} · ${md:>18,.0f} {marca} ${abs(delta):>13,.0f}")
    print("─" * 82)
    print(f"{'TOTAL':<9} {'':>6}${tot_a:>18,.0f} {'':>6}${tot_d:>18,.0f}")

    if abs(tot_a - tot_d) > 0.5:
        print(f"\n  El total difiere en ${abs(tot_a - tot_d):,.0f}: son los ganados "
              f"sin fecha de cierre, que quedan fuera del reporte nuevo.")
    if huerfanos:
        print(f"\n  ⚠  {huerfanos} lead(s) en Cerrado Ganado sin fecha_cierre. "
              f"No aparecen en ningún mes.")

    if POR_VENDEDOR:
        pa, pd = _por_vendedor(cur, "fecha_creacion"), _por_vendedor(cur, "fecha_cierre")
        print("\n\nPor vendedor (solo donde cambia algo)")
        for m in meses:
            nombres = sorted(set(pa.get(m, {})) | set(pd.get(m, {})))
            filas = [(n, pa.get(m, {}).get(n, 0.0), pd.get(m, {}).get(n, 0.0)) for n in nombres]
            filas = [f for f in filas if abs(f[1] - f[2]) > 0.5]
            if not filas:
                continue
            print(f"\n  {m}")
            for n, a, d in filas:
                print(f"    {n:<26} ${a:>14,.0f} → ${d:>14,.0f}   ({d - a:+,.0f})")

    cur.close()
    con.close()


if __name__ == "__main__":
    main()
