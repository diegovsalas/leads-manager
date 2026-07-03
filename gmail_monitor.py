"""
Monitor de correos salientes de vendedores (Gmail Workspace).

Lee correos enviados (in:sent) por cada vendedor con gmail_address registrado
en `usuarios`, filtrando para que el destinatario NO sea @grupoavantex.com
(solo correos a clientes/prospectos externos), y los persiste en sales_emails.

Auth: Service Account con domain-wide delegation. La cuenta de servicio
impersona cada vendedor para acceder a su Gmail (read-only).

Config:
  GMAIL_SERVICE_ACCOUNT_JSON  — string con el JSON del service account
  GMAIL_INTERNAL_DOMAIN       — dominio interno a excluir (default 'grupoavantex.com')
  GMAIL_POLL_LOOKBACK_MIN     — cuántos minutos atrás revisar en cada poll (default 15)
  GMAIL_RETENTION_DAYS        — días a conservar en BD (default 365)
"""
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from extensions import db
from models import Usuario, SalesEmail

log = logging.getLogger("gmail_monitor")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
INTERNAL_DOMAIN = os.getenv("GMAIL_INTERNAL_DOMAIN", "grupoavantex.com").lower()
LOOKBACK_MIN = int(os.getenv("GMAIL_POLL_LOOKBACK_MIN", "15"))
RETENTION_DAYS = int(os.getenv("GMAIL_RETENTION_DAYS", "365"))
# Ventana de backfill inicial cuando un vendedor se agrega por primera vez.
# Solo aplica UNA vez por vendedor (después se marca gmail_backfilled_at).
BACKFILL_DAYS = int(os.getenv("GMAIL_BACKFILL_DAYS", "30"))


