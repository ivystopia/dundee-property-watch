"""A compact daily property report. Internal source diagnostics are never rendered."""
from __future__ import annotations

from datetime import date
from html import escape
import hashlib
from itertools import groupby
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

from model import MAX_PRICE_GBP


PAGE_SIZE = 20


def label(url):
    host = urlsplit(url).hostname or "Listing"
    for domain, name in [("rightmove.co.uk", "Rightmove"), ("zoopla.co.uk", "Zoopla"), ("onthemarket.com", "OnTheMarket"),
                         ("tspc.co.uk", "TSPC"), ("s1homes.com", "s1homes"), ("primelocation.com", "PrimeLocation")]:
        if host == domain or host.endswith("." + domain):
            return name
    return host.removeprefix("www.")


def card(p, day):
    e = escape
    primary = label(p["url"]) if p["portal_fallback"] else p["agent"] + " · Original listing"
    links = [f'<a class="original" href="{e(p["url"], quote=True)}" rel="noopener noreferrer">{e(primary)}</a>']
    seen = {p["url"]}
    for link in p.get("additional_urls", []):
        if link["url"] not in seen:
            seen.add(link["url"])
            links.append(f'<a href="{e(link["url"], quote=True)}" rel="noopener noreferrer">{e(label(link["url"]))}</a>')
    date_text = "Listed " + date.fromisoformat(p["listed_date"]).strftime("%-d %B %Y") if p["listed_date"] else "First seen " + date.fromisoformat(day).strftime("%-d %B %Y") + "; listing date unavailable"
    kind = " · Auction" if p["listing_kind"] == "auction" else " · New build, plot " + e(p["plot_number"]) if p["listing_kind"] == "new_build" else ""
    notes = f'<p class="notes">{e(p["notes"])}</p>' if p["notes"] else ""
    fallback = '<p class="agent">Original agent page unavailable; portal listing linked.</p>' if p["portal_fallback"] else ""
    photo = ""
    image_url = p.get("image_url")
    if image_url and re.fullmatch(r"images/[a-f0-9]{64}\.jpg", image_url):
        caption = "Developer’s house-type image; appearance may vary." if p["listing_kind"] == "new_build" else ""
        photo = f'''<figure class="photo"><a href="{e(p['url'], quote=True)}" aria-label="View listing for {e(p['address'], quote=True)}"><img src="{e(image_url, quote=True)}" alt="{e(p['address'], quote=True)}" loading="lazy" decoding="async" width="640" height="427" onerror="this.closest('article').classList.remove('has-photo');this.closest('figure').remove()"></a>{f'<figcaption>{caption}</figcaption>' if caption else ''}</figure>'''
    return f'''<article{' class="has-photo"' if photo else ''}>{photo}<div class="card-content"><div class="topline"><div class="price">£{p['price_gbp']:,}<span class="qualifier">{e(p['price_qualifier'])}</span></div><span class="facts">{p['bedrooms']} bedrooms · {e(p['property_type'])}{kind}</span></div>
<h3>{e(p['address'])}</h3><p class="date">{e(date_text)} · {e(p['agent'])}</p>{notes}{fallback}<nav class="links" aria-label="Listings for {e(p['address'], quote=True)}">{''.join(links)}</nav></div></article>'''


def pagination(page, total):
    if total == 1:
        return ""
    previous = "index.html" if page == 2 else f"page-{page - 1}.html"
    newer = f'<a href="{previous}" rel="prev">← Newer</a>' if page > 1 else ""
    older = f'<a href="page-{page + 1}.html" rel="next">Older →</a>' if page < total else ""
    return f'<nav class="pagination" aria-label="Property pages">{newer}<span>Page {page} of {total}</span>{older}</nav>'


