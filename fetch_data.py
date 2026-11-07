#!/usr/bin/env python3
"""
Helsinki court finder - data fetcher.

Reads the halls to check from halls.json, fetches their free courts and writes
one data.json that the web page (index.html) reads.

    python3 fetch_data.py                       # writes site/data.json
    python3 fetch_data.py --out data.json       # choose the output file
    python3 fetch_data.py --print 2026-11-09    # print one day to the terminal
    python3 fetch_data.py --halls other.json    # use another hall list

Needs only Python 3.9+, no installs.

To add a hall on a booking system we already support (Urheiluhallit, Cintoia),
edit only halls.json. A new booking system needs a fetch_<system> function
here and an entry in FETCHERS.

Exit code is 1 when no hall could be read at all, so an automatic job can
keep serving the previous data instead of publishing an empty page.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    HELSINKI = ZoneInfo("Europe/Helsinki")
except Exception:  # very old Python / missing tz database
    HELSINKI = timezone(timedelta(hours=2))

UA = "helsinki-badminton-finder/0.1"
DAYS_AHEAD = 14  # today + 13 more days
PAUSE = 0.25  # seconds between requests, to stay gentle on the venues
HERE = os.path.dirname(os.path.abspath(__file__))

# Hall fields that are safe and useful to publish in data.json.
PUBLIC_HALL_FIELDS = (
    "id", "name", "area", "address", "lat", "lon", "phone", "photos", "description",
    "opening_hours", "opening_exceptions", "amenities", "booking_rules",
    "system", "book_url", "benefits",
)


# ------------------------------------------------------------------ Urheiluhallit
UH_URL = (
    "https://kauppa.urheiluhallit.fi/webshop/reservations2/apiresourceslots"
    "?date={start}--{end}&service_id={service}&location_ids={locations}"
    "&resource_ids={resources}&tag_ids="
)


def uh_parse(slots, day, res_to_hall, sport, links=None):
    """Urheiluhallit slot list -> rows for one day.

    Free courts = resource_ids - resource_ids_reserved
    (the built-in free / free_count fields are unreliable).
    links: hall id -> booking link with a {monday} placeholder; their
    calendar opens on a Monday, so each row links to the week of its day.
    """
    links = links or {}
    monday = (day - timedelta(days=day.weekday())).isoformat()
    rows = []
    for s in slots:
        start = datetime.strptime(s["time_start"], "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(s["time_end"], "%Y-%m-%d %H:%M:%S")
        if start.date() != day:
            continue
        reserved = set(s.get("resource_ids_reserved") or [])
        free = [r for r in (s.get("resource_ids") or []) if r not in reserved]
        by_hall = {}
        for r in free:
            hall = res_to_hall.get(r)
            if hall:
                by_hall.setdefault(hall, []).append(r)
        price = s.get("prices")
        price_eur = price / 100 if isinstance(price, (int, float)) else None
        for hall, ids in by_hall.items():
            row = {
                "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"),
                "hall": hall,
                "sport": sport,
                "courts": None,  # only counts are known, not court names
                "count": len(ids),
                "price": price_eur,
                "price_max": price_eur,
            }
            if hall in links:
                row["book_url"] = links[hall].format(monday=monday)
            rows.append(row)
    return rows


def fetch_urheiluhallit(halls, system, days):
    """One request per week per sport covers every Urheiluhallit hall."""
    rows_by_day = {d: [] for d in days}
    status = {h["id"]: [True, []] for h in halls}
    mondays = sorted({d - timedelta(days=d.weekday()) for d in days})
    for sport, service in system.get("services", {}).items():
        group = [h for h in halls if sport in h["sports"]]
        if not group:
            continue
        res_to_hall = {r: h["id"] for h in group for r in h["sports"][sport]["resource_ids"]}
        locations = sorted({h["location_id"] for h in group})
        template = system.get("book_link")
        links = {
            h["id"]: template.replace("{service}", str(service)).replace("{location}", str(h["location_id"]))
            for h in group
        } if template else {}
        errors, ok_weeks = [], 0
        for monday in mondays:
            sunday = monday + timedelta(days=6)
            url = UH_URL.format(
                start=monday.isoformat(),
                end=sunday.isoformat(),
                service=service,
                locations="%2C".join(map(str, locations)),
                resources="%2C".join(map(str, res_to_hall)),
            )
            try:
                slots = get_json(url)
                ok_weeks += 1
            except Exception as e:
                errors.append(f"{sport} week {monday}: {type(e).__name__}")
                continue
            for d in days:
                if monday <= d <= sunday:
                    rows_by_day[d] += uh_parse(slots, d, res_to_hall, sport, links)
            time.sleep(PAUSE)
        for h in group:
            status[h["id"]][0] = status[h["id"]][0] and ok_weeks > 0
            status[h["id"]][1] += errors
    return rows_by_day, status


# ------------------------------------------------------------------------ Cintoia
CINTOIA_INDEX = (
    "https://firestore.googleapis.com/v1/projects/{project}/databases/(default)"
    "/documents/freeindex/{index}/indexes/public"
)


def cintoia_file_url(index_doc, day):
    """Firestore index document -> S3 url of that day's file, or None."""
    entry = index_doc.get("fields", {}).get(day.strftime("%Y%m%d"))
    if not entry:
        return None
    return entry["mapValue"]["fields"]["key"]["stringValue"]


