"""
SDR Prospector — port directo de vendedores.cloud/sdr.js.

Búsqueda de prospectos en Meta Ad Library + Google Places + Instagram + WhatsApp,
con clasificación local vs corporate y templates de WhatsApp por unidad de negocio.

Devuelve dicts crudos. La persistencia (SdrResult, Lead) la hace blueprints/sdr.py.
Cost tracking (Google Places ~$0.032/call) está stubbed hasta que portemos api_costs.py.

Env vars requeridas:
  GOOGLE_PLACES_API_KEY (requerido para Maps + countBranches)
  META_ACCESS_TOKEN (opcional; sin él cae a scrape público de Meta)
"""

import logging
import os
import re
import urllib.parse
from typing import Any, Optional

import requests

log = logging.getLogger("sdr_prospector")

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# ── WhatsApp message templates por unidad ─────────────────────────


TEMPLATES = {
    "aromatex": {
        "subject": "Marketing Olfativo",
        "message": (
            "Hola! Soy de Aromatex, parte de Grupo Avantex. Vi que {name} "
            "tiene una gran presencia y nos encantaria ayudarles a "
            "diferenciarse con marketing olfativo.\n\n"
            "Nuestros clientes incrementan hasta 20% el tiempo de "
            "permanencia de sus clientes con aromas personalizados.\n\n"
            "Me encantaria platicarte como funciona. Tienes 5 minutos esta semana?"
        ),
    },
    "pestex": {
        "subject": "Control de Plagas Profesional",
        "message": (
            "Hola! Soy de Pestex, parte de Grupo Avantex. Nos especializamos "
            "en control de plagas para negocios como {name}.\n\n"
            "Contamos con certificaciones COEPRIS y garantizamos cumplimiento "
            "sanitario al 100%. Protegemos mas de 500 negocios en Mexico.\n\n"
            "Le gustaria una evaluacion gratuita de su negocio?"
        ),
    },
    "weldex": {
        "subject": "Servicios de Limpieza Profesional",
        "message": (
            "Hola! Soy de Weldex, parte de Grupo Avantex. Ofrecemos servicios "
            "de limpieza e intendencia profesional para negocios como {name}.\n\n"
            "Nuestros clientes reducen hasta 30% sus costos vs personal propio, "
            "con imagen profesional garantizada.\n\n"
            "Podemos agendar una visita para cotizarle sin compromiso?"
        ),
    },
}


def get_templates() -> dict:
    return TEMPLATES


def generate_whatsapp_link(name: str, phone: str, unit: str) -> str:
    template = TEMPLATES.get(unit) or TEMPLATES["aromatex"]
    msg = template["message"].format(name=name)
    norm = phone if phone.startswith("52") else "52" + phone
    return f"https://wa.me/{norm}?text={urllib.parse.quote(msg)}"


# ── Corporate blacklist ────────────────────────────────────────────


CORPORATE_BLACKLIST = (
    "walmart", "bodega aurrera", "oxxo", "7-eleven", "7 eleven",
    "cinepolis", "cinemex", "liverpool", "palacio de hierro", "sears",
    "starbucks", "mcdonalds", "mcdonald", "burger king", "wendys",
    "wendy", "subway", "dominos", "domino", "kfc", "pizza hut",
    "little caesars", "little caesar", "soriana", "chedraui", "heb",
    "h-e-b", "costco", "sams club", "sam's club", "home depot", "lowes",
    "lowe's", "office depot", "autozone", "coppel", "elektra", "telcel",
    "at&t", "bancomer", "bbva", "banamex", "citibanamex", "banorte",
    "hsbc", "santander", "farmacias guadalajara", "farmacia guadalajara",
    "farmacias del ahorro", "farmacia del ahorro", "farmacias benavides",
    "farmacia benavides", "sanborns", "femsa", "coca cola", "coca-cola",
    "pepsi", "pepsico", "bimbo", "cemex", "ampm", "am pm", "circle k",
    "alsea", "grupo modelo", "heineken", "walmart supercenter",
    "superama", "la comer", "mega soriana",
)

