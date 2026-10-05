#!/usr/bin/env python3
"""
ImmoScout24 rental alert.

Every run (every 10 minutes on GitHub Actions) it fetches your saved rental
search from ImmoScout's mobile API and emails you any offer it has never seen
before — with warm rent, cold rent, utilities, deposit and move-in date pulled
from the listing itself (search results only show the cold rent).

No history is kept: data/seen.json only remembers which offers were already
seen, so nothing is alerted twice. An ad that is deleted and re-posted under a
new ID (same postcode, rooms and size ±1 m²) is recognised and NOT alerted
again. Seen offers are forgotten 60 days after they were last online.

Configuration (GitHub secrets):
  SEARCH_URL          your ImmoScout rental search URL
  GMAIL_ADDRESS       the Gmail account that sends the mail
  GMAIL_APP_PASSWORD  16-char Google app password (NOT your normal password)
  MAIL_TO             recipients, comma-separated
Optional:
  MAX_PAGES           safety cap on result pages to fetch (default 10)
  FORCE_EMAIL         "true" = email every offer currently online, as a [TEST]
"""

import os
import re
import sys
import json
import base64
import smtplib
import datetime as dt
from html import escape
from zoneinfo import ZoneInfo
from urllib.parse import urlsplit, parse_qs
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

STATE_FILE = os.path.join(os.path.dirname(__file__), "data", "seen.json")
TZ = ZoneInfo("Europe/Berlin")
NOW = dt.datetime.now(TZ)
STAMP = NOW.strftime("%Y-%m-%d %H:%M")

API = "https://api.mobile.immobilienscout24.de"
HEADERS = {"User-Agent": "ImmoScout24_1500_30_._", "Accept": "application/json"}

FORGET_AFTER_DAYS = 60     # drop seen offers this long after they were last online
SIZE_TOLERANCE_M2 = 1.0    # re-post match: living space may differ by this much
MAX_DETAIL_FETCHES = 25    # per run, to stay gentle with the API
FAILS_BEFORE_ERROR = 6     # consecutive failed fetches (~1 h) before the run fails

# ImmoScout web path segment -> mobile API realEstateType
REALESTATE_TYPES = {
    "wohnung-mieten": "apartmentrent",
    "wohnung-kaufen": "apartmentbuy",
    "haus-mieten": "houserent",
    "haus-kaufen": "housebuy",
}
# web URL filters the API understands under the same name. pricetype matters:
# "calculatedtotalrent" makes the price filter apply to the warm rent.
PASSTHROUGH = ("numberofrooms", "price", "livingspace", "pricetype",
               "constructionyear", "heatingtypes", "equipment")


# --------------------------------------------------------------------------- #
# Parsing helpers (German number formats)
# --------------------------------------------------------------------------- #
def parse_eur(text):
    """'2.749 €' -> 2749 ; 'auf Anfrage' -> None"""
    digits = re.sub(r"[^\d]", "", (text or "").split(",")[0])
    return int(digits) if digits else None