def court_sort_key(name):
    """K2 before K10."""
    digits = "".join(c for c in name if c.isdigit())
    return (name.rstrip("0123456789"), int(digits) if digits else 0, name)


def cintoia_parse(day_file, hall_id, court_map):
    """Cintoia day file -> rows. A slot that is listed is bookable; a slot
    that is missing from the file is already taken. Courts not in court_map
    (other sports we don't track) are skipped."""
    by_slot = {}
    for court_id, slots in day_file.items():
        known = court_map.get(court_id)
        if not known:
            continue
        sport, name = known
        for s in slots:
            key = (sport, f"{s['s'][:2]}:{s['s'][2:]}", f"{s['e'][:2]}:{s['e'][2:]}")
            by_slot.setdefault(key, []).append((name, s["p"] / 100))
    rows = []
    for (sport, start, end), courts in by_slot.items():
        courts.sort(key=lambda c: court_sort_key(c[0]))
        prices = [p for _, p in courts]
        rows.append(
            {
                "start": start,
                "end": end,
                "hall": hall_id,
                "sport": sport,
                "courts": [c for c, _ in courts],
                "count": len(courts),
                "price": min(prices),
                "price_max": max(prices),
            }
        )
    return rows


def fetch_cintoia(halls, system, days):
    """Each Cintoia hall has its own public free-slot index."""
    rows_by_day = {d: [] for d in days}
    status = {}
    for h in halls:
        court_map = {
            cid: (sport, name)
            for sport, cfg in h["sports"].items()
            for cid, name in cfg.get("court_ids", {}).items()
        }
        url = CINTOIA_INDEX.format(project=system["firestore_project"], index=h["freeindex"])
        try:
            index = get_json(url)
        except Exception as e:
            status[h["id"]] = [False, [f"index: {type(e).__name__}"]]
            continue
        errors = []
        for d in days:
            try:
                file_url = cintoia_file_url(index, d)
                if file_url:
                    rows_by_day[d] += cintoia_parse(get_json(file_url), h["id"], court_map)
                    time.sleep(PAUSE)
            except Exception as e:
                errors.append(f"{d}: {type(e).__name__}")
        status[h["id"]] = [True, errors]
    return rows_by_day, status


FETCHERS = {
    "urheiluhallit": fetch_urheiluhallit,
    "cintoia": fetch_cintoia,
}