def _load_credentials_json() -> Optional[dict]:
    raw = os.getenv("GMAIL_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"GMAIL_SERVICE_ACCOUNT_JSON inválido: {e}")
        return None


def is_configured() -> bool:
    return _load_credentials_json() is not None


def _build_service(impersonate_email: str):
    """Construye cliente Gmail API impersonando un usuario del dominio."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_info = _load_credentials_json()
    if not creds_info:
        raise RuntimeError("GMAIL_SERVICE_ACCOUNT_JSON no configurado")
    creds = service_account.Credentials.from_service_account_info(
        creds_info, scopes=SCOPES, subject=impersonate_email,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list, name: str) -> Optional[str]:
    """Extrae el valor de un header por nombre case-insensitive."""
    name_lower = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == name_lower:
            return h.get("value")
    return None


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")


def _extract_emails(header_value: Optional[str]) -> list:
    """De un header tipo To/Cc 'Foo <a@b.com>, c@d.com' saca lista de emails."""
    if not header_value:
        return []
    return [e.lower() for e in _EMAIL_RE.findall(header_value)]


def _all_external(emails: list) -> bool:
    """True si la lista tiene al menos un email Y ninguno es del dominio interno."""
    if not emails:
        return False
    return all(not e.endswith("@" + INTERNAL_DOMAIN) for e in emails)


def _parse_gmail_message(msg: dict) -> Optional[dict]:
    """Convierte un message resource de Gmail API a dict listo para SalesEmail."""
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    to_raw = _header(headers, "To")
    cc_raw = _header(headers, "Cc")
    from_raw = _header(headers, "From")
    subject = _header(headers, "Subject") or ""
    date_raw = _header(headers, "Date")

    to_list = _extract_emails(to_raw)
    cc_list = _extract_emails(cc_raw)
    from_list = _extract_emails(from_raw)
    from_email = from_list[0] if from_list else ""

    # FILTRO clave: si CUALQUIER destinatario es interno, descartar el correo
    # (solo capturamos correos genuinamente externos a clientes/prospectos)
    if not to_list and not cc_list:
        return None
    all_dests = to_list + cc_list
    if any(e.endswith("@" + INTERNAL_DOMAIN) for e in all_dests):
        return None
    if not _all_external(all_dests):
        return None

    # internalDate es ms desde epoch (más confiable que Date header)
    internal_ms = msg.get("internalDate")
    sent_at = None
    if internal_ms:
        try:
            sent_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            pass
    if not sent_at:
        sent_at = datetime.now(timezone.utc)

    # snippet: Gmail lo provee directo (típicamente ~100-200 chars)
    snippet = (msg.get("snippet") or "")[:200]

    # Body completo + adjuntos
    bodies = _extract_bodies(payload)
    has_attachment = bool(bodies["attachments"]) or _has_attachment(payload)

    return {
        "gmail_message_id": msg.get("id"),
        "gmail_thread_id":  msg.get("threadId"),
        "sent_at":          sent_at,
        "from_email":       from_email,
        "to_emails":        to_list,
        "cc_emails":        cc_list,
        "subject":          subject[:500] if subject else None,
        "snippet":          snippet,
        "body_text":        bodies["text"] or None,
        "body_html":        bodies["html"] or None,
        "attachments":      bodies["attachments"],
        "has_attachment":   has_attachment,
    }


def _has_attachment(payload: dict) -> bool:
    if not payload:
        return False
    if payload.get("filename"):
        return True
    for part in (payload.get("parts") or []):
        if _has_attachment(part):
            return True
    return False


_BODY_TEXT_MAX = 200_000   # ~200KB tope para body_text
_BODY_HTML_MAX = 500_000   # ~500KB tope para body_html (puede traer imágenes inline base64)


def _decode_body(data_b64url: str) -> str:
    if not data_b64url:
        return ""
    try:
        return base64.urlsafe_b64decode(data_b64url).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_bodies(payload: dict, acc: dict = None) -> dict:
    """Recorre el payload MIME del mensaje, acumula body_text y body_html.
    Retorna {'text': str, 'html': str, 'attachments': [{filename,mime,size}]}.
    Solo procesa partes inline; los adjuntos solo se listan, no se descargan."""
    if acc is None:
        acc = {"text": "", "html": "", "attachments": []}
    if not payload:
        return acc

    mime = (payload.get("mimeType") or "").lower()
    filename = payload.get("filename") or ""
    body = payload.get("body") or {}
    parts = payload.get("parts") or []

    # Adjunto: tiene filename y attachmentId (data está en body.attachmentId, no inline)
    if filename:
        acc["attachments"].append({
            "filename":      filename,
            "mime_type":     mime,
            "size":          body.get("size", 0),
            "attachment_id": body.get("attachmentId"),  # para descargar después
        })
    else:
        data = body.get("data")
        if data and mime == "text/plain" and len(acc["text"]) < _BODY_TEXT_MAX:
            decoded = _decode_body(data)
            acc["text"] = (acc["text"] + "\n" + decoded)[:_BODY_TEXT_MAX] if acc["text"] else decoded[:_BODY_TEXT_MAX]
        elif data and mime == "text/html" and len(acc["html"]) < _BODY_HTML_MAX:
            decoded = _decode_body(data)
            acc["html"] = (acc["html"] + "\n" + decoded)[:_BODY_HTML_MAX] if acc["html"] else decoded[:_BODY_HTML_MAX]

    for part in parts:
        _extract_bodies(part, acc)

    return acc


def _query_for_lookback(minutes: int) -> str:
    """Gmail query: enviados en los últimos N minutos, excluyendo destinatarios
    internos. `newer_than` con unidad mínima h; para minutos usamos `after:`."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(minutes=minutes)).timestamp())
    # Filtro a nivel de query: -to:@dominio (puede haber falsos negativos si hay
    # múltiples destinatarios mezclados; el parser hace doble check después)
    return f"in:sent after:{after_ts} -to:@{INTERNAL_DOMAIN}"


def _has_attachment_ids(email: "SalesEmail") -> bool:
    """True si todos los attachments del correo tienen attachment_id (campo
    necesario para poder descargar). Si no, hay que re-fetch."""
    atts = email.attachments or []
    for a in atts:
        if a.get("filename") and not a.get("attachment_id"):
            return False
    return True


def poll_vendor(vendedor: Usuario, lookback_min: int = LOOKBACK_MIN,
                backfill_bodies: bool = True, force_refresh: bool = False) -> dict:
    """Pull correos salientes de un vendedor.

    backfill_bodies=True (default): si encuentra un correo ya guardado pero
    sin body_text/body_html, lo re-fetcha con format=full y actualiza la fila.
    force_refresh=True: re-fetch incluso si ya tiene body (útil para refrescar
    attachment_id u otros campos nuevos del schema).

    Backfill automático para vendedores nuevos: si gmail_backfilled_at es None,
    se usa una ventana extendida de BACKFILL_DAYS solo en este poll, y se
    marca el timestamp para que el siguiente poll vuelva al lookback normal.
    """
    stats = {"vendedor": vendedor.nombre, "fetched": 0, "saved": 0,
             "skipped_internal": 0, "backfilled": 0, "errors": 0,
             "initial_backfill": False}
    if not vendedor.gmail_address:
        return stats

    try:
        svc = _build_service(vendedor.gmail_address)
    except Exception as e:
        log.exception(f"build_service falló para {vendedor.gmail_address}: {e}")
        stats["errors"] = 1
        return stats

    # ── Backfill inicial para vendedores nuevos ─────────────────────
    is_initial = getattr(vendedor, "gmail_backfilled_at", None) is None
    effective_lookback = lookback_min
    if is_initial:
        effective_lookback = BACKFILL_DAYS * 24 * 60  # días → minutos
        stats["initial_backfill"] = True
        log.info(f"[gmail] backfill inicial para {vendedor.nombre} ({vendedor.gmail_address}): {BACKFILL_DAYS} días")

    query = _query_for_lookback(effective_lookback)
    try:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=500).execute()
    except Exception as e:
        log.exception(f"list falló para {vendedor.gmail_address}: {e}")
        stats["errors"] = 1
        return stats

    messages = resp.get("messages") or []
    stats["fetched"] = len(messages)

    for m in messages:
        mid = m.get("id")
        if not mid:
            continue

        existing = SalesEmail.query.filter_by(gmail_message_id=mid).first()
        if existing:
            # Decide si re-fetch necesario
            has_body = existing.body_text or existing.body_html
            needs_att_ids = bool(existing.attachments) and not _has_attachment_ids(existing)
            if has_body and not force_refresh and not needs_att_ids:
                continue
            if not has_body and not backfill_bodies:
                continue

        try:
            # format=full: trae body + adjuntos + headers
            msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        except Exception as e:
            log.warning(f"get message {mid} falló: {e}")
            stats["errors"] += 1
            continue

        parsed = _parse_gmail_message(msg)
        if not parsed:
            stats["skipped_internal"] += 1
            continue

        try:
            if existing:
                # UPDATE: backfill body en correo viejo
                for k, v in parsed.items():
                    if k != "gmail_message_id":  # no toques el PK lógico
                        setattr(existing, k, v)
                stats["backfilled"] += 1
            else:
                # INSERT nuevo
                email = SalesEmail(vendedor_id=vendedor.id, **parsed)
                db.session.add(email)
                stats["saved"] += 1
            db.session.flush()
        except Exception as e:
            db.session.rollback()
            log.warning(f"persist msg {mid} falló: {e}")
            stats["errors"] += 1

    # Marcar backfill como completado (independiente de si hubo saved>0 o no)
    if is_initial:
        vendedor.gmail_backfilled_at = datetime.now(timezone.utc)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.exception(f"commit final falló: {e}")
        stats["errors"] += 1

    return stats


