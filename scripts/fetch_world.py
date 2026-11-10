#!/usr/bin/env python3
"""Mappa mondiale di DendrOSM (monumental-trees/): scarica da Overpass tutti gli alberi monumentali
(denotation=natural_monument) del mondo, calcola gli stessi check del tool QA e scrive
data/world/<mappa>.geojson.gz e data/world/index.json (stessa forma di data/index.json).

Ogni albero che cade in una regione del tool QA con dati riceve la proprietà "qa" (lo slug della regione): la
scheda della mappa mondiale mostra allora, se l'albero ha errori, il solo link «vedi nel tool QA».

Uso: python scripts/fetch_world.py [monumental]   (senza argomenti: tutte le mappe di MAPS)
I link (image, website, url) non si ricontrollano qui: il check broken_link usa solo la cache dei link del tool QA."""
import hashlib
import json
import math
import sys
import time

import fetch_trees as ft

WORLD_DIR = ft.DATA / "world"
WORLD_INDEX = WORLD_DIR / "index.json"
BOUNDARIES = ft.DATA / "regions_boundaries.geojson"

MAPS = {
    "monumental": {
        "name": "Alberi monumentali",
        "query": """[out:json][timeout:900][maxsize:2147483648];
node["denotation"~"(^|;)natural_monument(;|$)"]["natural"="tree"];
out body qt;""",
    },
}


# Sfoltimento per zoom: sotto FULL_Z la pagina mostra solo alcuni alberi, uno per zona. Per ogni zoom z < FULL_Z il
# mondo si divide in celle di CELL_PX pixel (tile da 512 px, come MapLibre) e in ogni cella si sceglie un albero
# (rank: prima quelli con una foto, poi il più grande); "mz" = il primo zoom in cui un albero è scelto. Le celle di uno
# zoom contengono esattamente quattro celle dello zoom dopo, quindi un albero visibile a uno zoom resta visibile
# avvicinandosi. Gli alberi con mz vanno anche nel file ridotto <mappa>.low.geojson.gz, l'unico usato sotto FULL_Z.
FULL_Z = 7
CELL_PX = 48
# errori che rendono inaffidabili le misure: quegli alberi contano come senza misura nella scelta
BAD_CIRC = {"circumference", "circumference_species", "diameter_unit", "circ_diam_mismatch", "number_invalid",
            "comma", "too_slender", "too_slender_species", "too_stocky"}
BAD_HEIGHT = {"height", "height_species", "number_invalid", "comma", "too_slender", "too_slender_species", "too_stocky"}


def has_photo(p):
    """Vero se la scheda dell'albero mostra una foto: le stesse fonti di imageInfo in index.html (wikimedia_commons,
    panoramax, image:fotoshi, image con un indirizzo, immagine dell'elemento Wikidata)."""
    return bool(p.get("wikimedia_commons") or ft.panoramax_ids(p) or p.get("image:fotoshi")
                or str(p.get("image", "")).startswith(("http://", "https://")) or p.get("wd_img"))


def rank(p):
    """Chiave di scelta (più grande = prima): con una foto prima di tutto (la mappa a zoom bassi mostra soprattutto
    alberi con la foto), poi circonferenza misurata (o dal diametro), altezza, nome o wikidata, infine l'id più basso
    (ordine fisso tra un aggiornamento e l'altro)."""
    issues = set(p["issues"].strip("|").split("|"))
    circ = 2 * math.pi * p["tr"] if p.get("tr") and not p.get("tr_est") and not BAD_CIRC & issues else 0
    h = ft.num(p.get("height")) if not BAD_HEIGHT & issues else None
    return (has_photo(p), circ, h or 0, bool(p.get("name") or p.get("wikidata")), -p["id"])


def add_min_zoom(fc):
    """Scrive "mz" negli alberi scelti per gli zoom sotto FULL_Z; restituisce il numero di alberi con mz."""
    pos = []
    for f in fc["features"]:
        lon, lat = f["geometry"]["coordinates"]
        lat = max(-85.0, min(85.0, lat))
        x = (lon + 180) / 360
        y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2
        pos.append((x, y, rank(f["properties"]), f["properties"]))
    for z in range(FULL_Z):
        n = 512 * 2 ** z / CELL_PX           # celle per lato
        best = {}
        for x, y, r, p in pos:
            cell = (int(x * n), int(y * n))
            if cell not in best or r > best[cell][0]:
                best[cell] = (r, p)
        for _, p in best.values():
            p.setdefault("mz", z)
    return sum(1 for *_, p in pos if "mz" in p)


