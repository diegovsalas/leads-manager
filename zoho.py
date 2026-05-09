"""
Zoho CRM integration. Port de vendedores.cloud/zoho.js.

OAuth2 con persistencia de tokens en DB (modelo ZohoToken, single-row).
Refresh automático cuando access_token expira (margen 60s).
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from urllib.parse import urlencode

from extensions import db
from models import ZohoToken

log = logging.getLogger("zoho")

ZOHO_ACCOUNTS = "https://accounts.zoho.com"
ZOHO_API = "https://www.zohoapis.com/crm/v2"

CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("ZOHO_REDIRECT_URI", "")

SCOPE = "ZohoCRM.modules.ALL,ZohoCRM.settings.ALL,ZohoCRM.users.ALL"


def _get_or_create_row() -> ZohoToken:
    row = db.session.get(ZohoToken, 1)
    if not row:
        row = ZohoToken(id=1)
        db.session.add(row)
        db.session.flush()
    return row


def get_auth_url() -> str:
    qs = urlencode({
        "scope": SCOPE,
        "client_id": CLIENT_ID,
        "response_type": "code",
        "access_type": "offline",
        "redirect_uri": REDIRECT_URI,
        "prompt": "consent",
    }, safe=",")
    return f"{ZOHO_ACCOUNTS}/oauth/v2/auth?{qs}"


def exchange_code(code: str) -> dict:
    """Intercambia el authorization code por tokens. Persiste en DB."""
    resp = requests.post(
        f"{ZOHO_ACCOUNTS}/oauth/v2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": code,
        }, timeout=20,
    )
    data = resp.json() if resp.status_code < 500 else {}
    access = data.get("access_token")
    if not access:
        return {"ok": False, "error": data}
    expires_in = int(data.get("expires_in") or 3600)
    row = _get_or_create_row()
    row.access_token = access
    if data.get("refresh_token"):
        row.refresh_token = data["refresh_token"]
    row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    row.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return {"ok": True, "expires_at": row.expires_at.isoformat()}


def _refresh_access_token(row: ZohoToken) -> str:
    if not row.refresh_token:
        raise RuntimeError("No refresh token. Re-authorize at /api/zoho/connect")
    resp = requests.post(
        f"{ZOHO_ACCOUNTS}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": row.refresh_token,
        }, timeout=20,
    )
    data = resp.json() if resp.status_code < 500 else {}
    access = data.get("access_token")
    if not access:
        raise RuntimeError(f"Refresh failed: {data}")
    expires_in = int(data.get("expires_in") or 3600)
    row.access_token = access
    row.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    row.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return access


def _get_access_token() -> str:
    row = _get_or_create_row()
    if not row.access_token:
        raise RuntimeError("Not connected to Zoho")
    if not row.expires_at or datetime.now(timezone.utc) >= (row.expires_at - timedelta(seconds=60)):
        return _refresh_access_token(row)
    return row.access_token


def _api(endpoint: str, method: str = "GET", body: Optional[dict] = None,
         extra_headers: Optional[dict] = None) -> dict:
    token = _get_access_token()
    headers = {
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.request(
        method, f"{ZOHO_API}{endpoint}",
        headers=headers, json=body, timeout=30,
    )
    if resp.status_code == 204:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def is_connected() -> bool:
    row = db.session.get(ZohoToken, 1)
    return bool(row and row.refresh_token)


def get_connection_info() -> dict:
    row = db.session.get(ZohoToken, 1)
    if not row:
        return {"connected": False}
    return row.to_dict()


# ── Data fetchers ─────────────────────────────────────────────────


def get_leads(page: int = 1, per_page: int = 200) -> dict:
    return _api(f"/Leads?page={page}&per_page={per_page}")


def get_deals(page: int = 1, per_page: int = 200) -> dict:
    return _api(f"/Deals?page={page}&per_page={per_page}")


def get_deal(deal_id: str) -> dict:
    return _api(f"/Deals/{deal_id}")


def get_contacts(page: int = 1, per_page: int = 200) -> dict:
    return _api(f"/Contacts?page={page}&per_page={per_page}")


def get_users() -> dict:
    return _api("/users?type=AllUsers")


def search_leads(criteria: str) -> dict:
    from urllib.parse import quote
    return _api(f"/Leads/search?criteria={quote(criteria)}")


def get_modified_leads(since_date: str) -> dict:
    return _api(
        "/Leads?sort_by=Modified_Time&sort_order=desc&per_page=200",
        extra_headers={"If-Modified-Since": since_date},
    )


def get_modified_deals(since_date: str) -> dict:
    return _api(
        "/Deals?sort_by=Modified_Time&sort_order=desc&per_page=200",
        extra_headers={"If-Modified-Since": since_date},
    )


def get_all_deals() -> list:
    """Pull todos los deals con paginación."""
    all_deals = []
    page = 1
    while True:
        result = get_deals(page=page, per_page=200)
        data = result.get("data") or []
        if not data:
            break
        all_deals.extend(data)
        info = result.get("info") or {}
        if not info.get("more_records"):
            break
        page += 1
    return all_deals
