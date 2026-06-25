#!/usr/bin/env python3
"""
Local Website Lead Generator
Discovers local businesses via OpenStreetMap, scores their websites 0-100,
maps the full tech stack, and surfaces Spruce My Site service opportunities.

Usage:
  python -m tools.lead_gen --location "Manchester, UK" --category restaurant
  python -m tools.lead_gen --location "Austin, TX" --category dentist --radius 10 --limit 30
"""

import argparse
import csv
import html as html_module
import re
import sys
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Constants ──────────────────────────────────────────────────────────────────

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
UA = "SpruceMySite-LeadGen/1.0 (local business website analyzer)"
FETCH_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# OSM tags per business category
CATEGORIES: dict[str, list[tuple[str, str]]] = {
    "restaurant":   [("amenity", "restaurant")],
    "cafe":         [("amenity", "cafe")],
    "bar":          [("amenity", "bar"), ("amenity", "pub")],
    "fast_food":    [("amenity", "fast_food")],
    "hotel":        [("tourism", "hotel"), ("tourism", "guest_house"), ("tourism", "hostel")],
    "dentist":      [("amenity", "dentist")],
    "doctor":       [("amenity", "doctors"), ("amenity", "clinic")],
    "hairdresser":  [("shop", "hairdresser"), ("shop", "beauty")],
    "beauty":       [("shop", "beauty"), ("shop", "cosmetics")],
    "gym":          [("leisure", "fitness_centre")],
    "pharmacy":     [("amenity", "pharmacy")],
    "plumber":      [("craft", "plumber")],
    "electrician":  [("craft", "electrician")],
    "lawyer":       [("office", "lawyer")],
    "accountant":   [("office", "accountant")],
    "estate_agent": [("office", "estate_agent")],
    "mechanic":     [("shop", "car_repair")],
    "florist":      [("shop", "florist")],
    "bakery":       [("shop", "bakery")],
    "optician":     [("shop", "optician")],
    "veterinary":   [("amenity", "veterinary")],
    "tattoo":       [("shop", "tattoo")],
    "travel":       [("shop", "travel_agency")],
    "insurance":    [("office", "insurance")],
    "cleaning":     [("shop", "cleaning")],
    "photographer": [("shop", "photo")],
    "butcher":      [("shop", "butcher")],
}

# ── Spruce My Site services — detected as missing opportunities ────────────────
# Each service: (key, label, description shown in report)
SPRUCE_SERVICES: list[tuple[str, str, str]] = [
    ("ssl",         "SSL/HTTPS",          "Secure your site with HTTPS"),
    ("mobile",      "Mobile Design",      "Make your site mobile-friendly"),
    ("speed",       "Speed Optimisation", "Improve page load performance"),
    ("seo",         "SEO Setup",          "Add title, meta description & H1"),
    ("schema",      "Schema Markup",      "Add structured data for Google"),
    ("analytics",   "Analytics",          "Install Google Analytics / GTM"),
    ("social",      "Social Integration", "Add social media links"),
    ("contact",     "Contact Page",       "Add a visible contact / enquiry page"),
    ("chat",        "Live Chat",          "Add a live chat widget"),
    ("cta",         "CTA / Lead Form",    "Add call-to-action or enquiry form"),
    ("maps",        "Google Maps Embed",  "Embed a map showing your location"),
    ("favicon",     "Favicon",            "Add a branded browser icon"),
    ("email_mktg",  "Email Marketing",    "Set up email list / newsletter"),
    ("modern_cms",  "Modern CMS/Design",  "Rebuild on a modern platform"),
]


# ── Data models ────────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    https:   int = 0   # /20
    speed:   int = 0   # /20
    mobile:  int = 0   # /15
    seo:     int = 0   # /25
    content: int = 0   # /20

    @property
    def total(self) -> int:
        return self.https + self.speed + self.mobile + self.seo + self.content


@dataclass
class TechStack:
    server:       str = ""
    cms:          str = ""
    framework:    str = ""
    js_lib:       str = ""
    css_fw:       str = ""
    analytics:    list = field(default_factory=list)
    ecommerce:    str = ""
    chat:         str = ""
    marketing:    str = ""
    maps:         str = ""
    payment:      str = ""
    backend:      str = ""
    cdn:          str = ""

    def badges(self) -> list[str]:
        items: list[str] = []
        for attr in ("cms", "framework", "js_lib", "css_fw", "server", "cdn", "backend"):
            v = getattr(self, attr)
            if v:
                items.append(v)
        items.extend(self.analytics)
        for attr in ("ecommerce", "chat", "marketing", "maps", "payment"):
            v = getattr(self, attr)
            if v:
                items.append(v)
        return items


@dataclass
class Business:
    name:    str
    address: str = ""
    phone:   str = ""
    website: str = ""
    lat:     float = 0.0
    lon:     float = 0.0
    osm_id:  str = ""

    score:           Optional[int]             = None
    score_breakdown: Optional[ScoreBreakdown]  = None
    tech_stack:      Optional[TechStack]       = None
    missing_services: list                     = field(default_factory=list)
    error:           str                        = ""
    response_time:   float                      = 0.0
    final_url:       str                        = ""

    @property
    def lead_grade(self) -> str:
        if not self.website:
            return "No Website"
        if self.score is None:
            return "Unreachable"
        if self.score <= 30:
            return "Prime"
        if self.score <= 50:
            return "Good"
        if self.score <= 70:
            return "Moderate"
        return "Low"


