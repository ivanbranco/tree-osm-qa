#!/usr/bin/env python3
"""Scarica gli alberi OSM di una regione via Overpass e produce data/trees.geojson con i check di qualità."""
import json, math, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

ISO = "IT-21"  # Piemonte
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
QUERY = f"""[out:json][timeout:900];
area["ISO3166-2"="{ISO}"]->.a;
node["natural"="tree"](area.a);
out body;"""

# Soglie e valori ammessi
MAX_CIRCUMFERENCE = 43.0   # m
MAX_HEIGHT = 115.92        # m
LEAF_TYPE = {"broadleaved", "needleleaved", "leafless"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}

DEFAULT_CROWN_R = 3.0   # m, se manca crown_diameter
DEFAULT_TRUNK_R = 0.25  # m, se manca circumference


def num(v):
    if v is None:
        return None
    m = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(m)?\s*$", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


def checks(t):
    issues = []
    c, h = num(t.get("circumference")), num(t.get("height"))
    if c is not None and c > MAX_CIRCUMFERENCE:
        issues.append("circumference")
    if h is not None and h > MAX_HEIGHT:
        issues.append("height")
    g = t.get("genus")
    if g is not None and len(g.split()) > 1:
        issues.append("genus")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        issues.append("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        issues.append("leaf_cycle")
    return issues


KEEP = ["name", "species", "species:wikidata", "genus", "genus:wikidata", "taxon",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "crown_diameter", "image", "wikimedia_commons", "wikidata", "start_date"]


def to_feature(el):
    t = el.get("tags", {})
    cd, c = num(t.get("crown_diameter")), num(t.get("circumference"))
    p = {k: t[k] for k in KEEP if k in t}
    p["id"] = el["id"]
    p["cr"] = round(cd / 2, 2) if cd and cd > 0 else DEFAULT_CROWN_R
    p["cr_est"] = not (cd and cd > 0)
    p["tr"] = round(c / (2 * math.pi), 3) if c and 0 < c <= MAX_CIRCUMFERENCE else DEFAULT_TRUNK_R
    iss = checks(t)
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


def fetch():
    body = urllib.parse.urlencode({"data": QUERY}).encode()
    for attempt in range(6):
        url = ENDPOINTS[attempt % len(ENDPOINTS)]
        try:
            req = urllib.request.Request(url, body, {"User-Agent": "alberi-osm/1.0"})
            with urllib.request.urlopen(req, timeout=1000) as r:
                return json.load(r)
        except Exception as e:
            print(f"Overpass {url}: {e}", file=sys.stderr)
            time.sleep(30)
    sys.exit("Overpass non raggiungibile")


def build(data):
    feats = [to_feature(e) for e in data["elements"] if e.get("type") == "node"]
    return {"type": "FeatureCollection",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "features": feats}


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "data" / "trees.geojson"
    fc = build(fetch())
    out.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"{len(fc['features'])} alberi -> {out}")