def render(db, output):
    latest = db.execute("SELECT MAX(day) FROM runs WHERE status IN ('complete','partial')").fetchone()[0]
    if not latest:
        raise ValueError("No completed research run to publish")
    rows = list(db.execute("SELECT r.day,r.data,p.first_seen,c.jpeg FROM reports r JOIN properties p ON p.id=r.property_id LEFT JOIN photos photo ON photo.property_id=p.id LEFT JOIN photo_cache c ON c.image_url=photo.image_url ORDER BY r.day DESC, json_extract(r.data,'$.listed_date') IS NULL, json_extract(r.data,'$.listed_date') DESC, json_extract(r.data,'$.price_gbp') DESC, r.property_id"))
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    added = sum(row[0] == latest for row in rows)
    output.mkdir(parents=True, exist_ok=True)
    images = output / "images"
    images.mkdir(exist_ok=True)
    image_names = set()
    prepared = []
    for day, data, first_seen, jpeg in rows:
        image_url = None
        if jpeg:
            name = hashlib.sha256(jpeg).hexdigest() + ".jpg"
            if name not in image_names:
                target = images / name
                if not target.exists() or target.read_bytes() != jpeg:
                    temporary = images / ("." + name + ".tmp")
                    temporary.write_bytes(jpeg)
                    temporary.replace(target)
                image_names.add(name)
            image_url = "images/" + name
        prepared.append((day, data, first_seen, image_url))
    rows = prepared
    assets = output / "assets"
    assets.mkdir(exist_ok=True)
    for source in (Path(__file__).resolve().parent / "assets").iterdir():
        if source.is_file():
            shutil.copyfile(source, assets / source.name)
    (output / ".nojekyll").write_text("")
    for page in range(1, pages + 1):
        sections = []
        if page == 1 and not added:
            sections.append(f'<section><h2>{date.fromisoformat(latest).strftime("%A, %-d %B %Y")}</h2><p class="empty">No new properties to report today.</p></section>')
        for day, group in groupby(rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], key=lambda row: row[0]):
            cards = []
            for _, data, first_seen, image_url in group:
                p = json.loads(data)
                p["image_url"] = image_url
                cards.append(card(p, first_seen[:10]))
            sections.append(f'<section><h2>Added {date.fromisoformat(day).strftime("%-d %B %Y")}</h2>{"".join(cards)}</section>')
        nav = pagination(page, pages)
        html = f'''<!doctype html>
<html lang="en-GB" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,follow"><meta name="theme-color" content="#147d59"><meta name="description" content="A daily archive of homes for sale in Dundee and Broughty Ferry: 2–3 bedrooms, advertised prices up to £{MAX_PRICE_GBP:,}."><title>Dundee property watch{' · Page ' + str(page) if page > 1 else ''}</title><link rel="icon" href="assets/favicon.svg" type="image/svg+xml"><script src="assets/theme.js"></script><link rel="stylesheet" href="assets/pico.jade.min.css"><link rel="stylesheet" href="assets/report.css"></head>
<body><a class="skip-link" href="#properties">Skip to properties</a><main class="container"><header class="masthead"><div class="masthead-top"><p class="eyebrow">Daily property watch</p><button id="theme-toggle" class="theme-toggle" type="button" aria-pressed="false" hidden><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20.9 13.2A9 9 0 0 1 10.8 3.1 9 9 0 1 0 20.9 13.2Z"/></svg><span>Dark mode</span></button></div><h1>Dundee &amp; Broughty Ferry</h1><p>Homes for sale · 2–3 bedrooms · Up to £{MAX_PRICE_GBP:,}</p><p class="count">Updated {date.fromisoformat(latest).strftime("%-d %B %Y")} · {added} new · {len(rows)} in the archive</p></header>{nav}<div id="properties">{''.join(sections)}</div>{nav}<footer><p>New finds are added each day; earlier entries stay in this archive.<br>Prices and availability reflect when each home was found. Check the linked listing for current details.<br>A listing date is shown where verified. New-build images may show the house type rather than the individual plot.</p><p class="footer-links"><a href="https://ivyf.net/">Ivy’s website</a><a href="https://github.com/ivystopia/dundee-property-watch">About this watch</a><a href="https://picocss.com/">Theme: Pico</a></p></footer></main></body></html>'''
        name = "index.html" if page == 1 else f"page-{page}.html"
        tmp = output / ("." + name + ".tmp")
        tmp.write_text(html)
        tmp.replace(output / name)
    # A reviewed duplicate repair can shrink the archive across a page boundary.
    # Only remove files in the renderer's own numbered-page namespace.
    for stale in output.glob("page-*.html"):
        match = re.fullmatch(r"page-(\d+)\.html", stale.name)
        if match and int(match[1]) > pages:
            stale.unlink()
    for stale in images.glob("*.jpg"):
        if re.fullmatch(r"[a-f0-9]{64}\.jpg", stale.name) and stale.name not in image_names:
            stale.unlink()
    return output / "index.html"