# ── Business discovery ─────────────────────────────────────────────────────────

def geocode(location: str) -> tuple[float, float]:
    r = requests.get(
        NOMINATIM_URL,
        params={"q": location, "format": "json", "limit": 1},
        headers={"User-Agent": UA},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"Cannot geocode {location!r} — try a more specific place name")
    return float(data[0]["lat"]), float(data[0]["lon"])


def _overpass_query(lat: float, lon: float, radius_m: int,
                    tags: list[tuple[str, str]], limit: int) -> str:
    lines: list[str] = []
    for key, val in tags:
        for etype in ("node", "way", "relation"):
            lines.append(f'  {etype}["{key}"="{val}"](around:{radius_m},{lat:.6f},{lon:.6f});')
    body = "\n".join(lines)
    return f"[out:json][timeout:90];\n(\n{body}\n);\nout center {limit};"


def fetch_businesses(location: str, category: str,
                     radius_km: float, limit: int) -> list[Business]:
    print(f"  Geocoding {location!r} …")
    lat, lon = geocode(location)
    print(f"  Centre: {lat:.4f}, {lon:.4f}")

    cat_key = category.lower().replace(" ", "_")
    tags = CATEGORIES.get(cat_key, [("amenity", cat_key)])

    query = _overpass_query(lat, lon, int(radius_km * 1000), tags, limit)
    print(f"  Querying Overpass API for {category!r} within {radius_km} km …")
    r = requests.post(OVERPASS_URL, data={"data": query}, timeout=120)
    r.raise_for_status()
    elements = r.json().get("elements", [])
    print(f"  Raw OSM elements: {len(elements)}")

    businesses: list[Business] = []
    seen: set[str] = set()

    for el in elements:
        t = el.get("tags", {})
        name = t.get("name", "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        el_lat = el.get("lat") or el.get("center", {}).get("lat") or lat
        el_lon = el.get("lon") or el.get("center", {}).get("lon") or lon

        addr = ", ".join(filter(None, [
            t.get("addr:housenumber", ""),
            t.get("addr:street", ""),
            t.get("addr:city", ""),
            t.get("addr:postcode", ""),
        ]))

        website = (t.get("website") or t.get("contact:website") or t.get("url") or "").strip()
        if website and not website.startswith(("http://", "https://")):
            website = "https://" + website

        phone = (t.get("phone") or t.get("contact:phone") or "").strip()

        businesses.append(Business(
            name=name, address=addr, phone=phone, website=website,
            lat=float(el_lat), lon=float(el_lon), osm_id=str(el.get("id", "")),
        ))

    return businesses


# ── Website fetching ───────────────────────────────────────────────────────────

def _fetch(url: str, timeout: int) -> tuple[Optional[requests.Response], float, str]:
    """Returns (response, elapsed, error_note)."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        t0 = time.perf_counter()
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=timeout,
                            allow_redirects=True, verify=True)
        return resp, time.perf_counter() - t0, ""
    except requests.exceptions.SSLError:
        try:
            t0 = time.perf_counter()
            resp = requests.get(url, headers=FETCH_HEADERS, timeout=timeout,
                                allow_redirects=True, verify=False)
            return resp, time.perf_counter() - t0, "SSL_ERROR"
        except Exception as e:
            return None, 0.0, f"SSL failed: {str(e)[:60]}"
    except requests.exceptions.ConnectionError:
        if url.startswith("https://"):
            try:
                t0 = time.perf_counter()
                resp = requests.get(url.replace("https://", "http://", 1),
                                    headers=FETCH_HEADERS, timeout=timeout, allow_redirects=True)
                return resp, time.perf_counter() - t0, "NO_HTTPS"
            except Exception:
                pass
        return None, 0.0, "Connection failed"
    except requests.exceptions.Timeout:
        return None, float(timeout), "TIMEOUT"
    except Exception as e:
        return None, 0.0, str(e)[:60]


# ── Scoring ────────────────────────────────────────────────────────────────────

def _score(soup: BeautifulSoup, resp: requests.Response,
           elapsed: float, err_note: str) -> ScoreBreakdown:
    bd = ScoreBreakdown()
    final = resp.url

    # HTTPS /20
    uses_https = final.startswith("https://")
    if uses_https and not err_note:
        bd.https = 20
    elif uses_https and err_note == "SSL_ERROR":
        bd.https = 5

    # Speed /20
    bd.speed = (20 if elapsed < 1.5 else
                15 if elapsed < 3.0 else
                8  if elapsed < 6.0 else 0)

    # Mobile /15
    viewport = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    bd.mobile = 15 if viewport else 0

    # SEO /25 (title 5 + meta-desc 5 + h1 5 + schema 5 + canonical 5)
    seo = 0
    title = soup.find("title")
    if title and title.get_text(strip=True):
        seo += 5
    mdesc = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if mdesc and mdesc.get("content", "").strip():
        seo += 5
    if soup.find("h1"):
        seo += 5
    if soup.find("script", attrs={"type": "application/ld+json"}):
        seo += 5
    if soup.find("link", attrs={"rel": re.compile(r"canonical", re.I)}):
        seo += 5
    bd.seo = seo

    # Content /20 (phone 5 + contact link 5 + social 5 + favicon 5)
    body_text = soup.get_text()
    html_str = str(soup).lower()
    cnt = 0
    if re.search(r'[\+\(]?[\d\s\-\.]{9,}', body_text):
        cnt += 5
    contact_a = soup.find("a", href=re.compile(r'contact|get.in.touch|reach.us', re.I))
    if not contact_a:
        contact_a = soup.find("a", string=re.compile(r'contact|get in touch', re.I))
    if contact_a:
        cnt += 5
    if any(d in html_str for d in ("facebook.com", "instagram.com", "twitter.com",
                                    "linkedin.com", "youtube.com", "tiktok.com", "x.com")):
        cnt += 5
    fav = soup.find("link", rel=lambda r: r and any(
        "icon" in (x.lower() if isinstance(x, str) else "") for x in (r if isinstance(r, list) else [r])
    ))
    if fav:
        cnt += 5
    bd.content = cnt

    return bd


# ── Tech-stack detection ───────────────────────────────────────────────────────

def _detect_stack(html: str, resp_headers: dict, url: str) -> TechStack:
    ts = TechStack()
    lo = html.lower()
    h = {k.lower(): v.lower() for k, v in resp_headers.items()}

    # Server
    srv = h.get("server", "")
    ts.server = (
        "Cloudflare"    if "cloudflare"  in srv else
        "Nginx"         if "nginx"       in srv else
        "Apache"        if "apache"      in srv else
        "LiteSpeed"     if "litespeed"   in srv else
        "Microsoft IIS" if "iis"         in srv else
        "OpenResty"     if "openresty"   in srv else ""
    )

    # CDN
    if "cf-ray" in h or "cloudflare" in srv:
        ts.cdn = "Cloudflare"
    elif "x-amz-cf-id" in h:
        ts.cdn = "AWS CloudFront"
    elif "x-fastly-request-id" in h:
        ts.cdn = "Fastly"

    # CMS
    if "/wp-content/" in html or "/wp-includes/" in html:
        ts.cms = "WordPress"
    elif "wixstatic.com" in html or "wix.com/_api" in html:
        ts.cms = "Wix"
    elif "squarespace.com" in html or 'generator" content="squarespace' in lo:
        ts.cms = "Squarespace"
    elif "cdn.shopify.com" in html or "shopify.com/s/files" in html:
        ts.cms = "Shopify"
    elif "data-wf-page" in html or "webflow.io" in html:
        ts.cms = "Webflow"
    elif 'content="drupal' in lo or "/sites/default/files/" in html:
        ts.cms = "Drupal"
    elif 'content="joomla' in lo:
        ts.cms = "Joomla"
    elif "weebly.com" in html or "weeblysite.com" in html:
        ts.cms = "Weebly"
    elif "ghost.org" in html or 'content="ghost' in lo:
        ts.cms = "Ghost"
    elif "jimdo.com" in html:
        ts.cms = "Jimdo"
    elif "sites.google.com" in url or "ghs.googlehosted.com" in url:
        ts.cms = "Google Sites"

    # Frontend framework
    if "_next/" in html or "__NEXT_DATA__" in html:
        ts.framework = "Next.js"
    elif "_nuxt/" in html or "__nuxt" in html:
        ts.framework = "Nuxt.js"
    elif "data-reactroot" in html or "data-reactid" in html:
        ts.framework = "React"
    elif "data-v-" in html or "__vue__" in html:
        ts.framework = "Vue.js"
    elif "ng-version" in html or "ng-app" in html:
        ts.framework = "Angular"
    elif "svelte-kit" in lo:
        ts.framework = "SvelteKit"

    # JS lib
    if "jquery" in lo and ("jquery.min.js" in lo or "jquery-" in lo):
        ts.js_lib = "jQuery"

    # CSS framework
    if "bootstrap" in lo and ("bootstrap.min.css" in lo or "bootstrap.bundle" in lo):
        ts.css_fw = "Bootstrap"
    elif "tailwindcss" in lo or "cdn.tailwindcss.com" in lo:
        ts.css_fw = "Tailwind CSS"
    elif "bulma" in lo and "bulma.min.css" in lo:
        ts.css_fw = "Bulma"
    elif "materialize" in lo:
        ts.css_fw = "Materialize"

    # Analytics
    ana: list[str] = []
    if "google-analytics.com" in html or "gtag/js" in html:
        ana.append("Google Analytics")
    if "googletagmanager.com" in html:
        ana.append("GTM")
    if "connect.facebook.net" in html and "fbevents.js" in html:
        ana.append("Facebook Pixel")
    if "hotjar.com" in html:
        ana.append("Hotjar")
    if "clarity.ms" in html:
        ana.append("MS Clarity")
    if "mixpanel.com" in html:
        ana.append("Mixpanel")
    ts.analytics = ana

    # E-commerce
    if "woocommerce" in lo:
        ts.ecommerce = "WooCommerce"
    elif "cdn.shopify.com" in html:
        ts.ecommerce = "Shopify"
    elif "magento" in lo:
        ts.ecommerce = "Magento"
    elif "prestashop" in lo:
        ts.ecommerce = "PrestaShop"

    # Chat
    if "intercomcdn.com" in html or "intercom.io" in html:
        ts.chat = "Intercom"
    elif "drift.com" in html:
        ts.chat = "Drift"
    elif "crisp.chat" in html:
        ts.chat = "Crisp"
    elif "tidio.com" in html:
        ts.chat = "Tidio"
    elif "tawk.to" in html:
        ts.chat = "Tawk.to"
    elif "livechat.com" in html:
        ts.chat = "LiveChat"
    elif "zopim.com" in html:
        ts.chat = "Zendesk Chat"

    # Marketing / email
    if "mailchimp.com" in html:
        ts.marketing = "Mailchimp"
    elif "hubspot.com" in html:
        ts.marketing = "HubSpot"
    elif "convertkit.com" in html:
        ts.marketing = "ConvertKit"
    elif "klaviyo.com" in html:
        ts.marketing = "Klaviyo"

    # Maps
    if "maps.googleapis.com" in html:
        ts.maps = "Google Maps"
    elif "mapbox.com" in html:
        ts.maps = "Mapbox"
    elif "leafletjs.com" in html:
        ts.maps = "Leaflet"

    # Payment
    if "js.stripe.com" in html:
        ts.payment = "Stripe"
    elif "paypal.com" in html and "paypalobjects.com" in html:
        ts.payment = "PayPal"
    elif "squareup.com" in html:
        ts.payment = "Square"

    # Backend
    xp = h.get("x-powered-by", "")
    ts.backend = (
        "PHP"         if "php"     in xp else
        "ASP.NET"     if "asp.net" in xp else
        "Node.js"     if "express" in xp else ""
    )

    return ts


# ── Spruce My Site opportunity detection ───────────────────────────────────────

def _missing_services(bd: ScoreBreakdown, ts: TechStack,
                       soup: BeautifulSoup, html: str) -> list[str]:
    """Return list of Spruce service keys the site is missing."""
    missing: list[str] = []
    lo = html.lower()

    if bd.https < 20:
        missing.append("ssl")
    if bd.mobile == 0:
        missing.append("mobile")
    if bd.speed < 15:
        missing.append("speed")
    if bd.seo < 20:          # missing at least 2 of 5 SEO points
        missing.append("seo")
    if bd.seo < 5:            # no schema at all
        missing.append("schema")
    if not ts.analytics:
        missing.append("analytics")
    if bd.content < 5:        # no social links
        missing.append("social")
    if bd.content < 10:       # no contact link
        missing.append("contact")
    if not ts.chat:
        missing.append("chat")
    # CTA / lead form detection
    has_form = bool(soup.find("form"))
    has_cta  = bool(soup.find("a", string=re.compile(r'book|quote|enquire|get started|call us|free', re.I)))
    if not has_form and not has_cta:
        missing.append("cta")
    if not ts.maps:
        missing.append("maps")
    if bd.content < 20:       # no favicon
        missing.append("favicon")
    if not ts.marketing:
        missing.append("email_mktg")
    # Outdated CMS / builder
    old_builders = {"Wix", "Weebly", "Jimdo", "Google Sites"}
    if ts.cms in old_builders or (not ts.cms and not ts.framework):
        missing.append("modern_cms")

    return missing


# ── Full analysis of one business ─────────────────────────────────────────────

def _analyse(biz: Business, timeout: int) -> Business:
    if not biz.website:
        biz.error = "No website"
        biz.score = None
        return biz

    resp, elapsed, err_note = _fetch(biz.website, timeout)
    biz.response_time = elapsed

    if resp is None:
        biz.error = err_note or "Unreachable"
        biz.score = 0
        biz.score_breakdown = ScoreBreakdown()
        return biz

    biz.final_url = resp.url

    try:
        soup = BeautifulSoup(resp.text[:600_000], "html.parser")
    except Exception as e:
        biz.error = f"Parse error: {e}"
        return biz

    bd = _score(soup, resp, elapsed, err_note)
    ts = _detect_stack(resp.text, dict(resp.headers), resp.url)

    biz.score_breakdown  = bd
    biz.score            = bd.total
    biz.tech_stack       = ts
    biz.missing_services = _missing_services(bd, ts, soup, resp.text)
    return biz


def analyse_all(businesses: list[Business], workers: int, timeout: int) -> list[Business]:
    has_site = [b for b in businesses if b.website]
    total = len(has_site)
    done  = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_analyse, b, timeout): b for b in has_site}
        for fut in as_completed(futures):
            done += 1
            biz = futures[fut]
            try:
                fut.result()
                status = (f"score={biz.score}" if biz.score is not None
                          else f"err={biz.error}")
            except Exception as exc:
                biz.error = str(exc)
                status = f"exception: {exc}"
            print(f"  [{done:>3}/{total}] {biz.name[:45]:<45} {status}")

    return businesses


# ── HTML report ────────────────────────────────────────────────────────────────

_SCORE_COLORS = [
    (30,  "#ef4444"),   # red    – prime lead
    (50,  "#f97316"),   # orange – good lead
    (70,  "#eab308"),   # yellow – moderate
    (85,  "#22c55e"),   # green  – good site
    (100, "#3b82f6"),   # blue   – excellent
]

def _score_color(score: Optional[int]) -> str:
    if score is None:
        return "#94a3b8"
    for threshold, color in _SCORE_COLORS:
        if score <= threshold:
            return color
    return "#3b82f6"


def _badge_class(grade: str) -> str:
    return {
        "No Website":  "badge-red",
        "Prime":       "badge-red",
        "Good":        "badge-orange",
        "Moderate":    "badge-yellow",
        "Low":         "badge-green",
        "Unreachable": "badge-gray",
    }.get(grade, "badge-gray")


def _tech_html(ts: Optional[TechStack]) -> str:
    if not ts:
        return '<span class="muted">—</span>'
    badges = ts.badges()
    if not badges:
        return '<span class="muted">Unknown</span>'
    return "".join(
        f'<span class="tech">{html_module.escape(b)}</span>'
        for b in badges[:8]
    )


def _service_label(key: str) -> str:
    for k, label, _ in SPRUCE_SERVICES:
        if k == key:
            return label
    return key


def _opportunities_html(missing: list[str]) -> str:
    if not missing:
        return '<span class="opp-none">✓ Well equipped</span>'
    items = [
        f'<span class="opp" title="{html_module.escape(desc)}">{html_module.escape(label)}</span>'
        for k, label, desc in SPRUCE_SERVICES if k in missing
    ]
    return "".join(items)


def build_report(businesses: list[Business], location: str, category: str) -> str:
    sorted_biz = sorted(
        businesses,
        key=lambda b: (b.score if b.score is not None else -1),
    )

    with_site     = [b for b in businesses if b.website]
    without_site  = [b for b in businesses if not b.website]
    scored        = [b for b in businesses if b.score is not None]
    avg_score     = int(sum(b.score for b in scored) / len(scored)) if scored else 0
    prime_count   = sum(
        1 for b in businesses
        if not b.website or (b.score is not None and b.score <= 30)
    )

    # Count missing service frequency
    service_freq: dict[str, int] = {}
    for b in businesses:
        for k in b.missing_services:
            service_freq[k] = service_freq.get(k, 0) + 1

    rows = ""
    for b in sorted_biz:
        # Website cell
        if b.website:
            display = (b.final_url or b.website).replace("https://", "").replace("http://", "").rstrip("/")
            display = display[:45]
            site_cell = (
                f'<a href="{html_module.escape(b.final_url or b.website)}" '
                f'target="_blank" rel="noopener">{html_module.escape(display)}</a>'
            )
        else:
            site_cell = '<span class="muted">None</span>'

        # Score cell
        grade = b.lead_grade
        gc    = _badge_class(grade)
        if b.score is not None:
            col = _score_color(b.score)
            score_cell = f"""
              <div class="sc-wrap">
                <span class="sc-num" style="color:{col}">{b.score}</span>
                <div class="sc-bar-bg"><div class="sc-bar" style="width:{b.score}%;background:{col}"></div></div>
                <span class="badge {gc}">{html_module.escape(grade)}</span>
              </div>"""
            if b.score_breakdown:
                bd = b.score_breakdown
                score_cell += (
                    f'<div class="sc-detail">'
                    f'🔒{bd.https} ⚡{bd.speed} 📱{bd.mobile} 🔍{bd.seo} 📋{bd.content}'
                    f'</div>'
                )
        elif not b.website:
            score_cell = f'<span class="badge {gc}">No Website</span>'
        else:
            score_cell = f'<span class="badge {gc}" title="{html_module.escape(b.error)}">Error</span>'

        maps_url = f"https://www.openstreetmap.org/?mlat={b.lat}&mlon={b.lon}&zoom=17"
        opp_count = len(b.missing_services)
        opp_label = f' <small class="opp-count">({opp_count})</small>' if opp_count else ""

        rows += f"""
<tr>
  <td>
    <strong>{html_module.escape(b.name)}</strong><br>
    <span class="muted small">{html_module.escape(b.address)}</span>
  </td>
  <td class="url-col">{site_cell}</td>
  <td class="score-col">{score_cell}</td>
  <td>{_tech_html(b.tech_stack)}</td>
  <td class="opp-col">{_opportunities_html(b.missing_services)}{opp_label}</td>
  <td><span class="muted small">{html_module.escape(b.phone)}</span></td>
  <td><a class="map-lnk" href="{maps_url}" target="_blank" rel="noopener">Map</a></td>
</tr>"""

    ts_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    loc_e  = html_module.escape(location)
    cat_e  = html_module.escape(category)

    # Top opportunities bar
    top_opps = sorted(service_freq.items(), key=lambda x: -x[1])[:6]
    opp_bars = ""
    max_freq = top_opps[0][1] if top_opps else 1
    for svc_key, freq in top_opps:
        lbl = _service_label(svc_key)
        pct = int(freq / max_freq * 100)
        opp_bars += f"""
          <div class="opp-bar-row">
            <span class="opp-bar-lbl">{html_module.escape(lbl)}</span>
            <div class="opp-bar-bg">
              <div class="opp-bar-fill" style="width:{pct}%"></div>
            </div>
            <span class="opp-bar-num">{freq}</span>
          </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lead Report — {loc_e} / {cat_e}</title>
<style>
:root {{
  --bg:#0f172a; --card:#1e293b; --border:#334155;
  --text:#e2e8f0; --muted:#64748b; --accent:#3b82f6;
  --red:#ef4444; --orange:#f97316; --yellow:#eab308;
  --green:#22c55e; --blue:#3b82f6;
}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);font-size:14px}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{text-decoration:underline}}