MAX_REVIEWS_LOCAL = 5000
MAX_BRANCHES_LOCAL = 10
MAX_IG_FOLLOWERS_LOCAL = 50000


def is_corporate(name: str) -> bool:
    if not name:
        return False
    lower = name.lower().strip()
    return any(brand in lower for brand in CORPORATE_BLACKLIST)


def classify_corporate(prospect: dict) -> Optional[str]:
    """Devuelve la razón si es corporate, o None si es local."""
    if is_corporate(prospect.get("name", "")):
        return "blacklist"
    if (prospect.get("reviews") or 0) > MAX_REVIEWS_LOCAL:
        return "reviews"
    if (prospect.get("branches") or 1) > MAX_BRANCHES_LOCAL:
        return "branches"
    if (prospect.get("ig_followers") or 0) > MAX_IG_FOLLOWERS_LOCAL:
        return "followers"
    return None


# ── Helpers ────────────────────────────────────────────────────────


def normalize_phone(num: Optional[str]) -> str:
    if not num:
        return ""
    digits = re.sub(r"\D", "", num)
    if len(digits) < 10:
        return ""
    if digits.startswith("521") and len(digits) == 13:
        return digits
    if digits.startswith("52") and len(digits) == 12:
        return digits
    if len(digits) == 10:
        return "52" + digits
    return "52" + digits[-10:]


def _fetch_text(url: str, timeout: float = 6.0, headers: Optional[dict] = None) -> str:
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": UA, "Accept": "text/html", **(headers or {})},
            timeout=timeout,
            allow_redirects=True,
        )
        return resp.text or ""
    except Exception as e:
        log.debug(f"fetch_text({url[:80]}...) failed: {e}")
        return ""


def extract_wa_from_html(html: str) -> str:
    if not html:
        return ""
    patterns = (
        re.compile(r"wa\.me/(\d{10,15})", re.I),
        re.compile(r"api\.whatsapp\.com/send\?phone=(\d{10,15})", re.I),
    )
    for p in patterns:
        m = p.search(html)
        if m and len(m.group(1)) >= 10:
            return normalize_phone(m.group(1))
    return ""


def extract_wa_from_website(url: str) -> str:
    if not url:
        return ""
    return extract_wa_from_html(_fetch_text(url, timeout=5.0))


# ── Meta Ad Library search ────────────────────────────────────────


