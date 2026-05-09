"""
SCIP Google Ads service. Port de scip/google-ads.service.js (~277 líneas).

Usa SDK google-ads (Python) — equivalente a google-ads-api Node v23.
Auth: OAuth refresh_token + developer_token + login_customer_id.
Todas las funciones públicas devuelven {success, data, error?} para
que callers nunca tiren excepción.

Env vars:
  GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN
  GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID
"""
import logging
import os
import re
import time
from typing import Any, Optional

log = logging.getLogger("scip_google")

CACHE_TTL = int(os.getenv("CACHE_TTL", "300"))

CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
REFRESH_TOKEN = os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
DEVELOPER_TOKEN = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")
LOGIN_CUSTOMER_ID = re.sub(r"\D", "", os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))

# Status enum mapping (igual que el legacy: SDK devuelve int)
CAMPAIGN_STATUS_MAP = {0: "UNSPECIFIED", 1: "UNKNOWN", 2: "ENABLED", 3: "PAUSED", 4: "REMOVED"}


def _strip_dashes(cid: Any) -> str:
    return re.sub(r"\D", "", str(cid or ""))


def _normalize_status(raw) -> str:
    if isinstance(raw, int):
        return CAMPAIGN_STATUS_MAP.get(raw, str(raw))
    if isinstance(raw, str) and raw.isdigit():
        return CAMPAIGN_STATUS_MAP.get(int(raw), raw)
    return str(raw) if raw is not None else "UNKNOWN"


# ── Cache simple ──────────────────────────────────────────────────


class _Cache:
    def __init__(self, ttl: int = CACHE_TTL):
        self._d: dict = {}
        self._ttl = ttl

    def get(self, k: str):
        v = self._d.get(k)
        if not v:
            return None
        val, exp = v
        if time.time() > exp:
            del self._d[k]
            return None
        return val

    def set(self, k: str, v, ttl: Optional[int] = None):
        self._d[k] = (v, time.time() + (ttl or self._ttl))

    def flush(self):
        self._d.clear()


_cache = _Cache()
_client = None


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN
                and DEVELOPER_TOKEN and LOGIN_CUSTOMER_ID)


def _get_client():
    """Lazy init del SDK (no romper el boot si no está la dep)."""
    global _client
    if _client is not None:
        return _client
    if not is_configured():
        raise RuntimeError("Google Ads credentials incompletas (ver env vars)")
    try:
        from google.ads.googleads.client import GoogleAdsClient
    except ImportError as e:
        raise RuntimeError(f"google-ads SDK no instalado: {e}")
    config = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "login_customer_id": LOGIN_CUSTOMER_ID,
        "use_proto_plus": True,
    }
    _client = GoogleAdsClient.load_from_dict(config)
    return _client


def _ok(data) -> dict:
    return {"success": True, "data": data}


def _err(msg: str) -> dict:
    return {"success": False, "error": msg, "data": None}


# ── Public API ─────────────────────────────────────────────────────


def list_accessible_customers() -> dict:
    """GET /listAccessibleCustomers — lista de customer IDs que el refresh
    token puede acceder. Útil para diagnóstico."""
    if not is_configured():
        return _err("not_configured")
    try:
        client = _get_client()
        svc = client.get_service("CustomerService")
        result = svc.list_accessible_customers()
        ids = [_strip_dashes(rn.split("/")[-1]) for rn in result.resource_names]
        return _ok({"customer_ids": ids})
    except Exception as e:
        log.error(f"list_accessible_customers: {e}")
        return _err(str(e))


def get_campaigns(customer_id: Optional[str] = None) -> dict:
    """Lista campañas con métricas básicas. customer_id default = LOGIN_CUSTOMER_ID."""
    cid = _strip_dashes(customer_id) or LOGIN_CUSTOMER_ID
    cache_key = f"campaigns_{cid}"
    cached = _cache.get(cache_key)
    if cached:
        return _ok(cached)
    if not is_configured():
        return _err("not_configured")
    try:
        client = _get_client()
        ga = client.get_service("GoogleAdsService")
        query = """
            SELECT
                campaign.id, campaign.name, campaign.status,
                campaign.advertising_channel_type, campaign.bidding_strategy_type,
                campaign_budget.amount_micros,
                metrics.impressions, metrics.clicks, metrics.cost_micros,
                metrics.ctr, metrics.average_cpc, metrics.conversions,
                metrics.cost_per_conversion
            FROM campaign
            WHERE segments.date DURING LAST_30_DAYS
            ORDER BY metrics.cost_micros DESC
        """
        response = ga.search(customer_id=cid, query=query)
        campaigns = []
        for row in response:
            campaigns.append({
                "id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": _normalize_status(row.campaign.status),
                "channel_type": str(row.campaign.advertising_channel_type),
                "bidding_strategy": str(row.campaign.bidding_strategy_type),
                "budget_amount_mxn": (row.campaign_budget.amount_micros or 0) / 1_000_000,
                "metrics": {
                    "impressions": int(row.metrics.impressions or 0),
                    "clicks": int(row.metrics.clicks or 0),
                    "spend": (row.metrics.cost_micros or 0) / 1_000_000,
                    "ctr": float(row.metrics.ctr or 0),
                    "cpc": (row.metrics.average_cpc or 0) / 1_000_000,
                    "conversions": float(row.metrics.conversions or 0),
                    "cost_per_conversion": (row.metrics.cost_per_conversion or 0) / 1_000_000,
                },
            })
        _cache.set(cache_key, campaigns)
        return _ok(campaigns)
    except Exception as e:
        log.error(f"get_campaigns: {e}")
        return _err(str(e))