/* Header */
header{{background:var(--card);border-bottom:2px solid var(--border);
  padding:1.5rem 2rem;display:flex;align-items:center;gap:1rem;flex-wrap:wrap}}
.logo{{font-size:1.5rem;font-weight:800;color:white;letter-spacing:-.02em}}
.logo span{{color:var(--accent)}}
.meta{{color:var(--muted);font-size:.85rem;margin-left:auto}}

/* Stats row */
.stats{{display:flex;gap:.75rem;padding:1.25rem 2rem;flex-wrap:wrap}}
.stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:.9rem 1.25rem;flex:1;min-width:120px}}
.stat-n{{font-size:1.8rem;font-weight:700;line-height:1}}
.stat-l{{font-size:.7rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;margin-top:.2rem}}
.c-red{{color:var(--red)}} .c-grn{{color:var(--green)}}
.c-blue{{color:var(--blue)}} .c-org{{color:var(--orange)}}

/* Two-col layout */
.body-grid{{display:grid;grid-template-columns:1fr 260px;gap:0;padding:0 2rem 2rem}}
@media(max-width:900px){{.body-grid{{grid-template-columns:1fr;padding:0 .5rem 2rem}}
  .sidebar{{order:-1}}}}

/* Table area */
.table-area{{min-width:0}}
.controls{{display:flex;gap:.75rem;padding:.75rem 0;flex-wrap:wrap;align-items:center}}
.controls input{{background:var(--card);border:1px solid var(--border);
  color:var(--text);padding:.45rem .9rem;border-radius:8px;font-size:.85rem;
  width:220px;outline:none}}
