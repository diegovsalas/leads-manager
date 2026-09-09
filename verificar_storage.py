#!/usr/bin/env python3
"""
Verifica que Supabase Storage esté bien configurado.

El error típico es poner en SUPABASE_URL el endpoint REST
(https://xxx.supabase.co/rest/v1/) en vez de la URL base del proyecto
(https://xxx.supabase.co). Con el primero, supabase-py arma
.../rest/v1/storage/v1/ y todo intento de subir archivo da 404 — pero el
código de tickets atrapa la excepción y sigue, así que las fotos se pierden
en silencio y nadie se entera.

Solo lista buckets. No sube, no borra, no modifica nada.

Uso — la llave sale de tu entorno, no se escribe en ningún archivo:

    SUPABASE_URL='https://xxx.supabase.co' \
    SUPABASE_SERVICE_KEY='...' \
    python3 verificar_storage.py
"""
import os
import sys

URL = (os.getenv("SUPABASE_URL") or "").strip()
KEY = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()

if not URL or not KEY:
    sys.exit("Falta SUPABASE_URL o SUPABASE_SERVICE_KEY en el entorno.")

# El chequeo que importa, antes de tocar la red.
if "/rest/v1" in URL or "/auth/v1" in URL or "/storage/v1" in URL:
    base = URL.split("/rest/v1")[0].split("/auth/v1")[0].split("/storage/v1")[0]
    sys.exit(
        f"MAL: SUPABASE_URL trae una ruta de API.\n"
        f"  tienes:  {URL}\n"
        f"  debe ser: {base.rstrip('/')}\n\n"
        f"Con la ruta incluida, Storage apunta a {URL.rstrip('/')}/storage/v1/\n"
        f"y toda subida da 404. En tickets el error se traga en silencio: las\n"
        f"fotos del portal se pierden sin aviso."
    )

from supabase import create_client  # noqa: E402

try:
    storage = create_client(URL, KEY).storage
    buckets = storage.list_buckets()
except Exception as e:
    sys.exit(f"No se pudo conectar a Storage: {type(e).__name__}: {e}")

print(f"OK. Storage responde en {URL.rstrip('/')}/storage/v1/\n")
nombres = {getattr(b, "name", None) or b.get("name") for b in buckets}
for n in sorted(x for x in nombres if x):
    print(f"  · {n}")

ESPERADOS = {
    "cierre-evidencias": "respaldo de cierre de Pestex",
    "tickets-fotos":     "fotos del portal de tickets",
    "facturas":          "PDF de facturas de incidencias",
}
faltan = {b: q for b, q in ESPERADOS.items() if b not in nombres}
if faltan:
    print("\nBuckets que la app crea sola la primera vez que los usa:")
    for b, q in faltan.items():
        print(f"  · {b}  ({q})")
    print("\nNo hace falta crearlos a mano; con la llave correcta se crean solos.")
