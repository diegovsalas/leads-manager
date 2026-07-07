# backups.py
"""
Backup diario REAL del CRM (SECURITY-2026-06-24).

Antes este módulo solo contaba filas y escribía a stderr (no respaldaba nada).
Ahora:
  1) Descubre tablas del schema public.
  2) Hace COPY ... TO STDOUT WITH CSV HEADER por cada tabla (excluye logs masivos
     y tablas re-sincronizables desde Savio para mantener el archivo manejable).
  3) Empaqueta en tar.gz en /tmp.
  4) Sube a Supabase Storage (bucket configurable) vía REST.
  5) Rota: borra backups > BACKUP_RETENTION_DAYS días.

Esta es la SEGUNDA línea de defensa. La PRIMERA es el snapshot diario automático
de Supabase Free (7 días de retención, fuera de nuestro código).

Configuración (Render env vars):
  SUPABASE_URL              — https://<proj>.supabase.co
  SUPABASE_SERVICE_KEY      — service_role key (NUNCA exponer al cliente)
  SUPABASE_BACKUP_BUCKET    — default 'crm-backups' (créalo en Supabase Studio)
  BACKUP_RETENTION_DAYS     — default 14
"""
import io
import logging
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone

import requests
from sqlalchemy import text as _sa_text

from extensions import db

log = logging.getLogger("backups")

# Tablas que NO respaldamos (logs masivos o re-sincronizables desde origen).
TABLAS_EXCLUIDAS = {
    # Logs masivos: si se pierden, no es crítico
    "mensajes_whatsapp",
    "sales_emails",
    "chatbot_messages",
    "chatbot_conversations",
    "sdr_dir_history",
    "chat_messages",
    "actividad_log",        # útil pero crece rápido — queda en el snapshot Supabase
    # Re-sincronizables desde Savio (no perdemos data por no respaldar aquí):
    "savio_customers",
    "savio_subscriptions",
    "savio_invoices",
    "savio_payments",
    # Tablas operacionales que se hidratan rapidísimo de origen:
    "zoho_tokens",
    "api_costs",
}


def _list_tables():
    """Lista tablas del schema public, excluye blacklist y sistema."""
    rows = db.session.execute(_sa_text("""
        SELECT tablename FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename NOT LIKE 'pg_%'
          AND tablename NOT LIKE 'sql_%'
        ORDER BY tablename
    """)).fetchall()
    return [r[0] for r in rows if r[0] not in TABLAS_EXCLUIDAS]


def _dump_table_to_file(table: str, target_path: str) -> int:
    """FIX-2026-07-07: dump directo a archivo — no carga en memoria.

    Antes usaba io.BytesIO() como buffer intermedio; con savio_invoices
    (85MB) eso pegaba en el heap Python y contribuía al OOM en Render 512MB.
    Ahora hace COPY TO STDOUT directo a un file handle → memoria constante.
    Retorna el tamaño en bytes del archivo escrito.
    """
    raw = db.session.connection().connection
    cur = raw.cursor()
    with open(target_path, "wb") as fp:
        cur.copy_expert(f'COPY "{table}" TO STDOUT WITH CSV HEADER', fp)
    cur.close()
    return os.path.getsize(target_path)


def _build_archive() -> tuple:
    """Genera tar.gz con CSVs de todas las tablas + manifest. Retorna (path, filename, size).

    FIX-2026-07-07: cada CSV se escribe primero a un archivo temporal
    individual, luego se agrega al tar con tar.add(<path>) que también
    hace streaming. Peak memory ~10MB independientemente del tamaño total.
    """
    ahora = datetime.now(timezone.utc)
    ts = ahora.strftime("%Y%m%d_%H%M%S")
    filename = f"crm_backup_{ts}.tar.gz"
    path = os.path.join(tempfile.gettempdir(), filename)

    tablas = _list_tables()
    manifest = [f"# CRM backup — {ahora.isoformat()}", f"# tablas: {len(tablas)}", ""]

    # Dir temporal para los CSVs individuales (se limpia al final)
    tmpdir = tempfile.mkdtemp(prefix="crm_bk_")
    try:
        with tarfile.open(path, "w:gz") as tar:
            for t in tablas:
                csv_path = os.path.join(tmpdir, f"{t}.csv")
                try:
                    size = _dump_table_to_file(t, csv_path)
                    tar.add(csv_path, arcname=f"{t}.csv")
                    manifest.append(f"{t}: {size} bytes")
                    # Liberar el archivo temporal después de agregarlo al tar
                    try: os.unlink(csv_path)
                    except OSError: pass
                except Exception as e:
                    log.warning(f"dump {t}: {e}")
                    manifest.append(f"{t}: ERROR ({e})")

            # Manifest al final (chico, sí cabe en BytesIO)
            manifest_bytes = "\n".join(manifest).encode("utf-8")
            minfo = tarfile.TarInfo(name="MANIFEST.txt")
            minfo.size = len(manifest_bytes)
            minfo.mtime = int(ahora.timestamp())
            tar.addfile(minfo, io.BytesIO(manifest_bytes))
    finally:
        # Cleanup del dir temporal
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    return path, filename, os.path.getsize(path)