def poll_all(lookback_min: int = LOOKBACK_MIN, backfill_bodies: bool = True,
             force_refresh: bool = False) -> dict:
    """Itera todos los vendedores con gmail_address y los polea."""
    if not is_configured():
        return {"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado"}

    vendedores = Usuario.query.filter(
        Usuario.gmail_address.isnot(None),
        Usuario.gmail_address != "",
    ).all()

    results = []
    for v in vendedores:
        results.append(poll_vendor(v, lookback_min=lookback_min,
                                    backfill_bodies=backfill_bodies,
                                    force_refresh=force_refresh))
    return {
        "vendedores":         len(vendedores),
        "total_saved":        sum(r["saved"] for r in results),
        "total_backfilled":   sum(r.get("backfilled", 0) for r in results),
        "total_fetched":      sum(r["fetched"] for r in results),
        "detalle":            results,
    }


def purge_old(days: int = RETENTION_DAYS) -> dict:
    """Borra registros con sent_at más viejos que `days`."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = SalesEmail.query.filter(SalesEmail.sent_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


# ──────────────────────────────────────────────
# KAM email response time tracking
# ──────────────────────────────────────────────
KAM_RESPONSE_LOOKBACK_DAYS = int(os.getenv("KAM_RESPONSE_LOOKBACK_DAYS", "30"))


def poll_kam_responses(lookback_days: int = KAM_RESPONSE_LOOKBACK_DAYS) -> dict:
    """Mide el tiempo de primera respuesta de cada KAM a emails de clientes externos.

    Para cada KAM activo con correo @{INTERNAL_DOMAIN}:
    1. Obtiene sus emails enviados en los últimos N días (excluyendo internos)
    2. Por cada hilo con >=2 mensajes, busca el primer email del cliente (externo)
       y la primera respuesta del KAM — calcula response_hours
    3. Persiste/actualiza KAMEmailResponse (upsert por kam_id + gmail_thread_id)
    """
    if not is_configured():
        return {"error": "GMAIL_SERVICE_ACCOUNT_JSON no configurado"}

    from models import UserCRM, RolCRM

    kams = UserCRM.query.filter(
        UserCRM.rol == RolCRM.KAM,
        UserCRM.activo.is_(True),
        UserCRM.correo.isnot(None),
        UserCRM.correo != "",
    ).all()

    results = []
    for kam in kams:
        if not kam.correo.lower().endswith("@" + INTERNAL_DOMAIN):
            continue
        stats = _poll_kam_for(kam, lookback_days)
        results.append(stats)

    return {
        "kams":          len(results),
        "total_saved":   sum(r.get("saved", 0) for r in results),
        "total_updated": sum(r.get("updated", 0) for r in results),
        "detalle":       results,
    }


def _build_contact_email_map(kam) -> dict:
    """Devuelve {email_lower: account_id (UUID)} para todos los contactos de
    las cuentas que el KAM atiende. Se usa para correlacionar threads a cuentas."""
    from models import CSAccount, CSContacto
    accounts = CSAccount.query.filter_by(kam_id=kam.id).all()
    mapping: dict = {}
    for acc in accounts:
        for c in acc.contactos:
            email = (c.correo or "").strip().lower()
            if email:
                mapping[email] = acc.id
    return mapping


def _build_domain_map(kam) -> dict:
    """Devuelve {dominio_lower: account_id} para heurística de correlación
    cuando el email exacto del cliente no matchea un contacto registrado.

    FEAT-2026-07-03: Estrategia C — deriva el dominio de:
      1. Correos de cs_contactos de las cuentas del KAM (contact_map ya
         cubre el email exacto; aquí extraemos SU dominio como pista)
      2. Nombre de la cuenta transformado a dominio candidato
         (ej. "AUTOZONE" → "autozone", "Farmacias del Ahorro" → "fahorro")

    NO mapea dominios genéricos (@gmail.com, @hotmail.com, etc.) para evitar
    falsos positivos.
    """
    from models import CSAccount, CSContacto
    genericos = {
        "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.mx",
        "live.com", "icloud.com", "aol.com", "me.com", "prodigy.net.mx",
    }
    accounts = CSAccount.query.filter_by(kam_id=kam.id).all()
    mapping: dict = {}
    for acc in accounts:
        for c in acc.contactos:
            email = (c.correo or "").strip().lower()
            if "@" in email:
                dom = email.split("@", 1)[1]
                if dom and dom not in genericos:
                    mapping.setdefault(dom, acc.id)
    return mapping


def _correlacionar(client_email, contact_map, domain_map, thread_account):
    """FEAT-2026-07-03 Estrategia C: correlaciona el email del cliente a
    una cuenta CS priorizando (1) match exacto de contacto, (2) match por
    dominio del contacto, (3) heurística de thread (si algún otro email
    del mismo thread ya se imputó a una cuenta, se hereda)."""
    if not client_email:
        return thread_account
    email = client_email.lower().strip()
    # 1. Match exacto
    if email in contact_map:
        return contact_map[email]
    # 2. Match por dominio
    if "@" in email:
        dom = email.split("@", 1)[1]
        if dom in domain_map:
            return domain_map[dom]
    # 3. Heurística de thread (si venimos con una cuenta ya inferida)
    return thread_account


def _poll_kam_for(kam, lookback_days: int) -> dict:
    stats = {"kam": kam.nombre, "saved": 0, "updated": 0, "skipped": 0, "errors": 0}

    try:
        svc = _build_service(kam.correo)
    except Exception as e:
        log.exception(f"[kam_response] build_service failed for {kam.correo}: {e}")
        stats["errors"] = 1
        return stats

    # Mapas de correlación (Estrategia C: exacto + dominio + heurística thread)
    contact_map = _build_contact_email_map(kam)
    domain_map  = _build_domain_map(kam)

    after_ts = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    query = f"in:sent after:{after_ts} -to:@{INTERNAL_DOMAIN}"

    try:
        resp = svc.users().messages().list(userId="me", q=query, maxResults=200).execute()
    except Exception as e:
        log.exception(f"[kam_response] list failed for {kam.correo}: {e}")
        stats["errors"] = 1
        return stats

    messages = resp.get("messages") or []
    seen_threads: set = set()

    for m in messages:
        tid = m.get("threadId")
        if not tid or tid in seen_threads:
            continue
        seen_threads.add(tid)
        _process_kam_thread(svc, kam, tid, contact_map, domain_map, stats)

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        log.exception(f"[kam_response] commit failed for {kam.nombre}: {e}")
        stats["errors"] += 1

    return stats


def _process_kam_thread(svc, kam, thread_id: str, contact_map: dict, domain_map: dict, stats: dict) -> None:
    from models import KAMEmailResponse

    try:
        thread = svc.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
    except Exception as e:
        log.warning(f"[kam_response] get thread {thread_id} failed: {e}")
        stats["errors"] += 1
        return

    msgs = thread.get("messages") or []
    if len(msgs) < 2:
        stats["skipped"] += 1
        return

    kam_email = kam.correo.lower()
    first_external: Optional[dict] = None
    first_kam_reply: Optional[datetime] = None

    for msg in msgs:
        headers = (msg.get("payload") or {}).get("headers") or []
        from_val = next((h["value"] for h in headers if (h.get("name") or "").lower() == "from"), "")
        from_emails = _extract_emails(from_val)
        is_from_kam = kam_email in [e.lower() for e in from_emails]

        internal_ms = msg.get("internalDate")
        if not internal_ms:
            continue
        try:
            msg_at = datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            continue

        if not is_from_kam and first_external is None:
            # Solo mensajes de externos reales (no del dominio interno)
            if from_emails and not any(e.lower().endswith("@" + INTERNAL_DOMAIN) for e in from_emails):
                subject_val = next(
                    (h["value"] for h in headers if (h.get("name") or "").lower() == "subject"), ""
                )
                first_external = {
                    "at": msg_at,
                    "from_email": from_emails[0].lower() if from_emails else None,
                    "subject": subject_val,
                }
        elif is_from_kam and first_external is not None and first_kam_reply is None:
            first_kam_reply = msg_at

    if not first_external or not first_kam_reply:
        stats["skipped"] += 1
        return

    response_secs = (first_kam_reply - first_external["at"]).total_seconds()
    if response_secs <= 0:
        stats["skipped"] += 1
        return

    response_hours = round(response_secs / 3600, 2)

    # Correlación con cuenta CS (Estrategia C: exacto → dominio → heurística thread).
    # thread_account: si algún KAMEmailResponse previo de OTRO email del mismo
    # thread ya se imputó a una cuenta, lo heredamos.
    from models import KAMEmailResponse as _KER
    client_email = first_external["from_email"]
    thread_account = None
    prev = _KER.query.filter_by(gmail_thread_id=thread_id).filter(
        _KER.account_id.isnot(None)
    ).first()
    if prev:
        thread_account = prev.account_id
    account_id = _correlacionar(client_email, contact_map, domain_map, thread_account)

    try:
        existing = KAMEmailResponse.query.filter_by(
            kam_id=kam.id, gmail_thread_id=thread_id
        ).first()

        if existing:
            existing.received_at    = first_external["at"]
            existing.replied_at     = first_kam_reply
            existing.response_hours = response_hours
            existing.synced_at      = datetime.now(timezone.utc)
            # Actualiza account_id si ahora hay match (puede haberse registrado el contacto después)
            if account_id and existing.account_id is None:
                existing.account_id = account_id
            stats["updated"] += 1
        else:
            db.session.add(KAMEmailResponse(
                kam_id          = kam.id,
                account_id      = account_id,
                gmail_thread_id = thread_id,
                subject         = (first_external["subject"] or "")[:500] or None,
                client_email    = client_email,
                received_at     = first_external["at"],
                replied_at      = first_kam_reply,
                response_hours  = response_hours,
            ))
            stats["saved"] += 1

        db.session.flush()
    except Exception as e:
        db.session.rollback()
        log.warning(f"[kam_response] persist thread {thread_id} failed: {e}")
        stats["errors"] += 1