def in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_geometry(x, y, g):
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    return any(in_ring(x, y, p[0]) and not any(in_ring(x, y, h) for h in p[1:]) for p in polys)


def qa_regions():
    """[(slug, bbox, geometria)] delle regioni del tool QA con dati (confini da regions_boundaries.geojson)."""
    idx = ft.load_json(ft.INDEX, "regions")
    out = []
    for f in ft.load_json(BOUNDARIES).get("features", []):
        slug = f["properties"].get("key")
        if slug in idx:
            pts = [c for c in _coords(f["geometry"]["coordinates"])]
            xs, ys = [c[0] for c in pts], [c[1] for c in pts]
            out.append((slug, (min(xs), min(ys), max(xs), max(ys)), f["geometry"]))
    return out


def _coords(c):
    if isinstance(c[0], (int, float)):
        yield c
    else:
        for x in c:
            yield from _coords(x)


def region_of(x, y, regions):
    for slug, (w, s, e, n), g in regions:
        if w <= x <= e and s <= y <= n and in_geometry(x, y, g):
            return slug
    return None


def update_map(key, index):
    spec = MAPS[key]
    print(f"== {spec['name']}", flush=True)
    data = ft.run_overpass(spec["query"], key, False, http_timeout=960)   # la query ha [timeout:900]
    if not data:
        ft.warn(f"{key}: Overpass non ha risposto, mantengo i dati precedenti")
        return False
    nodes = sum(1 for el in data["elements"] if el.get("type") == "node")
    prev = index.get(key, {}).get("count")
    if prev and nodes < prev * ft.MIN_KEEP_RATIO:
        ft.warn(f"{key}: solo {nodes} alberi contro {prev} dell'ultimo aggiornamento: mantengo i dati precedenti")
        return False
    ft.update_wikidata_cache(data["elements"])   # check su Wikidata, foto e voci di Wikipedia nella scheda
    ft.update_fotoshi_cache(data["elements"])
    fc = ft.build(data)
    regions = qa_regions()
    for f in fc["features"]:
        slug = region_of(*f["geometry"]["coordinates"], regions)
        if slug:
            f["properties"]["qa"] = slug
    n_low = add_min_zoom(fc)
    raw = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    path = WORLD_DIR / f"{key}.geojson.gz"
    ft.write_gz(path, raw)
    low = {"type": "FeatureCollection", "features": [f for f in fc["features"] if "mz" in f["properties"]]}
    low_path = WORLD_DIR / f"{key}.low.geojson.gz"
    ft.write_gz(low_path, json.dumps(low, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    print(f"{key}: {n_low} alberi visibili sotto zoom {FULL_Z} -> {low_path} ({low_path.stat().st_size / 1e3:.0f} KB)")
    errors = sum(1 for f in fc["features"] if f["properties"]["issues"] != "|")
    index[key] = {"name": spec["name"], "count": len(fc["features"]), "errors": errors, "diam": "mm",
                  "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "sha1": hashlib.sha1(raw).hexdigest()}
    print(f"{key}: {len(fc['features'])} alberi ({sum(1 for f in fc['features'] if 'qa' in f['properties'])} "
          f"nelle regioni del tool QA) -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return True


def main(argv):
    keys = [a for a in argv if a] or list(MAPS)
    unknown = [k for k in keys if k not in MAPS]
    if unknown:
        sys.exit(f"Mappa sconosciuta: {', '.join(unknown)}. Disponibili: {', '.join(MAPS)}")
    index = ft.load_json(WORLD_INDEX, "regions")
    ok = True
    for key in keys:
        ok &= update_map(key, index)
        WORLD_DIR.mkdir(parents=True, exist_ok=True)
        WORLD_INDEX.write_text(json.dumps({"regions": index}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