def _supabase_upload(local_path: str, filename: str) -> bool:
    """Sube el archivo a Supabase Storage. True si OK."""
    url_base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key      = os.getenv("SUPABASE_SERVICE_KEY", "")
    bucket   = os.getenv("SUPABASE_BACKUP_BUCKET", "crm-backups")

    if not url_base or not key:
        log.warning("backup: SUPABASE_URL o SUPABASE_SERVICE_KEY no configurados — "
                    "el archivo quedó SOLO en /tmp y se borra al redeploy. "
                    "Configurar para activar upload.")
        return False

    upload_url = f"{url_base}/storage/v1/object/{bucket}/{filename}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/gzip",
        "x-upsert":      "true",
    }
    with open(local_path, "rb") as f:
        r = requests.post(upload_url, headers=headers, data=f, timeout=120)
    if r.status_code in (200, 201):
        log.info(f"backup subido: {bucket}/{filename} ({os.path.getsize(local_path)} bytes)")
        return True
    log.warning(f"backup upload falló {r.status_code}: {r.text[:200]}")
    return False


def _supabase_rotate(retention_days: int):
    """Borra backups > retention_days días del bucket."""
    url_base = os.getenv("SUPABASE_URL", "").rstrip("/")
    key      = os.getenv("SUPABASE_SERVICE_KEY", "")
    bucket   = os.getenv("SUPABASE_BACKUP_BUCKET", "crm-backups")
    if not url_base or not key:
        return

    list_url = f"{url_base}/storage/v1/object/list/{bucket}"
    headers  = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    r = requests.post(list_url, headers=headers,
                      json={"limit": 1000, "offset": 0, "prefix": "crm_backup_"},
                      timeout=30)
    if r.status_code != 200:
        log.warning(f"backup rotate list falló: {r.status_code}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    to_delete = []
    for item in r.json():
        created = item.get("created_at") or item.get("updated_at")
        if not created:
            continue
        try:
            cdt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if cdt < cutoff:
            to_delete.append(item["name"])

    if not to_delete:
        return

    del_url = f"{url_base}/storage/v1/object/{bucket}"
    dr = requests.delete(del_url, headers=headers,
                         json={"prefixes": to_delete}, timeout=30)
    if dr.status_code == 200:
        log.info(f"backup rotate: borrados {len(to_delete)} archivos > {retention_days}d")
    else:
        log.warning(f"backup rotate delete falló: {dr.status_code}")


def ejecutar_backup():
    """Punto de entrada del cron diario (3am CST)."""
    try:
        path, filename, size = _build_archive()
        uploaded = _supabase_upload(path, filename)
        if uploaded:
            retention = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))
            try:
                _supabase_rotate(retention)
            except Exception as e:
                log.warning(f"backup rotate error (no fatal): {e}")
        # Limpia el local SIEMPRE para no llenar /tmp
        try:
            os.unlink(path)
        except OSError:
            pass
        print(
            f"[Backup] OK: {filename} ({size} bytes) — uploaded={uploaded}",
            file=sys.stderr,
        )
        return {"ok": True, "filename": filename, "size": size, "uploaded": uploaded}
    except Exception as e:
        log.exception("backup falló")
        print(f"[Backup] FAIL: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}