def search_meta_ads(state: str, giro: str, limit: int) -> list[dict]:
    """Intenta API oficial → scrape público → Google fallback."""
    results: list[dict] = []

    # Strategy 1: Meta Ads API oficial
    if META_ACCESS_TOKEN:
        try:
            q = urllib.parse.quote(f"{giro} {state} Mexico")
            url = (
                f"https://graph.facebook.com/v19.0/ads_archive?"
                f"search_terms={q}&ad_reached_countries=MX&ad_active_status=ACTIVE&"
                f"limit={min(limit, 25)}&access_token={META_ACCESS_TOKEN}&"
                "fields=page_id,page_name,ad_snapshot_url,spend"
            )
            resp = requests.get(url, timeout=10)
            data = resp.json() if resp.status_code < 400 else {}
            for ad in data.get("data") or []:
                pid = ad.get("page_id") or ""
                results.append({
                    "name": ad.get("page_name") or "",
                    "page_id": pid,
                    "facebook_url": f"https://facebook.com/{pid}" if pid else "",
                    "instagram_handle": "",
                    "meta_ad_url": ad.get("ad_snapshot_url") or "",
                    "ad_spend": ad.get("spend"),
                    "source": "meta_api",
                })
            if results:
                return results
        except Exception as e:
            log.warning(f"Meta API error: {e}")

    # Strategy 2: scrape Meta Ad Library page
    try:
        q = urllib.parse.quote(f"{giro} {state}")
        url = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=MX&q={q}"
        html = _fetch_text(url, timeout=8.0, headers={"Accept-Language": "es-MX,es;q=0.9"})
        names = re.findall(r'"page_name":"([^"]+)"', html)
        ids = re.findall(r'"page_id":"(\d+)"', html)
        for i in range(min(len(names), limit)):
            pid = ids[i] if i < len(ids) else ""
            results.append({
                "name": names[i],
                "page_id": pid,
                "facebook_url": f"https://facebook.com/{pid}" if pid else "",
                "instagram_handle": "",
                "meta_ad_url": (
                    f"https://www.facebook.com/ads/library/?active_status=active&"
                    f"ad_type=all&country=MX&view_all_page_id={pid}"
                ) if pid else "",
                "ad_spend": None,
                "source": "meta_scrape",
            })
        if results:
            return results
    except Exception as e:
        log.debug(f"Meta scrape failed: {e}")

    # Strategy 3: Google search fallback
    try:
        q = urllib.parse.quote(f'site:facebook.com/ads/library "{giro}" "{state}"')
        url = f"https://www.google.com/search?q={q}&num={min(limit, 10)}&hl=es"
        html = _fetch_text(url, timeout=8.0)
        for m in re.finditer(r"<h3[^>]*>([^<]+)</h3>", html, re.I):
            if len(results) >= limit:
                break
            name = re.sub(
                r"Ads about.*|Facebook.*|Meta.*|Ad Library.*", "", m.group(1), flags=re.I
            ).strip()
            if name and 2 < len(name) < 80:
                results.append({
                    "name": name, "page_id": "", "facebook_url": "",
                    "instagram_handle": "", "meta_ad_url": "",
                    "ad_spend": None, "source": "meta_google",
                })
    except Exception as e:
        log.debug(f"Google fallback failed: {e}")

    return results


# ── Instagram + WhatsApp lookup (Aromatex flow) ────────────────────


def find_instagram_whatsapp(prospect: dict) -> dict:
    name = re.sub(r"[^a-zA-Z0-9\s]", "", prospect.get("name", "")).strip()
    if not name:
        return {**prospect, "whatsapp": "", "wa_source": "", "ig_followers": 0}

    ig_followers = 0

    # Strategy 1: Facebook page → IG handle + WA
    if prospect.get("facebook_url"):
        html = _fetch_text(prospect["facebook_url"], timeout=6.0)
        if html:
            m = re.search(r"instagram\.com/([a-zA-Z0-9._]+)", html, re.I)
            if m and m.group(1) not in ("p", "reel", "stories", "explore"):
                prospect["instagram_handle"] = m.group(1)
            wa = extract_wa_from_html(html)
            if wa:
                return {**prospect, "whatsapp": wa, "wa_source": "facebook", "ig_followers": ig_followers}

    # Strategy 2: Google → IG profile + follower count
    try:
        q = urllib.parse.quote(f'site:instagram.com "{name}"')
        html = _fetch_text(f"https://www.google.com/search?q={q}&num=3&hl=es", timeout=6.0)
        m = re.search(r"instagram\.com/([a-zA-Z0-9._]{2,30})", html, re.I)
        if m and m.group(1) not in ("p", "reel", "stories", "explore", "accounts"):
            prospect["instagram_handle"] = prospect.get("instagram_handle") or m.group(1)
        fm = re.search(r"([\d,.]+[KkMm]?)\s*(?:seguidores|followers)", html, re.I)
        if fm:
            f = fm.group(1).replace(",", "")
            try:
                if f.endswith(("K", "k")):
                    ig_followers = int(float(f[:-1]) * 1000)
                elif f.endswith(("M", "m")):
                    ig_followers = int(float(f[:-1]) * 1000000)
                else:
                    ig_followers = int(f)
            except ValueError:
                ig_followers = 0
    except Exception:
        pass

    # Strategy 3: IG profile → WA in bio or linktree
    handle = prospect.get("instagram_handle")
    if handle:
        html = _fetch_text(f"https://www.instagram.com/{handle}/", timeout=6.0)
        if html:
            wa = extract_wa_from_html(html)
            if wa:
                return {**prospect, "whatsapp": wa, "wa_source": "instagram", "ig_followers": ig_followers}
            lm = re.search(r"linktr\.ee/([a-zA-Z0-9._-]+)", html, re.I)
            if lm:
                bio_html = _fetch_text(f"https://linktr.ee/{lm.group(1)}", timeout=4.0)
                wa2 = extract_wa_from_html(bio_html)
                if wa2:
                    return {**prospect, "whatsapp": wa2, "wa_source": "instagram_bio", "ig_followers": ig_followers}

    return {**prospect, "whatsapp": "", "wa_source": "", "ig_followers": ig_followers}