.controls input:focus{{border-color:var(--accent)}}
.controls select{{background:var(--card);border:1px solid var(--border);
  color:var(--text);padding:.45rem .9rem;border-radius:8px;font-size:.85rem;
  outline:none;cursor:pointer}}
.export-btn{{background:var(--accent);color:#fff;border:none;padding:.45rem 1rem;
  border-radius:8px;font-size:.82rem;font-weight:600;cursor:pointer;margin-left:auto}}
.export-btn:hover{{opacity:.85}}
.tbl-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:collapse}}
th{{background:var(--card);color:var(--muted);font-size:.7rem;font-weight:700;
  text-transform:uppercase;letter-spacing:.06em;padding:.65rem .9rem;
  border-bottom:2px solid var(--border);white-space:nowrap;cursor:pointer;
  user-select:none;text-align:left}}
th:hover{{color:var(--text)}}
th::after{{content:' ↕';opacity:.3}}
th.asc::after{{content:' ↑';opacity:1}}
th.desc::after{{content:' ↓';opacity:1}}
td{{padding:.65rem .9rem;border-bottom:1px solid var(--border);vertical-align:top}}
tr:hover td{{background:rgba(255,255,255,.02)}}
tr.hidden{{display:none}}

.muted{{color:var(--muted)}} .small{{font-size:.75rem}}
.url-col{{max-width:180px;word-break:break-all;font-size:.8rem}}
.score-col{{min-width:150px}}
.opp-col{{min-width:220px}}

