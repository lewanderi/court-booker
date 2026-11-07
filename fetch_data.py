#!/usr/bin/env python3
"""
Helsinki badminton court finder - data fetcher.

Reads free badminton courts from two venues and writes one data.json that the
web page (index.html) reads.

    python3 fetch_data.py                       # writes site/data.json
    python3 fetch_data.py --out data.json       # choose the output file
    python3 fetch_data.py --print 2026-11-09    # print one day to the terminal

Needs only Python 3.9+, no installs.

Exit code is 1 when no venue could be read at all, so an automatic job can
keep serving the previous data instead of publishing an empty page.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo

    HELSINKI = ZoneInfo("Europe/Helsinki")
except Exception:  # very old Python / missing tz database
    HELSINKI = timezone(timedelta(hours=2))

UA = "helsinki-badminton-finder/0.1"
DAYS_AHEAD = 14  # today + 13 more days
PAUSE = 0.25  # seconds between requests, to stay gentle on the venues

# ------------------------------------------------------------------ Urheiluhallit
UH_URL = (
    "https://kauppa.urheiluhallit.fi/webshop/reservations2/apiresourceslots"
    "?date={start}--{end}&service_id=27&location_ids=1%2C2%2C8"
    "&resource_ids=79%2C80%2C81%2C82%2C133%2C134%2C135%2C136%2C137%2C196%2C197"
    "%2C191%2C192%2C193%2C194%2C276%2C277%2C280%2C281&tag_ids="
)
UH_HALLS = {}
for _i in (79, 80, 81, 82):
    UH_HALLS[_i] = "Kallio"
for _i in (191, 192, 193, 194, 196, 197, 276, 277, 280, 281):
    UH_HALLS[_i] = "Pasila"
for _i in (133, 134, 135, 136, 137):
    UH_HALLS[_i] = "Mäkelänrinne"


def uh_parse(slots, day):
    """Urheiluhallit slot list -> rows for one day.

    Free courts = resource_ids - resource_ids_reserved
    (the built-in free / free_count fields are unreliable).
    """
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
            by_hall.setdefault(UH_HALLS.get(r, "Urheiluhallit"), []).append(r)
        price = s.get("prices")
        price_eur = price / 100 if isinstance(price, (int, float)) else None
        for hall, ids in by_hall.items():
            rows.append(
                {
                    "start": start.strftime("%H:%M"),
                    "end": end.strftime("%H:%M"),
                    "venue": "urheiluhallit",
                    "hall": hall,
                    "courts": None,  # only counts are known, not court names
                    "count": len(ids),
                    "price": price_eur,
                    "price_max": price_eur,
                }
            )
    return rows


# --------------------------------------------------------------------- Meilahti
ML_INDEX = (
    "https://firestore.googleapis.com/v1/projects/falcon-328a1/databases/(default)"
    "/documents/freeindex/meilahdenliikuntakeskus-bSZNqwvU/indexes/public"
)
ML_COURTS = {
    "HznSAERtCT8tGijrzlRF": "K1",
    "yWLsipR00zGfPInWCBbU": "K2",
    "ohOwH1w4kllk7lbFUoBV": "K3",
    "IcyqB5GHTA1tgp8vK0yj": "K4",
    "VtDpAMJ2mz4CzPOcUQMc": "K5",
    "GLcw22uVYjOY1dKIQquq": "K6",
    "IymznG33tOpdxNcQCsKM": "K7",
    "0bpxVzkSg03lTIsD1tJ0": "K8",
    "RtUPYMJhxALg97hTVd4R": "K9",
    "AJy3849ho87Gp9SsGg3L": "K10",
}


def ml_file_url(index_doc, day):
    """Firestore index document -> S3 url of that day's file, or None."""
    entry = index_doc.get("fields", {}).get(day.strftime("%Y%m%d"))
    if not entry:
        return None
    return entry["mapValue"]["fields"]["key"]["stringValue"]