def get_campaign(customer_id: str, campaign_id: str) -> dict:
    """Detalle de 1 campaña (filtra get_campaigns y devuelve la matched)."""
    cid = _strip_dashes(customer_id) or LOGIN_CUSTOMER_ID
    res = get_campaigns(cid)
    if not res["success"]:
        return res
    match = next((c for c in res["data"] if c["id"] == str(campaign_id)), None)
    if not match:
        return _err("campaign_not_found")
    return _ok(match)


def get_metrics(customer_id: Optional[str] = None, days: int = 30) -> dict:
    """Métricas agregadas account-level."""
    cid = _strip_dashes(customer_id) or LOGIN_CUSTOMER_ID
    if not is_configured():
        return _err("not_configured")
    days = max(1, min(int(days), 365))
    period = f"LAST_{days}_DAYS" if days in (7, 14, 30, 90, 180, 365) else f"LAST_30_DAYS"
    try:
        client = _get_client()
        ga = client.get_service("GoogleAdsService")
        query = f"""
            SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions,
                   metrics.cost_per_conversion
            FROM customer
            WHERE segments.date DURING {period}
        """
        response = ga.search(customer_id=cid, query=query)
        agg = {"impressions": 0, "clicks": 0, "spend": 0.0, "conversions": 0,
               "ctr": 0.0, "cpc": 0.0, "cost_per_conversion": 0.0}
        n = 0
        for row in response:
            agg["impressions"] += int(row.metrics.impressions or 0)
            agg["clicks"] += int(row.metrics.clicks or 0)
            agg["spend"] += (row.metrics.cost_micros or 0) / 1_000_000
            agg["conversions"] += float(row.metrics.conversions or 0)
            agg["ctr"] += float(row.metrics.ctr or 0)
            agg["cpc"] += (row.metrics.average_cpc or 0) / 1_000_000
            agg["cost_per_conversion"] += (row.metrics.cost_per_conversion or 0) / 1_000_000
            n += 1
        if n > 1:
            agg["ctr"] /= n
            agg["cpc"] /= n
            agg["cost_per_conversion"] /= n
        return _ok({"period": period, "totals": agg})
    except Exception as e:
        log.error(f"get_metrics: {e}")
        return _err(str(e))


def get_ad_groups(customer_id: str, campaign_id: str) -> dict:
    cid = _strip_dashes(customer_id) or LOGIN_CUSTOMER_ID
    if not is_configured():
        return _err("not_configured")
    try:
        client = _get_client()
        ga = client.get_service("GoogleAdsService")
        query = f"""
            SELECT ad_group.id, ad_group.name, ad_group.status,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions
            FROM ad_group
            WHERE campaign.id = {campaign_id} AND segments.date DURING LAST_30_DAYS
            ORDER BY metrics.cost_micros DESC
        """
        response = ga.search(customer_id=cid, query=query)
        out = []
        for row in response:
            out.append({
                "id": str(row.ad_group.id),
                "name": row.ad_group.name,
                "status": _normalize_status(row.ad_group.status),
                "metrics": {
                    "impressions": int(row.metrics.impressions or 0),
                    "clicks": int(row.metrics.clicks or 0),
                    "spend": (row.metrics.cost_micros or 0) / 1_000_000,
                    "conversions": float(row.metrics.conversions or 0),
                },
            })
        return _ok(out)
    except Exception as e:
        log.error(f"get_ad_groups: {e}")
        return _err(str(e))


def get_daily_metrics(customer_id: Optional[str] = None, days: int = 30) -> dict:
    """Métricas por día. Para gráficos de tendencias."""
    cid = _strip_dashes(customer_id) or LOGIN_CUSTOMER_ID
    if not is_configured():
        return _err("not_configured")
    days = max(1, min(int(days), 365))
    period = f"LAST_{days}_DAYS" if days in (7, 14, 30, 90, 180, 365) else "LAST_30_DAYS"
    try:
        client = _get_client()
        ga = client.get_service("GoogleAdsService")
        query = f"""
            SELECT segments.date, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM customer
            WHERE segments.date DURING {period}
            ORDER BY segments.date ASC
        """
        response = ga.search(customer_id=cid, query=query)
        out = []
        for row in response:
            out.append({
                "date": str(row.segments.date),
                "impressions": int(row.metrics.impressions or 0),
                "clicks": int(row.metrics.clicks or 0),
                "spend": (row.metrics.cost_micros or 0) / 1_000_000,
                "conversions": float(row.metrics.conversions or 0),
            })
        return _ok(out)
    except Exception as e:
        log.error(f"get_daily_metrics: {e}")
        return _err(str(e))


def health() -> dict:
    return {
        "configured": is_configured(),
        "login_customer_id_set": bool(LOGIN_CUSTOMER_ID),
        "developer_token_set": bool(DEVELOPER_TOKEN),
        "cached_keys": len(_cache._d),
    }


def flush_cache() -> dict:
    n = len(_cache._d)
    _cache.flush()
    return {"flushed": n}
