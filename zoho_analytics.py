"""
Zoho Analytics API integration — fetch citas data via bulk async export.

Env vars:
  ZOHO_ANALYTICS_CLIENT_ID      — OAuth2 Client ID (Self Client de Analytics)
  ZOHO_ANALYTICS_CLIENT_SECRET  — OAuth2 Client Secret
  ZOHO_ANALYTICS_REFRESH_TOKEN  — Refresh token con scope ZohoAnalytics.data.read
  ZOHO_ANALYTICS_ORG_ID         — Organization ID en Zoho Analytics
  ZOHO_ANALYTICS_WORKSPACE_ID   — Workspace ID (ej. "2756245000000005001")
  ZOHO_ANALYTICS_VIEW_ID        — View/tabla ID (ej. "2756245000000027162")
"""
import csv
import io
import json
import logging
import os
import time

import requests

log = logging.getLogger("zoho_analytics")

CLIENT_ID = os.getenv("ZOHO_ANALYTICS_CLIENT_ID") or os.getenv("ZOHO_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("ZOHO_ANALYTICS_CLIENT_SECRET") or os.getenv("ZOHO_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("ZOHO_ANALYTICS_REFRESH_TOKEN", "")
ORG_ID = os.getenv("ZOHO_ANALYTICS_ORG_ID", "")
WORKSPACE_ID = os.getenv("ZOHO_ANALYTICS_WORKSPACE_ID", "")
VIEW_ID = os.getenv("ZOHO_ANALYTICS_VIEW_ID", "")

ACCOUNTS_URL = "https://accounts.zoho.com"
API_BASE = "https://analyticsapi.zoho.com/restapi/v2"

_access_token = ""


def _refresh_access_token():
    global _access_token
    resp = requests.post(
        f"{ACCOUNTS_URL}/oauth/v2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
        },
        timeout=15,
    )
    if not resp.ok:
        log.error("Zoho token refresh failed: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"Zoho token refresh: HTTP {resp.status_code}")
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"No access_token in response: {data}")
    _access_token = data["access_token"]
    return _access_token


def _headers():
    return {
        "Authorization": f"Zoho-oauthtoken {_access_token}",
        "ZANALYTICS-ORGID": ORG_ID,
    }


def _ensure_token():
    if not _access_token:
        _refresh_access_token()


def _api_get(url, params=None, retry=True, timeout=30):
    _ensure_token()
    resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    if resp.status_code == 401 and retry:
        _refresh_access_token()
        resp = requests.get(url, headers=_headers(), params=params, timeout=timeout)
    return resp


def is_configured():
    return all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN, ORG_ID, WORKSPACE_ID, VIEW_ID])


def get_workspaces():
    resp = _api_get(f"{API_BASE}/workspaces")
    if not resp.ok:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    return resp.json()


def get_views(workspace_id):
    resp = _api_get(f"{API_BASE}/workspaces/{workspace_id}/views")
    if not resp.ok:
        return {"error": f"HTTP {resp.status_code}: {resp.text[:300]}"}
    return resp.json()


def fetch_citas(criteria=None):
    """Fetch citas via async bulk export (CSV) → parse → return list of dicts.

    Args:
        criteria: optional Zoho criteria string to filter rows,
                  e.g. '"Fecha de Inicio" >= \'01/01/2025\''

    Returns {"rows": [...], "count": N} or {"error": "..."}.
    """
    if not is_configured():
        return {"error": "Zoho Analytics no configurado — faltan variables de entorno"}

    config = {"responseFormat": "csv"}
    if criteria:
        config["criteria"] = criteria

    # 1) Create bulk export job
    resp = _api_get(
        f"{API_BASE}/bulk/workspaces/{WORKSPACE_ID}/views/{VIEW_ID}/data",
        params={"CONFIG": json.dumps(config)},
    )
    if not resp.ok:
        return {"error": f"Crear export job: HTTP {resp.status_code} — {resp.text[:300]}"}

    data = resp.json()
    job_id = data.get("data", {}).get("jobId")
    if not job_id:
        return {"error": f"No jobId in response: {data}"}

    # 2) Poll until job completes (max ~2 min)
    job_url = f"{API_BASE}/bulk/workspaces/{WORKSPACE_ID}/exportjobs/{job_id}"
    for _ in range(24):
        time.sleep(5)
        poll = _api_get(job_url)
        if not poll.ok:
            return {"error": f"Poll job: HTTP {poll.status_code}"}
        status = poll.json().get("data", {}).get("jobStatus", "")
        if status == "JOB COMPLETED":
            break
        if "FAIL" in status or "ERROR" in status:
            return {"error": f"Export job failed: {status}"}
    else:
        return {"error": "Export job timeout (2 min)"}

    # 3) Download CSV
    download_url = f"{job_url}/data"
    dl = _api_get(download_url, timeout=120)
    if not dl.ok:
        return {"error": f"Download: HTTP {dl.status_code}"}

    # 4) Parse CSV into list of dicts
    text = dl.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    log.info("Zoho Analytics: %d citas fetched (job %s)", len(rows), job_id)
    return {"rows": rows, "count": len(rows)}
