#!/usr/bin/env python3
"""
LKKU Aircraft Watch — lokální server
-------------------------------------
Spusť tento soubor: python3 lkku_radar_server.py
Prohlížeč se otevře automaticky na http://127.0.0.1:8765/

Co to dělá:
- Spustí malý webový server přímo na tvém počítači.
- Server sám (ne prohlížeč) se dotazuje veřejných ADS-B API (adsb.fi, adsb.lol)
  na letadla — tím odpadá CORS blokace, na kterou narazil přímý dotaz z prohlížeče.
- Prohlížeč se ptá jen lokálního serveru, ten odpovědi přeposílá dál.

Vypnutí: v terminálu, kde server běží, stiskni Ctrl+C.
Nic se nikam needěje ani neinstaluje — jen tenhle jeden soubor.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import urllib.parse
import json
import re
import csv
import math
import os
import webbrowser
import threading
import sys
import time
import uuid
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PORT = 8765
LKKU_LAT = 49.0294
LKKU_LON = 17.4397
REQUEST_RADIUS_NM = 45  # bezpečnostní rezerva; přesný výběr do 30 NM dělá appka v prohlížeči
API_URL = f"https://opendata.adsb.fi/api/v3/lat/{LKKU_LAT}/lon/{LKKU_LON}/dist/{REQUEST_RADIUS_NM}"
ROUTE_API_BASE = "https://api.adsbdb.com/v0/callsign/"

_route_cache = {}  # callsign -> flightroute dict or None (not found)


def fetch_route(callsign):
    callsign = (callsign or "").strip().upper()
    if not callsign:
        return None
    if callsign in _route_cache:
        return _route_cache[callsign]

    req = urllib.request.Request(
        ROUTE_API_BASE + urllib.parse.quote(callsign),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)",
            "Accept": "application/json",
        },
    )
    route = None
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        route = data.get("response", {}).get("flightroute")
    except Exception:
        route = None
    _route_cache[callsign] = route
    return route


AIRCRAFT_API_BASE = "https://api.adsbdb.com/v0/aircraft/"
_operator_cache = {}  # registration -> operator name string or None (not found)


def fetch_operator(registration):
    reg = (registration or "").strip().upper()
    if not reg:
        return None
    if reg in _operator_cache:
        return _operator_cache[reg]

    req = urllib.request.Request(
        AIRCRAFT_API_BASE + urllib.parse.quote(reg),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)",
            "Accept": "application/json",
        },
    )
    operator = None
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())
        aircraft = data.get("response")
        if isinstance(aircraft, dict):
            operator = aircraft.get("registered_owner") or None
    except Exception:
        operator = None
    _operator_cache[reg] = operator
    return operator


METAR_URL = "https://tgftp.nws.noaa.gov/data/observations/metar/stations/LKKU.TXT"
METAR_CACHE_TTL = 300  # seconds between real fetches
METAR_MAX_AGE_MIN = 120  # older than this counts as "not available"

_metar_cache = {"data": None, "ts": 0}


def _flight_category(vis_sm, ceiling_ft):
    if vis_sm is None and ceiling_ft is None:
        return None

    def cat_from_vis(v):
        if v is None:
            return "VFR"
        if v < 1:
            return "LIFR"
        if v < 3:
            return "IFR"
        if v < 5:
            return "MVFR"
        return "VFR"

    def cat_from_ceiling(c):
        if c is None:
            return "VFR"
        if c < 500:
            return "LIFR"
        if c < 1000:
            return "IFR"
        if c <= 3000:
            return "MVFR"
        return "VFR"

    order = {"LIFR": 0, "IFR": 1, "MVFR": 2, "VFR": 3}
    cv = cat_from_vis(vis_sm)
    cc = cat_from_ceiling(ceiling_ft)
    return cv if order[cv] < order[cc] else cc


def parse_metar(text):
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None

    try:
        obs_dt = datetime.strptime(lines[0], "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        obs_dt = None

    raw = " ".join(lines[1:])
    tokens = raw.split()
    cavok = "CAVOK" in tokens
    vis_sm = None
    ceiling_ft = None

    for tok in tokens:
        m = re.fullmatch(r"(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?", tok)
        if m:
            cover, height = m.group(1), int(m.group(2))
            if cover in ("BKN", "OVC"):
                ft = height * 100
                if ceiling_ft is None or ft < ceiling_ft:
                    ceiling_ft = ft
            continue
        m = re.fullmatch(r"VV(\d{3})", tok)
        if m:
            ft = int(m.group(1)) * 100
            if ceiling_ft is None or ft < ceiling_ft:
                ceiling_ft = ft
            continue
        if vis_sm is None:
            m = re.fullmatch(r"(\d{1,2})SM", tok)
            if m:
                vis_sm = float(m.group(1))
                continue
            m = re.fullmatch(r"\d{4}", tok)
            if m:
                vis_sm = int(tok) / 1609.344
                continue

    if cavok:
        category = "VFR"
    else:
        category = _flight_category(vis_sm, ceiling_ft)

    age_minutes = None
    if obs_dt is not None:
        age_minutes = (datetime.now(timezone.utc) - obs_dt).total_seconds() / 60

    available = age_minutes is not None and age_minutes <= METAR_MAX_AGE_MIN

    return {
        "raw": raw,
        "obs_time": obs_dt.isoformat() if obs_dt else None,
        "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
        "category": category if available else None,
        "available": available,
    }


def fetch_metar():
    now = time.time()
    if _metar_cache["data"] is not None and now - _metar_cache["ts"] < METAR_CACHE_TTL:
        return _metar_cache["data"]

    req = urllib.request.Request(
        METAR_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            text = resp.read().decode()
        result = parse_metar(text)
    except Exception:
        result = None

    if result is None:
        result = {"raw": None, "obs_time": None, "age_minutes": None, "category": None, "available": False}

    _metar_cache["data"] = result
    _metar_cache["ts"] = now
    return result


# Bristell (BRM Aero) ICAO type designators, per doc8643.com
BRISTELL_TYPES = ["BR23", "NG5", "BR8", "B23E"]
BRISTELL_API_BASE = "https://api.adsb.lol/v2/type/"
BRISTELL_CACHE_TTL = 25  # seconds between real fetches

_bristell_cache = {"data": None, "ts": 0}
_geocode_cache = {}  # (lat_grid, lon_grid) -> bigdatacloud reverse-geocode response dict or None


def _reverse_geocode(lat, lon):
    # Round to a ~55 km grid so nearby aircraft share one lookup/cache entry.
    key = (round(lat * 2) / 2, round(lon * 2) / 2)
    if key in _geocode_cache:
        return _geocode_cache[key]
    info = None
    try:
        url = (
            "https://api.bigdatacloud.net/data/reverse-geocode-client"
            f"?latitude={lat}&longitude={lon}&localityLanguage=en"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            info = json.loads(resp.read().decode())
    except Exception:
        info = None
    _geocode_cache[key] = info
    return info


def _country_code_for(lat, lon):
    info = _reverse_geocode(lat, lon)
    return (info or {}).get("countryCode") or None


def fetch_bristell():
    now = time.time()
    if _bristell_cache["data"] is not None and now - _bristell_cache["ts"] < BRISTELL_CACHE_TTL:
        return _bristell_cache["data"]

    seen = {}
    for t in BRISTELL_TYPES:
        req = urllib.request.Request(
            BRISTELL_API_BASE + t,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            for ac in data.get("ac", []) or []:
                hex_id = ac.get("hex")
                if hex_id:
                    seen[hex_id] = ac
        except Exception:
            continue

    aircraft = list(seen.values())
    for ac in aircraft:
        lat, lon = ac.get("lat"), ac.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            ac["country_code"] = _country_code_for(lat, lon)
            # Bristell/GA callsigns never match a published route (that lookup only
            # covers scheduled airline flights), so give the UI a fallback: the
            # nearest airfield to the current position, for it to label as a
            # probable departure/destination based on altitude and climb/descent.
            ac["nearest_airport"] = reverse_locate(lat, lon)
        else:
            ac["country_code"] = None
            ac["nearest_airport"] = None

    result = {"ac": aircraft}
    _bristell_cache["data"] = result
    _bristell_cache["ts"] = now
    return result


# ---------- Nearest-airfield lookup (OurAirports open data, ~80k airports/strips) ----------

AIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
AIRPORTS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "airports_cache.csv")
AIRPORTS_CACHE_MAX_AGE_DAYS = 30

_airports_cache = None  # lazy-loaded list of dicts


def _ensure_airports_csv():
    if os.path.exists(AIRPORTS_CACHE_PATH):
        age_days = (time.time() - os.path.getmtime(AIRPORTS_CACHE_PATH)) / 86400
        if age_days < AIRPORTS_CACHE_MAX_AGE_DAYS:
            return
    try:
        req = urllib.request.Request(
            AIRPORTS_CSV_URL, headers={"User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        with open(AIRPORTS_CACHE_PATH, "wb") as f:
            f.write(raw)
    except Exception:
        pass  # fine to keep using a stale/missing cache


def _load_airports():
    global _airports_cache
    if _airports_cache is not None:
        return _airports_cache

    _ensure_airports_csv()
    airports = []
    try:
        with open(AIRPORTS_CACHE_PATH, "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("type") == "closed":
                    continue
                try:
                    lat = float(row["latitude_deg"])
                    lon = float(row["longitude_deg"])
                except (KeyError, ValueError):
                    continue
                code = row.get("icao_code") or row.get("gps_code") or row.get("ident") or ""
                airports.append({"code": code, "name": row.get("name") or "", "lat": lat, "lon": lon})
    except Exception:
        airports = []

    _airports_cache = airports
    return airports


def _haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def nearest_airport(lat, lon, max_km=15):
    best, best_dist = None, max_km
    for ap in _load_airports():
        # cheap bounding-box pre-filter before the trig-heavy haversine call
        if abs(ap["lat"] - lat) > 0.5 or abs(ap["lon"] - lon) > 0.8:
            continue
        d = _haversine_km(lat, lon, ap["lat"], ap["lon"])
        if d < best_dist:
            best, best_dist = ap, d
    return best


def reverse_locate(lat, lon):
    if lat is None or lon is None:
        return "Unknown"
    airport = nearest_airport(lat, lon)
    if airport:
        return f"{airport['code']} · {airport['name']}" if airport["code"] else airport["name"]
    info = _reverse_geocode(lat, lon) or {}
    locality = info.get("locality") or info.get("city")
    country = info.get("countryName")
    if locality and country:
        return f"near {locality}, {country}"
    if country:
        return f"over {country}"
    return "Unknown location"


# Primary IANA timezone per ISO-3166 country code, for the countries BRM Aero's
# fleet realistically operates in. Good enough for a single-timezone approximation;
# falls back to UTC for anything not listed.
COUNTRY_TZ = {
    "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "DE": "Europe/Berlin",
    "PL": "Europe/Warsaw", "AT": "Europe/Vienna", "CH": "Europe/Zurich",
    "NL": "Europe/Amsterdam", "BE": "Europe/Brussels", "FR": "Europe/Paris",
    "GB": "Europe/London", "IE": "Europe/Dublin", "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
    "HU": "Europe/Budapest", "IT": "Europe/Rome", "ES": "Europe/Madrid",
    "PT": "Europe/Lisbon", "RO": "Europe/Bucharest", "GR": "Europe/Athens",
    "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana", "BG": "Europe/Sofia",
    "EE": "Europe/Tallinn", "LV": "Europe/Riga", "LT": "Europe/Vilnius",
    "US": "America/New_York",
}


def local_time_str(dt_utc, lat, lon):
    """Format a UTC datetime as local time at (lat, lon), tagged 'LT'. Falls back to UTC."""
    if dt_utc is None:
        return "—"
    country = _country_code_for(lat, lon) if lat is not None and lon is not None else None
    tz_name = COUNTRY_TZ.get(country)
    if tz_name:
        try:
            local_dt = dt_utc.astimezone(ZoneInfo(tz_name))
            return local_dt.strftime("%Y-%m-%d %H:%M") + " LT"
        except Exception:
            pass
    return dt_utc.strftime("%Y-%m-%d %H:%M") + " UTC"


# ---------- BRM Aero fleet watch (incl. ferry / test-flight marks) ----------

BRM_FLEET_REGISTRATIONS = [
    "OK-DUI90", "OK-QUU06", "OK-VAU99", "D-MZYW",
    "OK-HTO", "OK-BRP", "OK-IDA",
]
FLEET_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brm_fleet_log.json")
FLEET_POLL_INTERVAL_SEC = 300  # 5 minutes

_fleet_lock = threading.Lock()
_fleet_last_poll_ts = 0


def _load_fleet_log():
    try:
        with open(FLEET_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_fleet_log(log):
    try:
        with open(FLEET_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2)
    except Exception:
        pass


def _query_registration(reg):
    reg_clean = reg.strip()
    urls = [
        f"https://api.adsb.lol/v2/reg/{urllib.parse.quote(reg_clean)}",
        f"https://opendata.adsb.fi/api/v2/registration/{urllib.parse.quote(reg_clean)}",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
            ac = data.get("ac") or []
            if ac:
                return ac
        except Exception:
            continue
    return []


def poll_fleet_once():
    """Check every watched registration and log sightings + any live duplicate transponders."""
    global _fleet_last_poll_ts
    with _fleet_lock:
        log = _load_fleet_log()
        now_iso = datetime.now(timezone.utc).isoformat()

        for i, reg in enumerate(BRM_FLEET_REGISTRATIONS):
            if i > 0:
                time.sleep(1.1)  # stay under adsb.fi's 1 request/second public limit
            try:
                aircraft_list = _query_registration(reg)
            except Exception:
                aircraft_list = []

            entry = log.setdefault(reg, {"hexes": {}, "duplicate_events": []})
            distinct_hexes_now = sorted({ac.get("hex") for ac in aircraft_list if ac.get("hex")})

            # Same registration broadcast by more than one transponder at the same moment
            # ("zalétávací značka" left unprogrammed on a delivered aircraft) -> flag it.
            if len(distinct_hexes_now) > 1:
                entry["duplicate_events"].append({
                    "ts": now_iso,
                    "hexes": distinct_hexes_now,
                    "positions": [
                        {
                            "hex": ac.get("hex"),
                            "lat": ac.get("lat"),
                            "lon": ac.get("lon"),
                            "callsign": (ac.get("flight") or "").strip(),
                        }
                        for ac in aircraft_list
                    ],
                })
                entry["duplicate_events"] = entry["duplicate_events"][-20:]

            for ac in aircraft_list:
                hex_id = ac.get("hex")
                if not hex_id:
                    continue
                h = entry["hexes"].setdefault(hex_id, {"first_seen": now_iso})
                h["last_seen"] = now_iso
                h["callsign"] = (ac.get("flight") or "").strip()
                h["type"] = ac.get("t")
                lat, lon = ac.get("lat"), ac.get("lon")
                if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
                    h["last_lat"] = lat
                    h["last_lon"] = lon

            _track_flight(entry, reg, aircraft_list, now_iso)

        _save_fleet_log(log)
        _fleet_last_poll_ts = time.time()
        return log


def _close_flight(entry, current_flight, landing_time_iso, landing_lat, landing_lon, landing_known):
    entry.setdefault("flights", []).append({
        "id": current_flight["id"],
        "registration": current_flight["registration"],
        "callsign": current_flight.get("callsign"),
        "type": current_flight.get("type"),
        "takeoff_time": current_flight["takeoff_time"],
        "takeoff_lat": current_flight.get("takeoff_lat"),
        "takeoff_lon": current_flight.get("takeoff_lon"),
        "takeoff_known": current_flight.get("takeoff_known", False),
        "landing_time": landing_time_iso,
        "landing_lat": landing_lat,
        "landing_lon": landing_lon,
        "landing_known": landing_known,
        "pilot": "",
    })
    entry["flights"] = entry["flights"][-200:]  # cap stored history per registration


def _track_flight(entry, reg, aircraft_list, now_iso):
    """Takeoff/landing state machine for the logbook, run once per registration per poll."""
    primary_ac = aircraft_list[0] if aircraft_list else None
    is_airborne = False
    lat = lon = None
    if primary_ac is not None:
        alt = primary_ac.get("alt_baro")
        is_airborne = alt is not None and alt != "ground"
        lat, lon = primary_ac.get("lat"), primary_ac.get("lon")
    has_position = isinstance(lat, (int, float)) and isinstance(lon, (int, float))

    current_flight = entry.get("current_flight")

    if primary_ac is not None and is_airborne:
        if current_flight is None:
            ground_pos = entry.get("last_ground_position")
            current_flight = {
                "id": uuid.uuid4().hex[:12],
                "registration": reg,
                "callsign": (primary_ac.get("flight") or "").strip(),
                "type": primary_ac.get("t"),
                "takeoff_time": ground_pos["ts"] if ground_pos else now_iso,
                "takeoff_lat": ground_pos["lat"] if ground_pos else lat,
                "takeoff_lon": ground_pos["lon"] if ground_pos else lon,
                "takeoff_known": ground_pos is not None,
                "last_seen_ts": now_iso,
                "last_lat": lat,
                "last_lon": lon,
            }
        else:
            current_flight["last_seen_ts"] = now_iso
            if has_position:
                current_flight["last_lat"] = lat
                current_flight["last_lon"] = lon
        entry["current_flight"] = current_flight

    elif primary_ac is not None and not is_airborne:
        if has_position:
            entry["last_ground_position"] = {"lat": lat, "lon": lon, "ts": now_iso}
        if current_flight is not None:
            _close_flight(entry, current_flight, now_iso, lat, lon, landing_known=True)
            entry["current_flight"] = None

    else:
        # not seen at all this poll
        if current_flight is not None:
            try:
                gap_sec = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(current_flight["last_seen_ts"])).total_seconds()
            except Exception:
                gap_sec = 0
            if gap_sec > FLEET_POLL_INTERVAL_SEC * 2:
                _close_flight(
                    entry, current_flight, current_flight["last_seen_ts"],
                    current_flight.get("last_lat"), current_flight.get("last_lon"),
                    landing_known=False,
                )
                entry["current_flight"] = None


def _fleet_poll_loop():
    while True:
        try:
            poll_fleet_once()
        except Exception:
            pass
        time.sleep(FLEET_POLL_INTERVAL_SEC)


def build_fleet_status():
    with _fleet_lock:
        log = _load_fleet_log()
    now = datetime.now(timezone.utc)
    fleet = []

    for reg in BRM_FLEET_REGISTRATIONS:
        entry = log.get(reg, {})
        hexes = entry.get("hexes", {})

        primary_hex, primary = None, None
        for hex_id, h in hexes.items():
            if primary is None or h.get("last_seen", "") > primary.get("last_seen", ""):
                primary_hex, primary = hex_id, h

        if primary:
            age_minutes = None
            try:
                age_minutes = (now - datetime.fromisoformat(primary["last_seen"])).total_seconds() / 60
            except Exception:
                pass
            lat, lon = primary.get("last_lat"), primary.get("last_lon")
            fleet.append({
                "registration": reg,
                "type": primary.get("type"),
                "callsign": primary.get("callsign"),
                "hex": primary_hex,
                "last_seen": primary.get("last_seen"),
                "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
                "currently_airborne": age_minutes is not None and age_minutes < (FLEET_POLL_INTERVAL_SEC / 60) * 1.5,
                "location": reverse_locate(lat, lon) if lat is not None else "Unknown",
                "lat": lat,
                "lon": lon,
                "known_hex_count": len(hexes),
                "duplicate_alert": len(hexes) > 1 or bool(entry.get("duplicate_events")),
                "duplicate_events": entry.get("duplicate_events", [])[-5:],
                "hexes": [{"hex": hx, **hd} for hx, hd in hexes.items()],
            })
        else:
            fleet.append({
                "registration": reg, "type": None, "callsign": None, "hex": None,
                "last_seen": None, "age_minutes": None, "currently_airborne": False,
                "location": "Never seen yet", "lat": None, "lon": None,
                "known_hex_count": 0, "duplicate_alert": False, "duplicate_events": [], "hexes": [],
            })

    return {"fleet": fleet, "poll_interval_sec": FLEET_POLL_INTERVAL_SEC, "last_poll_ts": _fleet_last_poll_ts}


def _format_duration(minutes):
    if minutes is None:
        return "—"
    h = int(minutes // 60)
    m = int(round(minutes % 60))
    return f"{h}:{m:02d}"


def build_logbook(reg_filter=None):
    with _fleet_lock:
        log = _load_fleet_log()

    regs = [reg_filter] if reg_filter else BRM_FLEET_REGISTRATIONS
    raw_rows = []

    for reg in regs:
        entry = log.get(reg, {})
        for f in entry.get("flights", []):
            try:
                takeoff_dt = datetime.fromisoformat(f["takeoff_time"])
            except Exception:
                takeoff_dt = None
            try:
                landing_dt = datetime.fromisoformat(f["landing_time"])
            except Exception:
                landing_dt = None

            duration_min = None
            if takeoff_dt and landing_dt:
                duration_min = (landing_dt - takeoff_dt).total_seconds() / 60

            takeoff_known = f.get("takeoff_known", False)
            landing_known = f.get("landing_known", False)
            incomplete = not takeoff_known or not landing_known
            if not takeoff_known and not landing_known:
                note = ("Departure and arrival unconfirmed - aircraft was already airborne when first "
                        "detected, and its signal was later lost mid-flight.")
            elif not takeoff_known:
                note = "Departure airport/time unknown - aircraft was already airborne when first detected on radar."
            elif not landing_known:
                note = ("Arrival airport/time unknown - signal was lost while still airborne; position shown "
                        "is the last known contact, not necessarily the landing site.")
            else:
                note = ""

            raw_rows.append((f.get("takeoff_time") or "", {
                "id": f.get("id"),
                "date": takeoff_dt.strftime("%Y-%m-%d") if takeoff_dt else "—",
                "registration": f.get("registration", reg),
                "type": f.get("type"),
                "callsign": f.get("callsign"),
                "departure": reverse_locate(f.get("takeoff_lat"), f.get("takeoff_lon")),
                "destination": reverse_locate(f.get("landing_lat"), f.get("landing_lon")),
                "takeoff_local": local_time_str(takeoff_dt, f.get("takeoff_lat"), f.get("takeoff_lon")),
                "landing_local": local_time_str(landing_dt, f.get("landing_lat"), f.get("landing_lon")),
                "duration_minutes": round(duration_min) if duration_min is not None else None,
                "duration_str": _format_duration(duration_min),
                "pilot": f.get("pilot", ""),
                "incomplete": incomplete,
                "incomplete_note": note,
            }))

    raw_rows.sort(key=lambda pair: pair[0])
    return [row for _, row in raw_rows]


def update_flight_pilot(flight_id, pilot_name):
    with _fleet_lock:
        log = _load_fleet_log()
        updated = False
        for entry in log.values():
            for f in entry.get("flights", []):
                if f.get("id") == flight_id:
                    f["pilot"] = (pilot_name or "").strip()[:100]
                    updated = True
                    break
            if updated:
                break
        if updated:
            _save_fleet_log(log)
        return updated


def export_logbook_csv(reg_filter=None):
    rows = build_logbook(reg_filter)
    buf = io.StringIO()
    buf.write("﻿")  # UTF-8 BOM so Excel renders Czech characters correctly
    writer = csv.writer(buf)
    writer.writerow(["Date", "Registration", "Type", "Callsign", "Departure", "Destination",
                      "Takeoff", "Landing", "Duration", "Pilot", "Incomplete", "Note"])
    for r in rows:
        writer.writerow([
            r["date"], r["registration"], r.get("type") or "", r.get("callsign") or "",
            r["departure"], r["destination"], r["takeoff_local"], r["landing_local"],
            r["duration_str"], r.get("pilot") or "",
            "YES" if r["incomplete"] else "", r.get("incomplete_note") or "",
        ])
    return buf.getvalue()


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BRM Aero · Aircraft Watch</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#070b09;
    --bg2:#0c130f;
    --grid:#1c3a26;
    --grid-dim:#122217;
    --sweep:#34d17a;
    --sweep-dim:#1a6b3d;
    --amber:#ffb545;
    --amber-dim:#8a641f;
    --cyan:#7fe8ff;
    --text:#c9ffe0;
    --text-dim:#6b8a76;
    --danger:#ff5d5d;
    --panel-border:#1c3a26;
  }
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;height:100%;background:var(--bg);}
  body{
    font-family:'IBM Plex Mono', monospace;
    color:var(--text);
    background:
      radial-gradient(ellipse at 50% -10%, #0e1a12 0%, var(--bg) 60%);
    min-height:100vh;
    display:flex;
    flex-direction:column;
  }

  .tabbar{
    display:flex;
    align-items:center;
    gap:4px;
    padding:10px 22px 0;
    background:#050807;
    border-bottom:1px solid var(--panel-border);
    flex-wrap:wrap;
  }
  .tabbar-brand{
    font-family:'Space Grotesk', sans-serif;
    font-weight:700;
    letter-spacing:2px;
    font-size:14px;
    color:var(--amber);
    margin-right:14px;
  }
  .tabbar-credit{
    margin-left:auto;
    font-size:10.5px;
    letter-spacing:1px;
    color:var(--text-dim);
    padding:10px 4px;
  }
  .tabbtn{
    background:transparent;
    border:1px solid transparent;
    border-bottom:none;
    color:var(--text-dim);
    font-family:'IBM Plex Mono', monospace;
    font-size:12px;
    letter-spacing:1px;
    text-transform:uppercase;
    padding:10px 18px;
    cursor:pointer;
    border-radius:6px 6px 0 0;
  }
  .tabbtn:hover{color:var(--text);}
  .tabbtn.active{
    color:var(--amber);
    background:var(--bg);
    border-color:var(--panel-border);
    border-bottom:1px solid var(--bg);
    margin-bottom:-1px;
  }

  .panel{display:none; flex-direction:column; flex:1; min-height:0;}
  .panel.active{display:flex;}

  .header{
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding:14px 22px;
    border-bottom:1px solid var(--panel-border);
    background:linear-gradient(180deg, #0b120d 0%, #070b09 100%);
    flex-wrap:wrap;
    gap:10px;
  }
  .brand{display:flex; align-items:baseline; gap:12px;}
  .brand .ident{
    font-family:'Space Grotesk', sans-serif;
    font-weight:700;
    font-size:28px;
    letter-spacing:2px;
    color:var(--amber);
    text-shadow:0 0 18px rgba(255,181,69,0.35);
  }
  .brand .sub{
    font-size:11px;
    letter-spacing:3px;
    color:var(--text-dim);
    text-transform:uppercase;
  }
  .headerstats{
    display:flex;
    gap:22px;
    font-size:12px;
    color:var(--text-dim);
    align-items:center;
    flex-wrap:wrap;
  }
  .headerstats b{color:var(--sweep); font-weight:600;}
  .headerstats .clock{
    font-family:'IBM Plex Mono',monospace;
    font-size:18px;
    color:var(--cyan);
    letter-spacing:1px;
  }
  .dot{
    width:8px;height:8px;border-radius:50%;
    display:inline-block;margin-right:6px;
    background:var(--sweep);
    box-shadow:0 0 8px var(--sweep);
    animation:blink 1.6s ease-in-out infinite;
  }
  .dot.off{background:var(--danger); box-shadow:0 0 8px var(--danger); animation:none;}
  .dot.neutral{background:var(--text-dim); box-shadow:none; animation:none;}
  @keyframes blink{0%,100%{opacity:1;}50%{opacity:.25;}}

  .metarbar{
    display:flex;
    align-items:center;
    gap:12px;
    padding:8px 22px;
    border-bottom:1px solid var(--panel-border);
    background:#080d0a;
    font-size:12px;
    flex-wrap:wrap;
  }
  .metar-label{font-size:10px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase;}
  .metar-badge{
    padding:2px 10px; border-radius:3px; font-weight:700; font-size:11px; letter-spacing:1px;
    border:1px solid var(--panel-border); color:var(--text-dim);
  }
  .metar-badge.vfr{color:#34d17a; border-color:#34d17a; background:rgba(52,209,122,.12);}
  .metar-badge.mvfr{color:#4aa8ff; border-color:#4aa8ff; background:rgba(74,168,255,.12);}
  .metar-badge.ifr{color:#ff5d5d; border-color:#ff5d5d; background:rgba(255,93,93,.12);}
  .metar-badge.lifr{color:#ff5df0; border-color:#ff5df0; background:rgba(255,93,240,.12);}
  .metar-badge.na{color:var(--text-dim); background:rgba(255,255,255,.03);}
  .metar-text{color:var(--text-dim); font-family:'IBM Plex Mono',monospace;}

  .main{
    flex:1;
    display:grid;
    grid-template-columns: minmax(300px, 460px) 1fr;
    gap:0;
    min-height:0;
  }
  @media (max-width: 880px){
    .main{grid-template-columns:1fr;}
  }

  .scope-wrap{
    border-right:1px solid var(--panel-border);
    display:flex;
    flex-direction:column;
    align-items:center;
    padding:18px;
    background:#060a08;
    overflow:auto;
  }

  .rangelist{
    width:100%;
    max-width:460px;
    font-size:11px;
  }
  .rangelist-title{color:var(--text-dim); letter-spacing:2px; font-size:10px; text-transform:uppercase; margin-bottom:6px;}
  .rangelist table{width:100%; border-collapse:collapse;}
  .rangelist th{
    text-align:left; color:var(--text-dim); font-weight:500; font-size:10px;
    border-bottom:1px solid var(--grid-dim); padding:4px 6px;
  }
  .rangelist td{padding:4px 6px; border-bottom:1px solid #0f1a13; color:var(--text-dim); cursor:pointer;}
  .rangelist tr:hover td{background:#0d1712;}
  .rangelist tr.active td{color:var(--cyan); background:#0c1a17;}
  .rangelist td.mono{font-variant-numeric:tabular-nums;}

  .board{
    padding:26px 30px;
    display:flex;
    flex-direction:column;
    gap:18px;
    overflow:auto;
  }
  .board-top{
    display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;
  }
  .board-top .label{font-size:11px; letter-spacing:3px; color:var(--text-dim); text-transform:uppercase;}
  .board-top-right{display:flex; align-items:center; gap:12px;}
  .resume-btn{
    background:rgba(52,209,122,.12);
    border:1px solid var(--sweep);
    color:var(--sweep);
    font-family:'IBM Plex Mono',monospace;
    font-size:11px;
    letter-spacing:1px;
    text-transform:uppercase;
    padding:6px 12px;
    border-radius:4px;
    cursor:pointer;
    display:inline-flex;
    align-items:center;
    gap:6px;
  }
  .resume-btn:hover{background:rgba(52,209,122,.22);}

  .fleet-table-wrap{overflow-x:auto;}
  .fleet-table{width:100%; border-collapse:collapse; font-size:12.5px;}
  .fleet-table th{
    text-align:left; color:var(--text-dim); font-weight:500; font-size:10.5px;
    text-transform:uppercase; letter-spacing:1px;
    border-bottom:1px solid var(--grid-dim); padding:8px 10px;
  }
  .fleet-table td{padding:8px 10px; border-bottom:1px solid #0f1a13; vertical-align:top;}
  .fleet-row.alert td{background:rgba(255,93,93,.08); color:#ffb3b3;}
  .fleet-status{display:inline-flex; align-items:center; gap:6px; font-size:11px; letter-spacing:.5px;}
  .fleet-badge-alert{
    display:inline-block; padding:2px 8px; border-radius:3px; font-size:10.5px; font-weight:700;
    color:var(--danger); border:1px solid var(--danger); background:rgba(255,93,93,.12);
    cursor:help;
  }
  .fleet-note{margin-top:18px; font-size:11px; color:var(--text-dim); line-height:1.6; max-width:900px;}
  .fleet-note code{color:var(--text);}

  .logbook-section{margin-top:34px; padding-top:22px; border-top:1px solid var(--panel-border);}
  .logbook-select{
    background:#0c1610; border:1px solid var(--panel-border); color:var(--text);
    font-family:'IBM Plex Mono',monospace; font-size:11px; padding:6px 10px; border-radius:4px;
  }
  .logbook-row.incomplete td{background:rgba(255,181,69,.08); color:var(--amber);}
  .logbook-pilot-input{
    background:transparent; border:1px solid var(--panel-border); color:var(--text);
    font-family:'IBM Plex Mono',monospace; font-size:12px; padding:4px 6px; border-radius:3px;
    width:120px;
  }
  .logbook-pilot-input:focus{border-color:var(--sweep); outline:none;}

  .progress-track{
    width:180px; height:3px; background:var(--grid-dim); border-radius:2px; overflow:hidden;
  }
  .progress-fill{height:100%; background:var(--sweep); width:0%; transition:width .2s linear;}

  .datablock{
    border:1px solid var(--panel-border);
    background:linear-gradient(180deg, #0c1610 0%, #080f0b 100%);
    border-radius:6px;
    padding:26px 30px;
    position:relative;
    min-height:260px;
  }
  .datablock::before{
    content:"";
    position:absolute; inset:0;
    background:repeating-linear-gradient(180deg, rgba(52,209,122,0.025) 0px, rgba(52,209,122,0.025) 1px, transparent 1px, transparent 3px);
    pointer-events:none; border-radius:6px;
  }
  .db-top{display:flex; justify-content:space-between; align-items:flex-start; flex-wrap:wrap; gap:16px;}
  .db-callsign{
    font-family:'Space Grotesk', sans-serif;
    font-weight:700;
    font-size:44px;
    letter-spacing:1px;
    color:var(--amber);
    text-shadow:0 0 22px rgba(255,181,69,.25);
    line-height:1;
  }
  .db-tail{font-size:14px; color:var(--text-dim); margin-top:6px;}
  .db-distance{
    text-align:right;
  }
  .db-distance .val{font-size:32px; color:var(--cyan); font-weight:600;}
  .db-distance .unit{font-size:12px; color:var(--text-dim);}
  .db-emergency{
    display:inline-block; margin-top:6px; padding:3px 10px; border-radius:3px;
    background:rgba(255,93,93,.15); border:1px solid var(--danger); color:var(--danger);
    font-size:11px; letter-spacing:1px; animation:blink 1s infinite;
  }

  .db-grid{
    margin-top:26px;
    display:grid;
    grid-template-columns:repeat(auto-fit, minmax(130px,1fr));
    gap:18px 20px;
  }
  .db-field .k{font-size:10px; letter-spacing:1.5px; color:var(--text-dim); text-transform:uppercase; margin-bottom:4px;}
  .db-field .v{font-size:22px; color:var(--text); font-variant-numeric:tabular-nums;}
  .db-field .v small{font-size:12px; color:var(--text-dim); margin-left:3px;}
  .v.up{color:var(--sweep);}
  .v.down{color:#ff9e6a;}
  .v.route{font-size:15px; line-height:1.3;}
  .v.route.probable{color:var(--amber);}
  .db-field.wide{grid-column:1 / -1;}

  .empty-state{
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    height:100%; min-height:220px; color:var(--text-dim); gap:10px; text-align:center;
  }
  .empty-state .big{font-size:16px; color:var(--text); letter-spacing:1px;}

  .banner{
    border:1px solid var(--amber-dim);
    background:rgba(255,181,69,.06);
    color:var(--amber);
    padding:10px 14px;
    border-radius:5px;
    font-size:12px;
    line-height:1.5;
  }
  .banner.err{border-color:var(--danger); background:rgba(255,93,93,.07); color:#ffb3b3;}

  .footer{
    padding:10px 22px;
    border-top:1px solid var(--panel-border);
    font-size:10.5px;
    color:var(--text-dim);
    display:flex; justify-content:space-between; flex-wrap:wrap; gap:6px;
  }
  .footer a{color:var(--text-dim);}

  ::-webkit-scrollbar{width:8px;}
  ::-webkit-scrollbar-thumb{background:var(--grid); border-radius:4px;}
</style>
</head>
<body>

<div class="tabbar">
  <span class="tabbar-brand">AIRCRAFT RADAR</span>
  <button class="tabbtn active" data-target="panel-lkku">LKKU Radar</button>
  <button class="tabbtn" data-target="panel-bristell">Bristell Radar</button>
  <button class="tabbtn" data-target="panel-brm">BRM Aero Fleet</button>
  <span class="tabbar-credit">Created by Petr Mitáš</span>
</div>

<div class="panel active" id="panel-lkku">
  <div class="header">
    <div class="brand">
      <span class="ident">LKKU · KUNOVICE</span>
      <span class="sub">Aircraft Watch — 30 NM / 55.6 km</span>
    </div>
    <div class="headerstats">
      <span><span class="dot" id="lkku-statusDot"></span><span id="lkku-statusText">Connecting…</span></span>
      <span>In range: <b id="lkku-countInRange">–</b></span>
      <span>Updated: <b id="lkku-lastUpdate">–</b></span>
      <span class="clock">--:--:--</span>
    </div>
  </div>

  <div class="metarbar">
    <span class="metar-label">METAR LKKU</span>
    <span class="metar-badge na" id="metarBadge">–</span>
    <span class="metar-text" id="metarText">Loading…</span>
  </div>

  <div class="main">
    <div class="scope-wrap">
      <div class="rangelist">
        <div class="rangelist-title">Aircraft in range (sorted by distance) · click to pin</div>
        <table>
          <thead>
            <tr><th>#</th><th>Tail</th><th>Call</th><th>Type</th><th>NM</th><th>FL</th></tr>
          </thead>
          <tbody id="lkku-rangeTableBody">
            <tr><td colspan="6" style="color:var(--text-dim); padding:10px 6px;">Waiting for data…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="board">
      <div id="lkku-bannerSlot"></div>

      <div class="board-top">
        <div class="label" id="lkku-boardLabel">Displayed aircraft · –</div>
        <div class="board-top-right">
          <button class="resume-btn" id="lkku-resumeBtn" style="display:none;">▶ Resume 10s rotation</button>
          <div class="progress-track" id="lkku-progressTrack"><div class="progress-fill" id="lkku-progressFill"></div></div>
        </div>
      </div>

      <div class="datablock" id="lkku-datablock">
        <div class="empty-state">
          <div class="big">No aircraft within 30 NM of LKKU</div>
          <div>Scan continuing…</div>
        </div>
      </div>
    </div>
  </div>

  <div class="footer">
    <span>Data source: <a href="https://adsb.fi" target="_blank">adsb.fi</a> (via the local server on your computer) — community ADS-B / MLAT network. Not for navigation.</span>
    <span id="lkku-debugInfo">–</span>
    <span><a href="#" id="lkku-toggleRaw">show raw data from last response</a></span>
  </div>
  <pre id="lkku-rawDump" style="display:none; max-height:260px; overflow:auto; margin:0 22px 14px; padding:12px; background:#050807; border:1px solid var(--panel-border); font-size:10px; color:var(--text-dim); white-space:pre-wrap;"></pre>
</div>

<div class="panel" id="panel-bristell">
  <div class="header">
    <div class="brand">
      <span class="ident">BRISTELL · WORLDWIDE</span>
      <span class="sub">All Bristell aircraft currently airborne</span>
    </div>
    <div class="headerstats">
      <span><span class="dot" id="bristell-statusDot"></span><span id="bristell-statusText">Connecting…</span></span>
      <span>Airborne: <b id="bristell-countInRange">–</b></span>
      <span>Updated: <b id="bristell-lastUpdate">–</b></span>
      <span class="clock">--:--:--</span>
    </div>
  </div>

  <div class="main">
    <div class="scope-wrap">
      <div class="rangelist">
        <div class="rangelist-title">Bristell fleet worldwide (sorted by distance from LKKU) · click to pin</div>
        <table>
          <thead>
            <tr><th>#</th><th>Tail</th><th>Call</th><th>Type</th><th>NM</th><th>FL</th><th>Flag</th></tr>
          </thead>
          <tbody id="bristell-rangeTableBody">
            <tr><td colspan="7" style="color:var(--text-dim); padding:10px 6px;">Waiting for data…</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="board">
      <div id="bristell-bannerSlot"></div>

      <div class="board-top">
        <div class="label" id="bristell-boardLabel">Displayed aircraft · –</div>
        <div class="board-top-right">
          <button class="resume-btn" id="bristell-resumeBtn" style="display:none;">▶ Resume 10s rotation</button>
          <div class="progress-track" id="bristell-progressTrack"><div class="progress-fill" id="bristell-progressFill"></div></div>
        </div>
      </div>

      <div class="datablock" id="bristell-datablock">
        <div class="empty-state">
          <div class="big">No Bristell aircraft airborne right now</div>
          <div>Scan continuing…</div>
        </div>
      </div>
    </div>
  </div>

  <div class="footer">
    <span>Data source: <a href="https://adsb.lol" target="_blank">adsb.lol</a> (via the local server on your computer) — community ADS-B / MLAT network. Country lookup via bigdatacloud.net. Not for navigation.</span>
    <span id="bristell-debugInfo">–</span>
    <span><a href="#" id="bristell-toggleRaw">show raw data from last response</a></span>
  </div>
  <pre id="bristell-rawDump" style="display:none; max-height:260px; overflow:auto; margin:0 22px 14px; padding:12px; background:#050807; border:1px solid var(--panel-border); font-size:10px; color:var(--text-dim); white-space:pre-wrap;"></pre>
</div>

<div class="panel" id="panel-brm">
  <div class="header">
    <div class="brand">
      <span class="ident">BRM AERO · FLEET</span>
      <span class="sub">Company aircraft &amp; ferry marks</span>
    </div>
    <div class="headerstats">
      <span><span class="dot" id="brm-statusDot"></span><span id="brm-statusText">Connecting…</span></span>
      <span>Airborne now: <b id="brm-airborneCount">–</b></span>
      <span>Updated: <b id="brm-lastUpdate">–</b></span>
      <span class="clock">--:--:--</span>
    </div>
  </div>

  <div class="board">
    <div id="brm-bannerSlot"></div>

    <div class="board-top">
      <div class="label">Tracked registrations · <span id="brm-count">–</span></div>
      <button class="resume-btn" id="brm-refreshBtn">↻ Refresh now</button>
    </div>

    <div class="fleet-table-wrap">
      <table class="fleet-table">
        <thead>
          <tr><th>#</th><th>Type</th><th>Registration</th><th>Status</th><th>Last seen on</th><th>Location</th><th>Alert</th></tr>
        </thead>
        <tbody id="brm-fleetTableBody">
          <tr><td colspan="7" style="color:var(--text-dim); padding:10px 6px;">Loading…</td></tr>
        </tbody>
      </table>
    </div>

    <div class="fleet-note">
      Watching 7 registrations, including ferry / test-flight marks (OK-DUI90, OK-QUU06, OK-VAU99, D-MZYW) used during
      factory test flights before delivery. Polled automatically every 5 minutes while this app is running and logged
      to <code>brm_fleet_log.json</code> next to the script, so history survives restarts. If a registration is ever
      seen on two different transponders (hex codes) — including at the exact same moment — the row below is flagged,
      which usually means a customer forgot to reprogram the transponder after taking delivery.
    </div>

    <div class="logbook-section">
      <div class="board-top">
        <div class="label">Flight logbook</div>
        <div class="board-top-right">
          <select id="logbook-regFilter" class="logbook-select">
            <option value="">All registrations</option>
          </select>
          <a class="resume-btn" id="logbook-exportBtn" href="/api/brm-fleet/logbook/export" download>⬇ Export to Excel (CSV)</a>
        </div>
      </div>
      <div class="fleet-table-wrap">
        <table class="fleet-table">
          <thead>
            <tr>
              <th>Date</th><th>Registration</th><th>Departure</th><th>Destination</th>
              <th>Takeoff</th><th>Landing</th><th>Duration</th><th>Pilot</th>
            </tr>
          </thead>
          <tbody id="logbook-tableBody">
            <tr><td colspan="8" style="color:var(--text-dim); padding:10px 6px;">Loading…</td></tr>
          </tbody>
        </table>
      </div>
      <div class="fleet-note">
        Flights are only recorded from the moment this app starts watching a registration — nothing is backfilled
        from before. Rows in yellow mean the signal was only partially caught (takeoff and/or landing not
        confirmed); hover the row for details.
      </div>
    </div>
  </div>
</div>

<script>
const LKKU = { lat: 49.0294, lon: 17.4397 };
const DEFAULT_ROTATE_MS = 10000;

function toRad(d){ return d * Math.PI / 180; }

function haversineKm(lat1, lon1, lat2, lon2){
  const R = 6371;
  const dLat = toRad(lat2-lat1);
  const dLon = toRad(lon2-lon1);
  const a = Math.sin(dLat/2)**2 + Math.cos(toRad(lat1))*Math.cos(toRad(lat2))*Math.sin(dLon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}
function fmt(val, digits=0, suffix=""){
  if(val === null || val === undefined || val === "" || Number.isNaN(val)) return "—";
  if(typeof val === "number") return val.toFixed(digits) + suffix;
  return String(val) + suffix;
}
function flagEmoji(countryCode){
  if(!countryCode || countryCode.length !== 2) return "";
  const A = 0x1F1E6;
  return [...countryCode.toUpperCase()].map(c => String.fromCodePoint(A + (c.charCodeAt(0) - 65))).join("");
}

// callsign -> { status: 'loading'|'ok'|'none', data, subscribers: Set<fn> }
let routeCache = {};
function ensureRoute(callsign, onResolved){
  const key = (callsign || "").trim().toUpperCase();
  if(!key || key === "—") return;
  let entry = routeCache[key];
  if(entry){
    if(entry.status === 'loading' && onResolved) entry.subscribers.add(onResolved);
    return;
  }
  entry = { status: 'loading', data: null, subscribers: new Set(onResolved ? [onResolved] : []) };
  routeCache[key] = entry;
  fetch('/api/route?callsign=' + encodeURIComponent(key))
    .then(res => res.json())
    .then(data => {
      entry.status = data.route ? 'ok' : 'none';
      entry.data = data.route || null;
    })
    .catch(() => {
      entry.status = 'none';
      entry.data = null;
    })
    .finally(() => {
      entry.subscribers.forEach(fn => { try{ fn(); }catch(e){} });
      entry.subscribers.clear();
    });
}

// registration -> { status: 'loading'|'ok'|'none', data, subscribers: Set<fn> }
let operatorCache = {};
function ensureOperator(registration, onResolved){
  const key = (registration || "").trim().toUpperCase();
  if(!key || key === "—") return;
  let entry = operatorCache[key];
  if(entry){
    if(entry.status === 'loading' && onResolved) entry.subscribers.add(onResolved);
    return;
  }
  entry = { status: 'loading', data: null, subscribers: new Set(onResolved ? [onResolved] : []) };
  operatorCache[key] = entry;
  fetch('/api/operator?reg=' + encodeURIComponent(key))
    .then(res => res.json())
    .then(data => {
      entry.status = data.operator ? 'ok' : 'none';
      entry.data = data.operator || null;
    })
    .catch(() => {
      entry.status = 'none';
      entry.data = null;
    })
    .finally(() => {
      entry.subscribers.forEach(fn => { try{ fn(); }catch(e){} });
      entry.subscribers.clear();
    });
}

async function fetchMetar(){
  const badge = document.getElementById('metarBadge');
  const text = document.getElementById('metarText');
  try{
    const res = await fetch('/api/metar', { cache: "no-store" });
    const data = await res.json();
    if(!data.available || !data.raw){
      badge.textContent = "N/A";
      badge.className = "metar-badge na";
      text.textContent = "NOT AVAILABLE";
      return;
    }
    const cat = data.category || "N/A";
    badge.textContent = cat;
    badge.className = "metar-badge " + cat.toLowerCase();
    const age = data.age_minutes !== null ? `${Math.round(data.age_minutes)} min ago` : "";
    text.textContent = `${data.raw}${age ? " · " + age : ""}`;
  }catch(e){
    badge.textContent = "N/A";
    badge.className = "metar-badge na";
    text.textContent = "NOT AVAILABLE";
  }
}

function tickClock(){
  const now = new Date().toLocaleTimeString('en-GB');
  document.querySelectorAll('.clock').forEach(el => el.textContent = now);
  setTimeout(tickClock, 1000);
}

function fmtAge(mins){
  if(mins === null || mins === undefined) return "";
  if(mins < 1) return "just now";
  if(mins < 60) return `${Math.round(mins)} min ago`;
  const hrs = mins / 60;
  if(hrs < 48) return `${hrs.toFixed(1)} h ago`;
  return `${(hrs/24).toFixed(1)} d ago`;
}

async function fetchBrmFleet(force){
  const statusDot = document.getElementById('brm-statusDot');
  const statusText = document.getElementById('brm-statusText');
  const banner = document.getElementById('brm-bannerSlot');
  const refreshBtn = document.getElementById('brm-refreshBtn');
  try{
    if(force) refreshBtn.disabled = true;
    const url = '/api/brm-fleet' + (force ? '?force=1' : '');
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json();
    statusDot.classList.remove('off');
    statusText.textContent = "Live";
    banner.innerHTML = "";

    const fleet = data.fleet || [];
    document.getElementById('brm-count').textContent = fleet.length;
    document.getElementById('brm-airborneCount').textContent = fleet.filter(f => f.currently_airborne).length;
    document.getElementById('brm-lastUpdate').textContent = new Date().toLocaleTimeString('en-GB');

    const regFilter = document.getElementById('logbook-regFilter');
    if(regFilter.options.length <= 1){
      fleet.forEach(f => {
        const opt = document.createElement('option');
        opt.value = f.registration;
        opt.textContent = f.registration;
        regFilter.appendChild(opt);
      });
    }

    const body = document.getElementById('brm-fleetTableBody');
    body.innerHTML = fleet.map((f, i) => {
      let statusHtml;
      if(f.currently_airborne){
        statusHtml = `<span class="fleet-status"><span class="dot"></span>Airborne</span>`;
      }else if(f.last_seen){
        statusHtml = `<span class="fleet-status" style="color:var(--text-dim);"><span class="dot neutral"></span>On ground / last seen</span>`;
      }else{
        statusHtml = `<span style="color:var(--text-dim);">Never seen</span>`;
      }
      const lastSeen = f.last_seen
        ? `${new Date(f.last_seen).toLocaleString('en-GB')}<br><small style="color:var(--text-dim);">${fmtAge(f.age_minutes)}</small>`
        : "—";
      const alertTitle = (f.hexes || []).map(h => `${h.hex} (${h.callsign || '—'}, last seen ${h.last_seen || '—'})`).join(' | ');
      const alertHtml = f.duplicate_alert
        ? `<span class="fleet-badge-alert" title="${alertTitle.replace(/"/g,'&quot;')}">⚠ ${f.known_hex_count} transponders</span>`
        : "";
      return `<tr class="fleet-row ${f.duplicate_alert ? 'alert' : ''}">
        <td class="mono">${i+1}</td>
        <td class="mono">${f.type || "—"}</td>
        <td class="mono">${f.registration}</td>
        <td>${statusHtml}</td>
        <td>${lastSeen}</td>
        <td>${f.location || "—"}</td>
        <td>${alertHtml}</td>
      </tr>`;
    }).join("");
  }catch(err){
    statusDot.classList.add('off');
    statusText.textContent = "Connection lost";
    banner.innerHTML = `<div class="banner err">Could not load fleet data (${err.message}). Check that the lkku_radar_server.py script is still running.</div>`;
  }finally{
    if(force) refreshBtn.disabled = false;
  }
}

async function savePilot(id, pilot, inputEl){
  try{
    const res = await fetch('/api/brm-fleet/logbook/pilot', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, pilot }),
    });
    const data = await res.json();
    inputEl.style.borderColor = data.ok ? 'var(--sweep)' : 'var(--danger)';
    setTimeout(() => { inputEl.style.borderColor = ''; }, 1200);
  }catch(e){
    inputEl.style.borderColor = 'var(--danger)';
  }
}

async function fetchLogbook(){
  const body = document.getElementById('logbook-tableBody');
  if(body.contains(document.activeElement)) return; // don't clobber an in-progress pilot edit
  const reg = document.getElementById('logbook-regFilter').value;
  const exportBtn = document.getElementById('logbook-exportBtn');
  exportBtn.href = '/api/brm-fleet/logbook/export' + (reg ? '?reg=' + encodeURIComponent(reg) : '');

  try{
    const url = '/api/brm-fleet/logbook' + (reg ? '?reg=' + encodeURIComponent(reg) : '');
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json();
    const flights = data.flights || [];

    if(flights.length === 0){
      body.innerHTML = `<tr><td colspan="8" style="color:var(--text-dim); padding:10px 6px;">No flights recorded yet.</td></tr>`;
      return;
    }

    body.innerHTML = flights.map(f => {
      const rowClass = f.incomplete ? 'logbook-row incomplete' : 'logbook-row';
      const titleAttr = f.incomplete ? ` title="${(f.incomplete_note||'').replace(/"/g,'&quot;')}"` : '';
      return `<tr class="${rowClass}"${titleAttr} data-id="${f.id}">
        <td class="mono">${f.date}</td>
        <td class="mono">${f.registration}</td>
        <td>${f.departure}</td>
        <td>${f.destination}</td>
        <td class="mono">${f.takeoff_local}</td>
        <td class="mono">${f.landing_local}</td>
        <td class="mono">${f.duration_str}</td>
        <td><input class="logbook-pilot-input" type="text" value="${(f.pilot||'').replace(/"/g,'&quot;')}" placeholder="Pilot name" data-id="${f.id}"></td>
      </tr>`;
    }).join("");

    body.querySelectorAll('.logbook-pilot-input').forEach(inp => {
      inp.addEventListener('change', () => savePilot(inp.getAttribute('data-id'), inp.value, inp));
    });
  }catch(err){
    body.innerHTML = `<tr><td colspan="8" style="color:var(--danger); padding:10px 6px;">Could not load logbook (${err.message}).</td></tr>`;
  }
}

document.getElementById('logbook-regFilter').addEventListener('change', fetchLogbook);
document.getElementById('brm-refreshBtn').addEventListener('click', () => fetchBrmFleet(true));

document.querySelectorAll('.tabbtn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(btn.getAttribute('data-target')).classList.add('active');
  });
});

function createRadarPanel(cfg){
  function $(name){ return document.getElementById(cfg.prefix + '-' + name); }

  let aircraftList = [];
  let rotateIndex = 0;
  let rotateTimer = null;
  let progressStart = 0;
  let isFrozen = false;
  let frozenHex = null;
  let pinnedAircraft = null;
  let pinnedStale = false;

  function getDisplayed(){
    if(isFrozen && pinnedAircraft) return pinnedAircraft;
    return aircraftList[rotateIndex];
  }

  function setStatus(ok, text){
    $('statusDot').classList.toggle('off', !ok);
    $('statusText').textContent = text;
  }

  function showBanner(html, isErr){
    $('bannerSlot').innerHTML = html ? `<div class="banner ${isErr?'err':''}">${html}</div>` : "";
  }

  async function fetchData(){
    try{
      const res = await fetch(cfg.endpoint, { cache: "no-store" });
      const data = await res.json();
      if(!res.ok) throw new Error(data.error || ("HTTP " + res.status));
      const raw = data.ac || data.aircraft || [];

      let list = raw
        .filter(a => typeof a.lat === "number" && typeof a.lon === "number")
        .map(a => {
          const distKm = haversineKm(LKKU.lat, LKKU.lon, a.lat, a.lon);
          return { ...a, _distKm: distKm, _distNm: distKm / 1.852 };
        });

      if(cfg.radiusNm){
        list = list.filter(a => a._distNm <= cfg.radiusNm);
      }
      list.sort((x,y) => x._distKm - y._distKm);

      aircraftList = list;
      setStatus(true, "Live");
      showBanner("", false);
      $('lastUpdate').textContent = new Date().toLocaleTimeString('en-GB');
      $('countInRange').textContent = aircraftList.length;
      $('debugInfo').textContent = cfg.debugText(raw.length, aircraftList.length);

      window[cfg.prefix + '_lastRaw'] = data;
      const rawDumpEl = $('rawDump');
      if(rawDumpEl.style.display !== 'none'){
        rawDumpEl.textContent = JSON.stringify(data, null, 2);
      }

      if(isFrozen && frozenHex){
        const found = aircraftList.find(a => a.hex === frozenHex);
        if(found){ pinnedAircraft = found; pinnedStale = false; }
        else { pinnedStale = true; }
      }
      if(!isFrozen && rotateIndex >= aircraftList.length) rotateIndex = 0;

      renderTable();
      renderDatablock();
    }catch(err){
      setStatus(false, "Connection lost");
      showBanner(cfg.errorText(err.message), true);
    }
  }

  function renderTable(){
    const body = $('rangeTableBody');
    const colCount = cfg.columns.length;
    if(aircraftList.length === 0){
      body.innerHTML = `<tr><td colspan="${colCount}" style="color:var(--text-dim); padding:10px 6px;">${cfg.emptyTableText}</td></tr>`;
      return;
    }
    const displayed = getDisplayed();
    body.innerHTML = aircraftList.map((a, i) => {
      const isActive = displayed && a.hex === displayed.hex;
      const cells = cfg.columns.map(col => `<td class="mono">${col.render(a, i)}</td>`).join("");
      return `<tr class="${isActive?'active':''}" data-hex="${a.hex}">${cells}</tr>`;
    }).join("");

    body.querySelectorAll('tr[data-hex]').forEach(row => {
      row.addEventListener('click', () => {
        const hex = row.getAttribute('data-hex');
        const idx = aircraftList.findIndex(a => a.hex === hex);
        if(idx >= 0) freeze(hex, idx);
      });
    });
  }

  function freeze(hex, idx){
    isFrozen = true;
    frozenHex = hex;
    pinnedAircraft = aircraftList[idx];
    pinnedStale = false;
    if(rotateTimer){ clearInterval(rotateTimer); rotateTimer = null; }
    renderDatablock();
    renderTable();
  }

  function resume(){
    const displayed = getDisplayed();
    isFrozen = false;
    frozenHex = null;
    pinnedAircraft = null;
    pinnedStale = false;
    if(displayed){
      const idx = aircraftList.findIndex(a => a.hex === displayed.hex);
      rotateIndex = idx >= 0 ? idx : 0;
    }
    restartRotateTimer();
    renderDatablock();
    renderTable();
  }

  function updateFreezeUI(){
    const btn = $('resumeBtn');
    const track = $('progressTrack');
    const label = $('boardLabel');
    if(isFrozen){
      btn.style.display = 'inline-flex';
      track.style.display = 'none';
      const d = getDisplayed();
      const name = d ? (d.flight || d.r || '—').trim() : '—';
      label.textContent = `Pinned · ${name}${pinnedStale ? ' (signal lost, last known data)' : ''}`;
    }else{
      btn.style.display = 'none';
      track.style.display = 'block';
      label.textContent = `Displayed aircraft · ${aircraftList.length ? (rotateIndex+1)+' / '+aircraftList.length : '–'}`;
    }
  }

  function renderDatablock(){
    const el = $('datablock');
    updateFreezeUI();

    const a = getDisplayed();
    if(!a){
      el.innerHTML = `<div class="empty-state">
        <div class="big">${cfg.emptyBig}</div>
        <div>${cfg.emptySmall}</div>
      </div>`;
      return;
    }

    const tail = (a.r || "—").trim();
    const callsign = (a.flight || "—").trim();
    const type = a.t ? a.t : "";
    const altBaro = a.alt_baro === "ground" ? "GND" : fmt(a.alt_baro, 0, " ft");
    const vRate = (a.baro_rate ?? a.geom_rate);
    const vClass = vRate > 100 ? "up" : (vRate < -100 ? "down" : "");
    const vArrow = vRate > 100 ? "▲ " : (vRate < -100 ? "▼ " : "");
    const emergency = a.emergency && a.emergency !== "none";

    const routeKey = callsign !== "—" ? callsign.toUpperCase() : null;
    const routeEntry = routeKey ? routeCache[routeKey] : null;
    let originText = "Unknown", destText = "Unknown";
    let originProbable = false, destProbable = false;
    let routeResolved = false; // did adsbdb actually know a published route for this callsign?
    if(routeEntry){
      if(routeEntry.status === 'loading'){
        originText = "Looking up…"; destText = "Looking up…";
      }else if(routeEntry.status === 'ok' && routeEntry.data){
        const o = routeEntry.data.origin, d = routeEntry.data.destination;
        if(o || d){
          routeResolved = true;
          originText = o ? `${o.icao_code || '—'} / ${o.iata_code || '—'} / ${o.name || 'Unknown'}` : "Unknown";
          destText = d ? `${d.icao_code || '—'} / ${d.iata_code || '—'} / ${d.name || 'Unknown'}` : "Unknown";
        }
      }
    }
    if(routeKey) ensureRoute(routeKey, () => renderDatablock());

    // GA/Bristell callsigns never match a published route (adsbdb only knows scheduled
    // airline routes) — fall back to the nearest airfield to the live position, and
    // only trust a direction (departure vs destination) when low + climbing/descending.
    if(!routeResolved && cfg.useNearestAirportFallback && a.nearest_airport && originText !== "Looking up…"){
      const trendVRate = a.baro_rate ?? a.geom_rate;
      const lowAlt = a.alt_baro === "ground" || (typeof a.alt_baro === "number" && a.alt_baro < 3000);
      if(lowAlt && typeof trendVRate === "number" && trendVRate > 200){
        originText = a.nearest_airport;
        destText = "Unknown";
      }else if(lowAlt && typeof trendVRate === "number" && trendVRate < -200){
        destText = a.nearest_airport;
        originText = "Unknown";
      }else{
        originText = a.nearest_airport + " (probable)";
        destText = a.nearest_airport + " (probable)";
        originProbable = true;
        destProbable = true;
      }
    }

    // Prefer the airline derived from the callsign (e.g. RYR -> Ryanair) — far more
    // reliable than a per-tail owner lookup, and we already fetch it for the route.
    const routeAirline = (routeEntry && routeEntry.status === 'ok' && routeEntry.data) ? routeEntry.data.airline : null;
    let operatorText = "—";
    if(routeAirline && routeAirline.name){
      operatorText = routeAirline.name;
    }else if(routeEntry && routeEntry.status === 'loading'){
      operatorText = "Looking up…";
    }else{
      // fall back to a per-registration owner lookup, mainly useful for GA/private aircraft
      const operatorKey = tail !== "—" ? tail.toUpperCase() : null;
      const operatorEntry = operatorKey ? operatorCache[operatorKey] : null;
      if(operatorEntry){
        if(operatorEntry.status === 'loading') operatorText = "Looking up…";
        else if(operatorEntry.status === 'ok' && operatorEntry.data) operatorText = operatorEntry.data;
        else operatorText = "Unknown";
      }
      if(operatorKey) ensureOperator(operatorKey, () => renderDatablock());
    }

    const countryField = cfg.showCountry
      ? `<div class="db-field"><div class="k">Country</div><div class="v">${flagEmoji(a.country_code) || "🏳"} ${a.country_code || "Unknown"}</div></div>`
      : "";

    el.innerHTML = `
      <div class="db-top">
        <div>
          <div class="db-callsign">${callsign !== "—" ? callsign : tail}</div>
          <div class="db-tail">Tail: ${tail}${a.hex ? " · ICAO24: " + a.hex.toUpperCase() : ""}${pinnedStale ? ' · <span style="color:var(--danger);">signal lost</span>' : ''}</div>
          ${emergency ? `<div class="db-emergency">⚠ EMERGENCY: ${a.emergency.toUpperCase()}</div>` : ""}
        </div>
        <div class="db-distance">
          <div class="val">${a._distNm.toFixed(1)}<span class="unit"> NM</span></div>
          <div class="unit">${a._distKm.toFixed(1)} km from LKKU</div>
        </div>
      </div>
      <div class="db-grid">
        <div class="db-field"><div class="k">Aircraft type</div><div class="v">${type || "Unknown"}</div></div>
        <div class="db-field"><div class="k">Callsign</div><div class="v">${callsign}</div></div>
        <div class="db-field"><div class="k">Tail number</div><div class="v">${tail}</div></div>
        <div class="db-field"><div class="k">Operator</div><div class="v" style="font-size:16px;">${operatorText}</div></div>
        <div class="db-field"><div class="k">Altitude</div><div class="v">${altBaro}</div></div>
        <div class="db-field"><div class="k">Vertical speed</div><div class="v ${vClass}">${vArrow}${fmt(vRate,0," fpm")}</div></div>
        <div class="db-field"><div class="k">True airspeed</div><div class="v">${fmt(a.tas,0," kts")}</div></div>
        <div class="db-field"><div class="k">Groundspeed</div><div class="v">${fmt(a.gs,0," kts")}</div></div>
        <div class="db-field"><div class="k">Heading</div><div class="v">${fmt(a.track ?? a.true_heading ?? a.mag_heading,0,"°")}</div></div>
        <div class="db-field"><div class="k">Squawk</div><div class="v">${fmt(a.squawk)}</div></div>
        ${countryField}
        <div class="db-field wide"><div class="k">Departure</div><div class="v route${originProbable ? ' probable' : ''}">${originText}</div></div>
        <div class="db-field wide"><div class="k">Destination</div><div class="v route${destProbable ? ' probable' : ''}">${destText}</div></div>
      </div>
    `;
  }

  function restartRotateTimer(){
    progressStart = Date.now();
    if(rotateTimer) clearInterval(rotateTimer);
    rotateTimer = setInterval(() => {
      if(!isFrozen && aircraftList.length > 0){
        rotateIndex = (rotateIndex + 1) % aircraftList.length;
        renderDatablock();
        renderTable();
      }
      progressStart = Date.now();
    }, cfg.rotateMs);
  }

  function tickProgress(){
    const el = $('progressFill');
    if(!isFrozen){
      const pct = Math.min(100, ((Date.now() - progressStart) / cfg.rotateMs) * 100);
      el.style.width = pct + "%";
    }
    requestAnimationFrame(tickProgress);
  }

  function init(){
    $('toggleRaw').addEventListener('click', (e) => {
      e.preventDefault();
      const el = $('rawDump');
      const show = el.style.display === 'none';
      el.style.display = show ? 'block' : 'none';
      e.target.textContent = show ? 'hide raw data' : 'show raw data from last response';
      if(show && window[cfg.prefix + '_lastRaw']){
        el.textContent = JSON.stringify(window[cfg.prefix + '_lastRaw'], null, 2);
      }
    });
    $('resumeBtn').addEventListener('click', resume);

    fetchData();
    setInterval(fetchData, cfg.fetchMs);
    restartRotateTimer();
    tickProgress();
  }

  return { init };
}

const lkkuPanel = createRadarPanel({
  prefix: 'lkku',
  endpoint: '/api/aircraft',
  radiusNm: 30,
  rotateMs: DEFAULT_ROTATE_MS,
  fetchMs: 8000,
  showCountry: false,
  columns: [
    { render: (a, i) => i + 1 },
    { render: a => (a.r||'—').trim() },
    { render: a => (a.flight||'—').trim() },
    { render: a => (a.t||'—').trim() },
    { render: a => a._distNm.toFixed(1) },
    { render: a => (a.alt_baro === "ground") ? "GND" : (a.alt_baro ? Math.round(a.alt_baro/100) : "—") },
  ],
  emptyTableText: "No aircraft in range",
  emptyBig: "No aircraft within 30 NM of LKKU",
  emptySmall: "Scan continuing…",
  debugText: (total, filtered) => `API returned ${total} aircraft total · after filtering to ≤30 NM, ${filtered} remain`,
  errorText: (msg) => "Could not load data from the local server (" + msg + "). " +
    "Check that the lkku_radar_server.py script is still running in the terminal and that you have a working internet connection. " +
    "If you closed the terminal, run the script again.",
});

const bristellPanel = createRadarPanel({
  prefix: 'bristell',
  endpoint: '/api/bristell',
  radiusNm: null,
  rotateMs: DEFAULT_ROTATE_MS,
  fetchMs: 20000,
  showCountry: true,
  useNearestAirportFallback: true,
  columns: [
    { render: (a, i) => i + 1 },
    { render: a => (a.r||'—').trim() },
    { render: a => (a.flight||'—').trim() },
    { render: a => (a.t||'—').trim() },
    { render: a => a._distNm.toFixed(1) },
    { render: a => (a.alt_baro === "ground") ? "GND" : (a.alt_baro ? Math.round(a.alt_baro/100) : "—") },
    { render: a => flagEmoji(a.country_code) || "—" },
  ],
  emptyTableText: "No Bristell aircraft airborne",
  emptyBig: "No Bristell aircraft airborne right now",
  emptySmall: "Scan continuing…",
  debugText: (total, filtered) => `API returned ${total} Bristell aircraft worldwide (types BR23/NG5/BR8/B23E) · ${filtered} with a valid position`,
  errorText: (msg) => "Could not load data from the local server (" + msg + "). " +
    "Check that the lkku_radar_server.py script is still running in the terminal and that you have a working internet connection. " +
    "If you closed the terminal, run the script again.",
});

// init
fetchMetar();
setInterval(fetchMetar, 5 * 60 * 1000);
tickClock();
lkkuPanel.init();
bristellPanel.init();
fetchBrmFleet();
setInterval(fetchBrmFleet, 60000);
fetchLogbook();
setInterval(fetchLogbook, 60000);
</script>
</body>
</html>
"""


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Keep the terminal quiet; comment this out if you want request logs.
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/aircraft"):
            self.handle_api()
        elif self.path.startswith("/api/route"):
            self.handle_route()
        elif self.path.startswith("/api/operator"):
            self.handle_operator()
        elif self.path.startswith("/api/metar"):
            self.handle_metar()
        elif self.path.startswith("/api/bristell"):
            self.handle_bristell()
        elif self.path.startswith("/api/brm-fleet/logbook/export"):
            self.handle_logbook_export()
        elif self.path.startswith("/api/brm-fleet/logbook"):
            self.handle_logbook()
        elif self.path.startswith("/api/brm-fleet"):
            self.handle_brm_fleet()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path.startswith("/api/brm-fleet/logbook/pilot"):
            self.handle_logbook_pilot_update()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_api(self):
        req = urllib.request.Request(
            API_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LKKU-Radar-Local/1.0)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as e:
            err_body = json.dumps({"error": f"HTTP {e.code} od opendata.adsb.fi"}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:
            err_body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)

    def handle_route(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        callsign = (qs.get("callsign") or [""])[0]
        route = fetch_route(callsign)
        body = json.dumps({"route": route}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_operator(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        reg = (qs.get("reg") or [""])[0]
        operator = fetch_operator(reg)
        body = json.dumps({"operator": operator}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_metar(self):
        result = fetch_metar()
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_bristell(self):
        try:
            result = fetch_bristell()
        except Exception as e:
            err_body = json.dumps({"error": str(e), "ac": []}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
            return
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_brm_fleet(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        force = (qs.get("force") or ["0"])[0] == "1"
        if force:
            try:
                poll_fleet_once()
            except Exception:
                pass
        result = build_fleet_status()
        body = json.dumps(result).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_logbook(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        reg = (qs.get("reg") or [""])[0].strip().upper() or None
        flights = build_logbook(reg)
        body = json.dumps({"flights": flights}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_logbook_export(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        reg = (qs.get("reg") or [""])[0].strip().upper() or None
        csv_text = export_logbook_csv(reg)
        body = csv_text.encode("utf-8")
        filename = f"brm_logbook_{reg}.csv" if reg else "brm_logbook.csv"
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_logbook_pilot_update(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            payload = {}
        ok = update_flight_pilot(payload.get("id"), payload.get("pilot"))
        body = json.dumps({"ok": ok}).encode("utf-8")
        self.send_response(200 if ok else 404)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"Nepodařilo se spustit server na portu {PORT}: {e}")
        print("Možná appka už v jiném okně terminálu běží — zkus otevřít http://127.0.0.1:8765/ v prohlížeči.")
        sys.exit(1)

    url = f"http://127.0.0.1:{PORT}/"
    print("=" * 50)
    print(" BRM Aero Aircraft Watch")
    print(f" Otevři v prohlížeči: {url}")
    print(" Pro ukončení stiskni Ctrl+C")
    print("=" * 50)

    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_fleet_poll_loop, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nUkončuji server…")
        httpd.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