def ml_parse(day_file, day):
    """Meilahti day file -> rows. A slot that is listed is bookable; a slot
    that is missing from the file is already taken."""
    by_slot = {}
    for court_id, slots in day_file.items():
        name = ML_COURTS.get(court_id)
        if not name:  # tennis / bubble courts
            continue
        for s in slots:
            key = (f"{s['s'][:2]}:{s['s'][2:]}", f"{s['e'][:2]}:{s['e'][2:]}")
            by_slot.setdefault(key, []).append((name, s["p"] / 100))
    rows = []
    for (start, end), courts in by_slot.items():
        courts.sort(key=lambda c: int(c[0][1:]))
        prices = [p for _, p in courts]
        rows.append(
            {
                "start": start,
                "end": end,
                "venue": "meilahti",
                "hall": "Forza",
                "courts": [c for c, _ in courts],
                "count": len(courts),
                "price": min(prices),
                "price_max": max(prices),
            }
        )
    return rows


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


def fetch_urheiluhallit(days):
    rows_by_day = {d: [] for d in days}
    errors, ok_weeks = [], 0
    mondays = sorted({d - timedelta(days=d.weekday()) for d in days})
    for monday in mondays:
        sunday = monday + timedelta(days=6)
        url = UH_URL.format(start=monday.isoformat(), end=sunday.isoformat())
        try:
            slots = get_json(url)
            ok_weeks += 1
        except Exception as e:
            errors.append(f"week {monday}: {type(e).__name__}")
            continue
        for d in days:
            if monday <= d <= sunday:
                rows_by_day[d] = uh_parse(slots, d)
        time.sleep(PAUSE)
    return rows_by_day, ok_weeks > 0, "; ".join(errors) or None


def fetch_meilahti(days):
    rows_by_day = {d: [] for d in days}
    try:
        index = get_json(ML_INDEX)
    except Exception as e:
        return rows_by_day, False, f"index: {type(e).__name__}"
    errors = []
    for d in days:
        try:
            url = ml_file_url(index, d)
            if url:
                rows_by_day[d] = ml_parse(get_json(url), d)
                time.sleep(PAUSE)
        except Exception as e:
            errors.append(f"{d}: {type(e).__name__}")
    return rows_by_day, True, "; ".join(errors) or None


VENUES = {
    "urheiluhallit": {
        "name": "Urheiluhallit",
        "book_url": "https://kauppa.urheiluhallit.fi/",
        "fetch": fetch_urheiluhallit,
    },
    "meilahti": {
        "name": "Meilahti",
        "book_url": "https://meilahdenliikuntakeskus.cintoia.com/",
        "fetch": fetch_meilahti,
    },
}


def build(days_ahead=DAYS_AHEAD):
    today = helsinki_today()
    days = [today + timedelta(days=i) for i in range(days_ahead)]
    out_days = {d.isoformat(): [] for d in days}
    venues = {}
    for vid, v in VENUES.items():
        try:
            rows_by_day, ok, err = v["fetch"](days)
        except Exception as e:  # never let one venue break the other
            rows_by_day, ok, err = {}, False, f"{type(e).__name__}: {e}"
        venues[vid] = {"name": v["name"], "book_url": v["book_url"], "ok": ok, "error": err}
        for d, rows in rows_by_day.items():
            out_days[d.isoformat()] += rows
    for rows in out_days.values():
        rows.sort(key=lambda r: (r["start"], r["venue"], r["hall"]))
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "venues": venues,
        "days": out_days,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/data.json")
    ap.add_argument("--print", dest="print_day", metavar="YYYY-MM-DD")
    a = ap.parse_args()

    data = build()
    for vid, v in data["venues"].items():
        status = "ok" if v["ok"] else "FAILED"
        print(f"{v['name']}: {status}" + (f" ({v['error']})" if v["error"] else ""), file=sys.stderr)

    if a.print_day:
        rows = data["days"].get(a.print_day, [])
        print(f"Free badminton courts on {a.print_day}\n")
        for r in rows:
            courts = ", ".join(r["courts"]) if r["courts"] else f"{r['count']} free"
            price = f"{r['price']:g} EUR" if r["price"] is not None else "-"
            print(f"{r['start']}-{r['end']}  {data['venues'][r['venue']]['name'] + ' ' + r['hall']:<28} {courts:<40} {price}")
        if not rows:
            print("(nothing found)")
    else:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(f"Wrote {a.out}", file=sys.stderr)

    if not any(v["ok"] for v in data["venues"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