def parse_num(text):
    """'102 m²' -> 102.0 ; '98,5 m²' -> 98.5 ; '4 Zi.' -> 4.0"""
    m = re.search(r"\d[\d.]*(?:,\d+)?", str(text or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def fmt_eur(n):
    return f"{n:,.0f} €".replace(",", ".") if isinstance(n, (int, float)) else "—"


def fmt_num(n):
    if not isinstance(n, (int, float)):
        return "—"
    return f"{n:.0f}" if float(n).is_integer() else f"{n:.1f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def shape_from_web(shape_param):
    """Convert the web URL's base64-ish shape into the polyline the API wants."""
    s = shape_param.replace(".", "=")
    return base64.urlsafe_b64decode(s).decode("latin1").replace("\x7f", "~")


def build_query(search_url):
    """Turn the ImmoScout web search URL into mobile-API query params."""
    parts = urlsplit(search_url)
    qs = {k: v[0] for k, v in parse_qs(parts.query).items()}
    seg = [p for p in parts.path.split("/") if p][-1] if parts.path else ""
    query = {"searchType": "shape",
             "realestatetype": REALESTATE_TYPES.get(seg, "apartmentrent")}
    if "shape" in qs:
        query["shape"] = shape_from_web(qs["shape"])
    query.update({f: qs[f] for f in PASSTHROUGH if f in qs})
    return query


def fetch_offers(search_url, max_pages, page_size=50):
    """All offers currently matching the search, as normalized dicts."""
    base = build_query(search_url)
    offers = {}
    for pageno in range(1, max_pages + 1):
        r = requests.get(f"{API}/search", headers=HEADERS, timeout=30,
                         params=dict(base, pagenumber=str(pageno), pagesize=str(page_size)))
        r.raise_for_status()
        data = r.json()
        if "results" not in data:
            raise ValueError(f"unexpected API response: {list(data)[:5]}")
        for item in data["results"]:
            eid = str(item.get("id") or "")
            if not eid or item.get("isProject") or eid in offers:
                continue
            vals = [a.get("value") or "" for a in item.get("attributes") or []]
            addr = item.get("address") or {}
            pic = (item.get("titlePicture") or {}).get("preview")
            offers[eid] = {
                "id": eid,
                "title": (item.get("title") or "").strip(),
                "cold_rent": next((parse_eur(v) for v in vals if "€" in v), None),
                "size_m2": next((parse_num(v) for v in vals if "m²" in v), None),
                "rooms": next((parse_num(v) for v in vals if "Zi" in v), None),
                "address_line": re.sub(r"\s*\(unvollständige Adresse\)", "",
                                       addr.get("line") or "").strip(),
                "postcode": addr.get("postcode"),
                "is_private": bool(item.get("isPrivate")),
                "published_ago": item.get("published"),
                # '>' in the CDN url breaks <img> parsing in email clients
                "image": pic.replace(">", "%3E") if pic else None,
                "url": f"https://www.immobilienscout24.de/expose/{eid}",
            }
        if pageno >= (data.get("numberOfPages") or 1):
            break
    return list(offers.values())


DETAIL_LABELS = {          # label prefix in the exposé -> our field
    "Gesamtmiete": "warm_rent",
    "Warmmiete": "warm_rent",
    "Kaltmiete": "cold_rent",
    "Nebenkosten": "utilities",
    "Heizkosten": "heating",
    "Kaution": "deposit",
    "Bezugsfrei ab": "available",
    "Etage": "floor",
}


def fetch_details(offer):
    """Add warm rent, utilities, deposit, move-in date and floor from the
    listing itself. Best effort: on any error the alert still goes out."""
    try:
        r = requests.get(f"{API}/expose/{offer['id']}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        sections = r.json().get("sections") or []
    except Exception as e:
        print(f"  details for {offer['id']} unavailable: {e}")
        return
    for sec in sections:
        for a in sec.get("attributes") or []:
            label, text = (a.get("label") or "").strip(), (a.get("text") or "").strip()
            if label.startswith("Heizkosten in Nebenkosten"):
                offer["heating_included"] = text.lower() == "ja"
                continue
            for prefix, field in DETAIL_LABELS.items():
                if label.startswith(prefix) and text and field not in offer.get("_d", ()):
                    offer.setdefault("_d", set()).add(field)
                    money = field in ("warm_rent", "cold_rent", "utilities", "heating")
                    offer[field] = parse_eur(text) if money else text
                    break
    offer.pop("_d", None)


# --------------------------------------------------------------------------- #
# Seen-offers memory
# --------------------------------------------------------------------------- #
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "seen": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def fingerprint(o):
    return {"postcode": o.get("postcode"), "address_line": o.get("address_line"),
            "rooms": o.get("rooms"), "size_m2": o.get("size_m2")}


def same_flat(a, b):
    """Same postcode (or same address line when a postcode is missing),
    same rooms, size within ±1 m²."""
    if a.get("postcode") and b.get("postcode"):
        if a["postcode"] != b["postcode"]:
            return False
    elif not (a.get("address_line") and a["address_line"] == b.get("address_line")):
        return False
    return (a.get("rooms") is not None and a["rooms"] == b.get("rooms")
            and a.get("size_m2") is not None and b.get("size_m2") is not None
            and abs(a["size_m2"] - b["size_m2"]) <= SIZE_TOLERANCE_M2)


def find_repost(seen, offer, online_ids):
    """A seen offer that is offline now and looks like the same flat."""
    for oid, s in seen.items():
        if oid not in online_ids and same_flat(s["fp"], offer):
            return oid
    return None


def triage(state, offers):
    """Record this run's offers; return (new, reposted) offers."""
    seen = state["seen"]
    online = {o["id"] for o in offers}
    new, reposted = [], []
    for o in offers:
        if o["id"] in seen:
            seen[o["id"]]["last_seen"] = STAMP
            continue
        old = find_repost(seen, o, online)
        seen[o["id"]] = {"first_seen": STAMP, "last_seen": STAMP, "fp": fingerprint(o),
                         "title": o["title"][:80], **({"repost_of": old} if old else {})}
        (reposted if old else new).append(o)
    cutoff = (NOW - dt.timedelta(days=FORGET_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M")
    for oid in [k for k, s in seen.items() if s["last_seen"] < cutoff]:
        del seen[oid]
    return new, reposted


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
FONT = "font-family:Arial,Helvetica,sans-serif"


def card(o):
    warm = o.get("warm_rent")
    cold = o.get("cold_rent")
    rent = (f'<span style="font-size:18px;font-weight:700">{fmt_eur(warm)}</span> warm'
            if warm else f'<span style="font-size:18px;font-weight:700">{fmt_eur(cold)}</span> cold')
    parts = []
    if warm and cold:
        nk = f" + {fmt_eur(o['utilities'])} utilities" if o.get("utilities") else ""
        parts.append(f"{fmt_eur(cold)} cold{nk}")
    if o.get("heating"):
        parts.append(f"heating {fmt_eur(o['heating'])}")
    elif o.get("heating_included") is False:
        parts.append("heating not included")
    facts = [f"{fmt_num(o.get('size_m2'))} m²", f"{fmt_num(o.get('rooms'))} rooms"]
    if warm and o.get("size_m2"):
        facts.append(f"{warm / o['size_m2']:.1f} €/m² warm".replace(".", ","))
    if o.get("floor"):
        facts.append(f"floor {escape(o['floor'])}")
    extra = []
    if o.get("deposit"):
        dep = o["deposit"]
        extra.append(f"Deposit: {fmt_eur(int(dep)) if dep.isdigit() else escape(dep)}")
    if o.get("available"):
        extra.append(f"Available: {escape(o['available'])}")
    extra.append("Private landlord" if o.get("is_private") else "Agency / company")
    if o.get("published_ago"):
        extra.append(f"Posted {escape(o['published_ago'])}")
    img = (f'<a href="{o["url"]}"><img src="{o["image"]}" width="160" height="120" alt="" '
           f'style="display:block;width:160px;height:120px;object-fit:cover;'
           f'border-radius:8px;border:0"></a>' if o.get("image") else "")
    td = "padding:12px 8px;border-bottom:1px solid #eee;vertical-align:top"
    return (
        f'<tr><td style="{td};width:160px;padding-right:4px">{img}</td>'
        f'<td style="{td}"><a href="{o["url"]}" style="color:#1a73e8;font-weight:600;'
        f'text-decoration:none">{escape(o["title"] or o["id"])}</a>'
        f'<div style="color:#666;font-size:12px">'
        f'{escape(o.get("address_line") or o.get("postcode") or "—")}</div>'
        f'<div style="margin-top:6px">{rent}'
        f'{"<span style=color:#666;font-size:12px> · " + " · ".join(parts) + "</span>" if parts else ""}</div>'
        f'<div style="font-size:13px;margin-top:2px">{" · ".join(facts)}</div>'
        f'<div style="font-size:12px;color:#555;margin-top:4px">{" · ".join(extra)}</div>'
        f'<a href="{o["url"]}" style="display:inline-block;margin-top:8px;padding:6px 12px;'
        f'background:#1a73e8;color:#fff;border-radius:6px;font-size:13px;font-weight:600;'
        f'text-decoration:none">Open on ImmoScout →</a></td></tr>')


def render(offers, heading, note):
    rows = "".join(card(o) for o in offers)
    return (f'<div style="max-width:680px;margin:auto;{FONT};color:#222">'
            f'<h2 style="margin-bottom:4px">{heading}</h2>'
            f'<p style="color:#666;margin-top:0">{NOW:%a %d.%m.%Y %H:%M} · {note}</p>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;width:100%;{FONT};font-size:14px">{rows}</table>'
            f'<p style="color:#999;font-size:12px">Automated check every 10 minutes. '
            f'Only offers never seen before are emailed; re-posted ads are skipped.</p></div>')


def short_place(o):
    line = o.get("address_line") or ""
    return line.split(",")[-1].strip() or o.get("postcode") or ""


def subject_for(new):
    if len(new) == 1:
        o = new[0]
        rent = (f"{fmt_eur(o['warm_rent'])} warm" if o.get("warm_rent")
                else f"{fmt_eur(o.get('cold_rent'))} cold")
        return (f"🔔 New rental: {rent} · {fmt_num(o.get('size_m2'))} m² · "
                f"{fmt_num(o.get('rooms'))} Zi · {short_place(o)}")
    rents = [o.get("warm_rent") or o.get("cold_rent") for o in new]
    low = min((r for r in rents if r), default=None)
    return f"🔔 {len(new)} new rentals" + (f" · from {fmt_eur(low)}" if low else "")


def send_email(subject, html):
    sender = os.environ["GMAIL_ADDRESS"]
    to = os.environ.get("MAIL_TO") or sender
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        server.sendmail(sender, [a.strip() for a in to.split(",")], msg.as_string())
    print(f"Email sent: {subject}")


# --------------------------------------------------------------------------- #
def main():
    missing = [k for k in ("SEARCH_URL", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
               if not os.environ.get(k)]
    if missing:
        # skip quietly instead of failing every 10 minutes until secrets exist
        print(f"Not configured yet (missing secrets: {', '.join(missing)}) — skipping.")
        return

    state = load_state()
    meta = state.setdefault("meta", {})
    first_run = not state.get("seen")
    try:
        offers = fetch_offers(os.environ["SEARCH_URL"],
                              int(os.environ.get("MAX_PAGES", "10")))
    except Exception as e:
        # transient API hiccups are common; only fail the run (so GitHub
        # notifies you) once it has been broken for about an hour
        meta["failed_runs"] = meta.get("failed_runs", 0) + 1
        save_state(state)
        print(f"Fetch failed ({meta['failed_runs']} in a row): {e}")
        if meta["failed_runs"] >= FAILS_BEFORE_ERROR:
            sys.exit("ImmoScout fetch keeps failing — check the API / SEARCH_URL.")
        return
    meta["failed_runs"] = 0
    # one state change a day keeps the repo "active", so GitHub never pauses
    # the schedule even if no new offer shows up for 60 days
    meta["last_run_date"] = NOW.date().isoformat()
    print(f"Fetched {len(offers)} offers")

    new, reposted = triage(state, offers)
    for o in reposted:
        print(f"Re-post skipped: {o['id']} (was {state['seen'][o['id']]['repost_of']}) {o['title'][:60]}")
    force = os.environ.get("FORCE_EMAIL", "").lower() in ("1", "true", "yes")

    if first_run or force:
        for o in offers[:MAX_DETAIL_FETCHES]:
            fetch_details(o)
        offers.sort(key=lambda o: o.get("warm_rent") or o.get("cold_rent") or 0)
        heading = (f"👀 Rental watch started — {len(offers)} offers online now"
                   if first_run else f"📋 {len(offers)} offers online now")
        subject = ("👀 Rental watch started" if first_run else "[TEST] Rental watch") + \
                  f" · {len(offers)} offers online"
        send_email(subject, render(offers, heading,
                                   "From now on you'll get an email only for new offers."))
    elif new:
        for o in new[:MAX_DETAIL_FETCHES]:
            fetch_details(o)
        send_email(subject_for(new), render(
            new, f"🔔 {len(new)} new rental offer{'s' if len(new) > 1 else ''}",
            f"{len(offers)} offers match your search right now"))
    else:
        print("No new offers.")

    save_state(state)


if __name__ == "__main__":
    main()