/* Score widget */
.sc-wrap{{display:flex;flex-direction:column;gap:.25rem}}
.sc-num{{font-size:1.6rem;font-weight:700;line-height:1}}
.sc-bar-bg{{background:var(--border);border-radius:3px;height:5px;overflow:hidden}}
.sc-bar{{height:100%;border-radius:3px}}
.sc-detail{{font-size:.65rem;color:var(--muted);margin-top:.2rem}}

/* Badges */
.badge{{display:inline-block;padding:.18rem .55rem;border-radius:999px;
  font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em}}
.badge-red{{background:rgba(239,68,68,.15);color:#ef4444}}
.badge-orange{{background:rgba(249,115,22,.15);color:#f97316}}
.badge-yellow{{background:rgba(234,179,8,.15);color:#eab308}}
.badge-green{{background:rgba(34,197,94,.15);color:#22c55e}}
.badge-gray{{background:rgba(100,116,139,.15);color:#94a3b8}}

/* Tech badges */
.tech{{display:inline-block;background:rgba(59,130,246,.08);
  color:#93c5fd;border:1px solid rgba(59,130,246,.18);
  border-radius:5px;padding:.12rem .45rem;font-size:.68rem;margin:.1rem .05rem}}

/* Opportunity tags */
.opp{{display:inline-block;background:rgba(249,115,22,.1);color:#fb923c;
  border:1px solid rgba(249,115,22,.2);border-radius:5px;
  padding:.12rem .4rem;font-size:.65rem;margin:.1rem .05rem;cursor:default}}
.opp-none{{color:var(--green);font-size:.75rem}}
.opp-count{{color:var(--muted);font-size:.7rem}}

/* Sidebar */
.sidebar{{padding:1.25rem 0 0 1.5rem;border-left:1px solid var(--border)}}
@media(max-width:900px){{.sidebar{{padding:1rem 0;border-left:none;
  border-bottom:1px solid var(--border)}}}}
.sidebar h3{{font-size:.8rem;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.08em;margin-bottom:.9rem}}
.opp-bar-row{{display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem}}
.opp-bar-lbl{{font-size:.72rem;color:var(--text);min-width:130px}}
.opp-bar-bg{{flex:1;background:var(--border);border-radius:3px;height:6px;overflow:hidden}}
.opp-bar-fill{{height:100%;background:var(--orange);border-radius:3px}}
.opp-bar-num{{font-size:.72rem;color:var(--muted);min-width:20px;text-align:right}}

.legend{{margin-top:1.5rem}}
.legend-row{{display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem;font-size:.75rem}}
.legend-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}

footer{{text-align:center;padding:1.5rem;color:var(--muted);
  font-size:.75rem;border-top:1px solid var(--border)}}
</style>
</head>
<body>

<header>
  <div class="logo">Spruce<span>MySite</span> — Lead Generator</div>
  <div class="meta">
    📍 {loc_e} &nbsp;|&nbsp; 🏷 {cat_e} &nbsp;|&nbsp; 🕐 {ts_str}
  </div>
</header>

<div class="stats">
  <div class="stat"><div class="stat-n c-blue">{len(businesses)}</div>
    <div class="stat-l">Businesses Found</div></div>
  <div class="stat"><div class="stat-n">{len(with_site)}</div>
    <div class="stat-l">Have a Website</div></div>
  <div class="stat"><div class="stat-n c-red">{len(without_site)}</div>
    <div class="stat-l">No Website</div></div>
  <div class="stat"><div class="stat-n">{avg_score}<small style="font-size:1rem">/100</small></div>
    <div class="stat-l">Avg Website Score</div></div>
  <div class="stat"><div class="stat-n c-red">{prime_count}</div>
    <div class="stat-l">Prime Leads</div></div>
</div>

<div class="body-grid">
<div class="table-area">
  <div class="controls">
    <input type="text" id="search" placeholder="Search name, tech, address…">
    <select id="grade-sel">
      <option value="">All grades</option>
      <option value="no website">No Website</option>
      <option value="prime">Prime</option>
      <option value="good">Good</option>
      <option value="moderate">Moderate</option>
    </select>
    <button class="export-btn" onclick="doExport()">⬇ Export CSV</button>
  </div>
  <div class="tbl-wrap">
    <table id="tbl">
      <thead><tr>
        <th onclick="sort(0)">Business</th>
        <th onclick="sort(1)">Website</th>
        <th onclick="sort(2)">Score</th>
        <th>Tech Stack</th>
        <th>Spruce Opportunities</th>
        <th>Phone</th>
        <th></th>
      </tr></thead>
      <tbody id="tbody">{rows}</tbody>
    </table>
  </div>
</div>

<div class="sidebar">
  <h3>Top Opportunities</h3>
  {opp_bars}

  <div class="legend">
    <h3 style="margin-bottom:.7rem">Score Guide</h3>
    <div class="legend-row"><div class="legend-dot" style="background:#ef4444"></div>0–30 &nbsp;Prime Lead</div>
    <div class="legend-row"><div class="legend-dot" style="background:#f97316"></div>31–50 Good Lead</div>
    <div class="legend-row"><div class="legend-dot" style="background:#eab308"></div>51–70 Moderate</div>
    <div class="legend-row"><div class="legend-dot" style="background:#22c55e"></div>71–85 Good Site</div>
    <div class="legend-row"><div class="legend-dot" style="background:#3b82f6"></div>86–100 Excellent</div>
    <p style="font-size:.7rem;color:var(--muted);margin-top:.75rem">
      Score breakdown:<br>
      🔒 HTTPS /20 &nbsp;⚡ Speed /20<br>
      📱 Mobile /15 &nbsp;🔍 SEO /25<br>
      📋 Content /20
    </p>
  </div>
</div>
</div>

<footer>Generated by Spruce My Site Lead Generator &nbsp;|&nbsp;
Business data © OpenStreetMap contributors (ODbL)</footer>

<script>
const tbody = document.getElementById('tbody');
let sortCol = 2, sortAsc = true;

function filterRows() {{
  const q = document.getElementById('search').value.toLowerCase();
  const g = document.getElementById('grade-sel').value.toLowerCase();
  tbody.querySelectorAll('tr').forEach(r => {{
    const t = r.textContent.toLowerCase();
    r.classList.toggle('hidden', (q && !t.includes(q)) || (g && !t.includes(g)));
  }});
}}
document.getElementById('search').addEventListener('input', filterRows);
document.getElementById('grade-sel').addEventListener('change', filterRows);

function sort(col) {{
  const ths = document.querySelectorAll('th');
  ths.forEach(h => h.classList.remove('asc','desc'));
  if (sortCol === col) sortAsc = !sortAsc; else {{ sortCol = col; sortAsc = true; }}
  ths[col].classList.add(sortAsc ? 'asc' : 'desc');
  const rows = [...tbody.querySelectorAll('tr')];
  rows.sort((a, b) => {{
    const av = a.cells[col]?.textContent.trim() || '';
    const bv = b.cells[col]?.textContent.trim() || '';
    const an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return sortAsc ? an - bn : bn - an;
    return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

function doExport() {{
  const vis = [...tbody.querySelectorAll('tr:not(.hidden)')];
  const hdr = ['Name','Address','Website','Score','Grade','Tech Stack','Opportunities','Phone'];
  const data = [hdr, ...vis.map(r => [
    r.cells[0]?.querySelector('strong')?.textContent || '',
    r.cells[0]?.querySelector('.small')?.textContent || '',
    r.cells[1]?.querySelector('a')?.href || '',
    r.cells[2]?.querySelector('.sc-num')?.textContent || r.cells[2]?.querySelector('.badge')?.textContent || '',
    r.cells[2]?.querySelector('.badge')?.textContent || '',
    [...(r.cells[3]?.querySelectorAll('.tech') || [])].map(b => b.textContent).join(', '),
    [...(r.cells[4]?.querySelectorAll('.opp') || [])].map(b => b.textContent).join(', '),
    r.cells[5]?.textContent.trim() || '',
  ])];
  const csv = data.map(r => r.map(c => '"' + String(c).replace(/"/g,'""') + '"').join(',')).join('\\n');
  const a = Object.assign(document.createElement('a'), {{
    href: URL.createObjectURL(new Blob([csv], {{type:'text/csv'}})),
    download: 'spruce_leads_{ts_str}.csv'.replace(/[: ]/g,'_'),
  }});
  a.click();
}}
</script>
</body>
</html>"""


# ── CSV export ─────────────────────────────────────────────────────────────────

def save_csv(businesses: list[Business], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "Name", "Address", "Phone", "Website", "Final URL",
            "Score", "Lead Grade",
            "HTTPS /20", "Speed /20", "Mobile /15", "SEO /25", "Content /20",
            "Server", "CMS", "Framework", "JS Lib", "CSS Framework",
            "Analytics", "E-commerce", "Chat", "Marketing", "CDN", "Backend",
            "Missing Services (Spruce Opportunities)", "Error",
        ])
        for b in businesses:
            bd = b.score_breakdown
            ts = b.tech_stack
            w.writerow([
                b.name, b.address, b.phone, b.website, b.final_url,
                b.score if b.score is not None else "",
                b.lead_grade,
                bd.https   if bd else "", bd.speed  if bd else "",
                bd.mobile  if bd else "", bd.seo    if bd else "",
                bd.content if bd else "",
                ts.server   if ts else "", ts.cms      if ts else "",
                ts.framework if ts else "", ts.js_lib   if ts else "",
                ts.css_fw   if ts else "",
                ", ".join(ts.analytics) if ts else "",
                ts.ecommerce if ts else "", ts.chat   if ts else "",
                ts.marketing if ts else "", ts.cdn    if ts else "",
                ts.backend  if ts else "",
                ", ".join(_service_label(k) for k in b.missing_services),
                b.error,
            ])


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Spruce My Site — Local Website Lead Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available categories:\n  " + ", ".join(sorted(CATEGORIES)),
    )
    p.add_argument("--location", "-l", required=True,
                   help="City / area to search  e.g. 'Manchester, UK'")
    p.add_argument("--category", "-c", required=True,
                   help="Business type  e.g. restaurant, dentist, gym")
    p.add_argument("--radius",   "-r", type=float, default=5.0,
                   help="Search radius in km (default 5)")
    p.add_argument("--limit",    "-n", type=int,   default=50,
                   help="Max businesses to find (default 50)")
    p.add_argument("--workers",  "-w", type=int,   default=5,
                   help="Parallel website analysers (default 5)")
    p.add_argument("--timeout",  "-t", type=int,   default=10,
                   help="Website fetch timeout in seconds (default 10)")
    p.add_argument("--output",   "-o", default=".",
                   help="Output directory (default: current dir)")
    args = p.parse_args()

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    slug   = re.sub(r"[^\w]+", "_", f"{args.location}_{args.category}").lower().strip("_")
    stamp  = datetime.now().strftime("%Y%m%d_%H%M")
    h_path = outdir / f"leads_{slug}_{stamp}.html"
    c_path = outdir / f"leads_{slug}_{stamp}.csv"

    print(f"\n{'═'*58}")
    print("  Spruce My Site — Local Website Lead Generator")
    print(f"{'═'*58}")
    print(f"  Location : {args.location}")
    print(f"  Category : {args.category}")
    print(f"  Radius   : {args.radius} km   Limit: {args.limit}")
    print(f"{'═'*58}\n")

    print("[1/3] Discovering businesses …")
    try:
        businesses = fetch_businesses(args.location, args.category,
                                      args.radius, args.limit)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)

    with_site = sum(1 for b in businesses if b.website)
    print(f"\n  {len(businesses)} businesses found, {with_site} have websites\n")

    print("[2/3] Analysing websites …")
    businesses = analyse_all(businesses, args.workers, args.timeout)

    print(f"\n[3/3] Writing reports …")
    h_path.write_text(build_report(businesses, args.location, args.category), encoding="utf-8")
    save_csv(businesses, c_path)

    scored     = [b for b in businesses if b.score is not None]
    avg        = int(sum(b.score for b in scored) / len(scored)) if scored else 0
    prime      = sum(1 for b in businesses
                     if not b.website or (b.score is not None and b.score <= 30))

    print(f"\n{'═'*58}")
    print("  RESULTS")
    print(f"{'═'*58}")
    print(f"  Total businesses : {len(businesses)}")
    print(f"  With website     : {with_site}")
    print(f"  No website       : {len(businesses) - with_site}  ← prime leads!")
    print(f"  Avg score        : {avg}/100")
    print(f"  Prime leads      : {prime}  (score ≤30 or no website)")
    print(f"{'═'*58}")
    print(f"\n  HTML → {h_path}")
    print(f"  CSV  → {c_path}\n")


if __name__ == "__main__":
    main()