# ── Google Places ──────────────────────────────────────────────────


def search_google_maps(state: str, giro: str, limit: int) -> list[dict]:
    if not GOOGLE_PLACES_API_KEY:
        log.warning("GOOGLE_PLACES_API_KEY no configurada")
        return []
    try:
        from api_costs import track_cost
        track_cost(service="google_places_api", action="places_search", cost_usd=0.032)
    except Exception:
        pass
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.displayName,places.formattedAddress,places.nationalPhoneNumber,"
            "places.internationalPhoneNumber,places.rating,places.userRatingCount,"
            "places.websiteUri,places.googleMapsUri"
        ),
    }
    body = {
        "textQuery": f"{giro} en {state} Mexico",
        "maxResultCount": min(limit, 20),
        "languageCode": "es",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=10)
        data = resp.json() if resp.status_code < 400 else {}
    except Exception as e:
        log.error(f"Google Maps error: {e}")
        return []
    out = []
    for p in data.get("places") or []:
        addr = p.get("formattedAddress") or ""
        cm = re.search(r",\s*([^,]+),\s*[A-Z]", addr)
        out.append({
            "name": (p.get("displayName") or {}).get("text") or "",
            "address": addr,
            "city": cm.group(1).strip() if cm else "",
            "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber") or "",
            "rating": p.get("rating"),
            "reviews": p.get("userRatingCount") or 0,
            "website": p.get("websiteUri") or "",
            "maps_url": p.get("googleMapsUri") or "",
        })
    return out


def count_branches(name: str, state: str) -> int:
    if not GOOGLE_PLACES_API_KEY:
        return 1
    url = "https://places.googleapis.com/v1/places:searchText"
    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": "places.displayName",
            },
            json={"textQuery": f"{name} {state}", "maxResultCount": 15, "languageCode": "es"},
            timeout=8,
        )
        data = resp.json() if resp.status_code < 400 else {}
    except Exception:
        return 1
    places = data.get("places") or []
    word = next(
        (w for w in name.lower().split() if len(w) > 3), name.lower()
    )
    count = sum(1 for p in places if word in (p.get("displayName") or {}).get("text", "").lower())
    return count or 1


# ── Main: search per unit ──────────────────────────────────────────


def search_prospects(state: str, giro: str, unit: str, limit: int = 10) -> dict[str, list[dict]]:
    log.info(f"[SDR] search: {giro} in {state} for {unit}, limit={limit}")
    if unit == "aromatex":
        return _search_aromatex(state, giro, limit)
    return _search_pestex_weldex(state, giro, unit, limit)


