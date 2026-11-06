#!/usr/bin/env python3
"""Scarica gli alberi OSM di una regione via Overpass e produce data/trees.geojson con i check di qualità."""
import json, math, re, sys, time, urllib.parse, urllib.request
from pathlib import Path

import os
import requests

# Piemonte: bbox (sud, ovest, nord, est) + filtro sull'area amministrativa (relation 44874)
BBOX = (44.0, 6.6, 46.5, 9.3)
AREA_ID = 3600000000 + 44874
MAX_DEPTH = 2  # 1 -> 4 -> 16 tile

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {
    "User-Agent": "AlberiOSMBot/1.0 (https://github.com/ivanbranco/alberi-osm)",
    "Accept-Encoding": "gzip",
}
OUT = Path(__file__).resolve().parent.parent / "data" / "trees.geojson"


def warn(msg):
    print(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}")


def query(bbox):
    s, w, n, e = bbox
    return f"""[out:json][timeout:360];
area({AREA_ID})->.a;
node["natural"="tree"](area.a)({s},{w},{n},{e});
out body qt;"""


def run_overpass(q, label):
    """Prova tutti gli endpoint; restituisce il JSON solo se completo."""
    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": q}, headers=HEADERS, timeout=420)
                if r.status_code in (429, 504):
                    print(f" [!] {url} -> HTTP {r.status_code} ({label}), attendo 60s")
                    time.sleep(60)
                    continue
                r.raise_for_status()
                data = r.json()
                remark = data.get("remark", "")
                if "elements" not in data or "error" in remark.lower():
                    print(f" [!] {url} -> incompleto ({label}): {remark[:150]}")
                    break
                return data
            except Exception as e:
                print(f" [!] {url} fallito ({label}, tentativo {attempt + 1}/2): {str(e)[:200]}")
                time.sleep(15)
    return None


def fetch_bbox(bbox, depth=0):
    """Scarica una bbox; se fallisce la divide in 4 tile (max MAX_DEPTH livelli)."""
    data = run_overpass(query(bbox), f"bbox livello {depth}")
    if data or depth >= MAX_DEPTH:
        return data
    print(f" -> divido la bbox in 4 tile (livello {depth + 1})")
    s, w, n, e = bbox
    mlat, mlon = (s + n) / 2, (w + e) / 2
    merged = {}
    for tile in [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]:
        part = fetch_bbox(tile, depth + 1)
        if part is None:
            return None  # meglio la cache che dati incompleti
        for el in part["elements"]:
            merged[el["id"]] = el
        time.sleep(5)
    return {"elements": list(merged.values())}


def fetch():
    return fetch_bbox(BBOX)


# Soglie e valori ammessi
MAX_CIRCUMFERENCE = 43.0   # m
MAX_HEIGHT = 115.92        # m
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json
SPECIES_LIMITS = {
    " ".join(k.split()).lower(): v
    for k, v in json.loads((Path(__file__).resolve().parent / "species_limits.json").read_text(encoding="utf-8")).items()
}


def species_limit(t):
    sp = t.get("species")
    return SPECIES_LIMITS.get(" ".join(sp.split()).lower()) if sp else None


LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}

DEFAULT_CROWN_R = 3.0   # m, se manca diameter_crown


def num(v):
    if v is None:
        return None
    m = re.match(r"^\s*(-?\d+(?:[.,]\d+)?)\s*(m)?\s*$", str(v))
    return float(m.group(1).replace(",", ".")) if m else None


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def panoramax_ids(t):
    """UUID da panoramax=* (anche separati da ';'; ignora parametri tipo &xyz=)."""
    ids = [x.lower() for x in UUID.findall(t.get("panoramax", ""))]
    return list(dict.fromkeys(ids))


def checks(t):
    issues = []
    c, h = num(t.get("circumference")), num(t.get("height"))
    if c is not None and c > MAX_CIRCUMFERENCE:
        issues.append("circumference")
    if h is not None and h > MAX_HEIGHT:
        issues.append("height")
    lim = species_limit(t)
    if lim:  # limite per specie: solo se non è già scattato quello generico
        if c is not None and "circumference" not in issues and c > lim["circumference"]:
            issues.append("circumference_species")
        if h is not None and "height" not in issues and h > lim["height"]:
            issues.append("height_species")
    sp = t.get("species")
    if sp is not None and len(sp.split()) == 1:
        issues.append("species")
    g = t.get("genus")
    if g is not None and len(g.split()) > 1:
        issues.append("genus")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        issues.append("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        issues.append("leaf_cycle")
    return issues


KEEP = ["name", "species", "genus", "taxon",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "diameter_crown", "image", "wikimedia_commons", "wikidata", "start_date",
        "ref:masaf", "ref:rami", "website", "url"]


def to_feature(el):
    t = el.get("tags", {})
    cd, c = num(t.get("diameter_crown")), num(t.get("circumference"))
    p = {k: t[k] for k in KEEP if k in t}
    p["id"] = el["id"]
    pano = panoramax_ids(t)
    if pano:
        p["panoramax"] = ";".join(pano)
    p["cr"] = round(cd / 2, 2) if cd and cd > 0 else DEFAULT_CROWN_R
    p["cr_est"] = not (cd and cd > 0)
    if c and c > 0:  # il tronco si disegna solo se c'è circumference
        p["tr"] = round(c / (2 * math.pi), 3)
    iss = checks(t)
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    lim = species_limit(t)
    if "height_species" in iss:
        p["lim_h"] = lim["height"]
    if "circumference_species" in iss:
        p["lim_c"] = lim["circumference"]
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


def build(data):
    feats = [to_feature(e) for e in data["elements"] if e.get("type") == "node"]
    return {"type": "FeatureCollection",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "features": feats}


if __name__ == "__main__":
    data = fetch()
    if not data:
        if OUT.exists():
            warn("Download Overpass fallito: mantengo i dati precedenti")
            sys.exit(0)
        sys.exit("Download Overpass fallito e nessuna cache disponibile")
    fc = build(data)
    OUT.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"{len(fc['features'])} alberi -> {OUT}")