# ------------------------------------------------------------------------ core
def get_json(url, tries=3):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def helsinki_today():
    return datetime.now(HELSINKI).date()


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    problems = []
    for i, h in enumerate(cfg.get("halls", [])):
        label = h.get("id") or f"hall #{i + 1}"
        for key in ("id", "name", "area", "system", "book_url", "sports"):
            if not h.get(key):
                problems.append(f"{label}: missing '{key}'")
        for sport, sc in (h.get("sports") or {}).items():
            if not isinstance(sc.get("courts"), int):
                problems.append(f"{label}: sports.{sport}.courts must be a number")
        if h.get("system") == "urheiluhallit":
            if not isinstance(h.get("location_id"), int):
                problems.append(f"{label}: missing 'location_id'")
            for sport, sc in (h.get("sports") or {}).items():
                if not sc.get("resource_ids"):
                    problems.append(f"{label}: sports.{sport}.resource_ids is empty")
        if h.get("system") == "cintoia":
            if not h.get("freeindex"):
                problems.append(f"{label}: missing 'freeindex'")
            for sport, sc in (h.get("sports") or {}).items():
                if not sc.get("court_ids"):
                    problems.append(f"{label}: sports.{sport}.court_ids is empty")
    ids = [h.get("id") for h in cfg.get("halls", [])]
    problems += [f"duplicate hall id '{i}'" for i in sorted({i for i in ids if i and ids.count(i) > 1})]
    if problems:
        raise SystemExit(f"{path} has problems:\n  " + "\n  ".join(problems))
    return cfg


def public_hall(h, ok, errors):
    out = {k: h.get(k) for k in PUBLIC_HALL_FIELDS}
    # Publish court counts per sport, never the booking systems' internal IDs.
    out["sports"] = {sport: {"courts": cfg.get("courts")} for sport, cfg in h["sports"].items()}
    out["ok"] = ok
    out["error"] = "; ".join(errors) or None
    return out


def build(cfg, days_ahead=DAYS_AHEAD):
    today = helsinki_today()
    days = [today + timedelta(days=i) for i in range(days_ahead)]
    out_days = {d.isoformat(): [] for d in days}
    status = {}
    for sys_id, system in cfg["systems"].items():
        group = [h for h in cfg["halls"] if h["system"] == sys_id]
        if not group:
            continue
        fetch = FETCHERS.get(sys_id)
        if not fetch:
            status.update({h["id"]: [False, [f"no fetcher for system '{sys_id}'"]] for h in group})
            continue
        try:
            rows_by_day, st = fetch(group, system, days)
        except Exception as e:  # never let one system break the others
            rows_by_day, st = {}, {h["id"]: [False, [f"{type(e).__name__}: {e}"]] for h in group}
        status.update(st)
        for d, rows in rows_by_day.items():
            out_days[d.isoformat()] += rows
    for h in cfg["halls"]:  # halls whose system is not listed in "systems"
        status.setdefault(h["id"], [False, [f"unknown system '{h['system']}'"]])
    for rows in out_days.values():
        rows.sort(key=lambda r: (r["start"], r["hall"], r["sport"]))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sports": sorted(cfg.get("sports", []), key=lambda s: s.get("order", 0)),
        "systems": {k: {"name": v.get("name", k)} for k, v in cfg["systems"].items()},
        "halls": [public_hall(h, *status[h["id"]]) for h in cfg["halls"]],
        "days": out_days,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data.json")
    ap.add_argument("--halls", default=os.path.join(HERE, "halls.json"))
    ap.add_argument("--print", dest="print_day", metavar="YYYY-MM-DD")
    a = ap.parse_args()

    data = build(load_config(a.halls))
    names = {h["id"]: h["name"] for h in data["halls"]}
    for h in data["halls"]:
        status = "ok" if h["ok"] else "FAILED"
        print(f"{h['name']}: {status}" + (f" ({h['error']})" if h["error"] else ""), file=sys.stderr)

    if a.print_day:
        rows = data["days"].get(a.print_day, [])
        print(f"Free courts on {a.print_day}\n")
        for r in rows:
            courts = ", ".join(r["courts"]) if r["courts"] else f"{r['count']} free"
            price = f"{r['price']:g} EUR" if r["price"] is not None else "-"
            print(f"{r['start']}-{r['end']}  {names[r['hall']]:<28} {r['sport']:<10} {courts:<40} {price}")
        if not rows:
            print("(nothing found)")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Wrote {a.out}", file=sys.stderr)

    if not any(h["ok"] for h in data["halls"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