def _search_aromatex(state: str, giro: str, limit: int) -> dict:
    """Aromatex: requires confirmed digital presence (wa.me)."""
    results: list[dict] = []
    corporates: list[dict] = []

    meta_results = search_meta_ads(state, giro, limit * 3)
    use_fallback = not meta_results

    if not use_fallback:
        enriched: list[dict] = []
        for p in meta_results:
            if len(enriched) >= limit * 2:
                break
            r = find_instagram_whatsapp(p)
            if r:
                enriched.append(r)

        for p in enriched:
            if not p.get("whatsapp"):
                continue
            if len(results) >= limit:
                break
            branches = count_branches(p["name"], state)
            gm_results = search_google_maps(state, p["name"], 3)
            word = next((w for w in p["name"].lower().split() if len(w) > 3), "")
            gm = next((g for g in gm_results if word and word in g["name"].lower()), gm_results[0] if gm_results else None)

            prospect = {
                **p, "branches": branches,
                "address": (gm or {}).get("address") or p.get("address") or "",
                "city": (gm or {}).get("city") or "",
                "rating": (gm or {}).get("rating"),
                "reviews": (gm or {}).get("reviews") or 0,
                "website": (gm or {}).get("website") or p.get("website") or "",
                "maps_url": (gm or {}).get("maps_url") or "",
                "phone": (gm or {}).get("phone") or "",
                "wa_status": "confirmado",
            }
            if branches < 2 and (prospect["reviews"] or 0) < 100:
                continue
            reason = classify_corporate(prospect)
            if reason:
                corporates.append({**prospect, "corp_reason": reason})
                continue
            results.append(prospect)

    if use_fallback or not results:
        for gm in search_google_maps(state, giro, 20):
            if len(results) >= limit:
                break
            branches = count_branches(gm["name"], state)
            if branches < 2 and gm["reviews"] < 100:
                continue
            wa = extract_wa_from_website(gm["website"])
            if not wa:
                continue
            prospect = {
                "name": gm["name"], "address": gm["address"], "city": gm["city"],
                "phone": gm["phone"], "whatsapp": wa, "wa_source": "website",
                "wa_status": "confirmado", "rating": gm["rating"],
                "reviews": gm["reviews"], "website": gm["website"],
                "maps_url": gm["maps_url"], "branches": branches,
                "instagram_handle": "", "facebook_url": "", "meta_ad_url": "",
                "source": "google", "ad_spend": None, "ig_followers": 0,
            }
            reason = classify_corporate(prospect)
            if reason:
                corporates.append({**prospect, "corp_reason": reason})
                continue
            results.append(prospect)

    log.info(f"[SDR][aromatex] done: {len(results)} local, {len(corporates)} corp")
    return {"local": results[:limit], "corporates": corporates}


def _search_pestex_weldex(state: str, giro: str, unit: str, limit: int) -> dict:
    """Pestex/Weldex: no digital presence required; phone from Maps cuenta."""
    results: list[dict] = []
    corporates: list[dict] = []

    for gm in search_google_maps(state, giro, 20):
        if len(results) + len(corporates) >= limit * 2:
            break
        branches = count_branches(gm["name"], state)
        wa = extract_wa_from_website(gm["website"])
        wa_source = "website" if wa else ""
        wa_status = "confirmado" if wa else ""
        if not wa and gm["phone"]:
            wa = normalize_phone(gm["phone"])
            wa_source = "google_maps"
            wa_status = "telefono"
        prospect = {
            "name": gm["name"], "address": gm["address"], "city": gm["city"],
            "phone": gm["phone"], "whatsapp": wa, "wa_source": wa_source,
            "wa_status": wa_status, "rating": gm["rating"],
            "reviews": gm["reviews"], "website": gm["website"],
            "maps_url": gm["maps_url"], "branches": branches,
            "instagram_handle": "", "facebook_url": "", "meta_ad_url": "",
            "source": "google", "ad_spend": None, "ig_followers": 0,
        }
        reason = classify_corporate(prospect)
        if reason:
            corporates.append({**prospect, "corp_reason": reason})
            continue
        results.append(prospect)

    log.info(f"[SDR][{unit}] done: {len(results)} local, {len(corporates)} corp")
    return {"local": results[:limit], "corporates": corporates}
