#!/usr/bin/env python3
"""Scarica da Nominatim i confini (semplificati) delle regioni di scripts/regions.json e delle nazioni divise
in regioni (group_rel) e li salva in data/regions_boundaries.geojson, usato dal sito per scegliere nazione e
regione toccando la mappa. Dati © contributori di OpenStreetMap (ODbL).
Scarica solo i confini che mancano nel file; `--all` li riscarica tutti.

Proprietà di ogni confine: key (identificativo: lo slug per le regioni, "g:<gruppo>" per le nazioni), name,
group (il gruppo della regione, o il gruppo stesso per una nazione) e, per le nazioni, country=true."""
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "regions_boundaries.geojson"
URL = "https://nominatim.openstreetmap.org/lookup"
HEADERS = {"User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)"}
DECIMALS = 3          # circa 100 m: bastano per scegliere la regione
THRESHOLD = 0.005     # semplificazione richiesta a Nominatim (gradi)
LOOKUP_MAX = 10      # id al massimo per ogni richiesta: con blocchi da 50 Nominatim andava in timeout (503)
RETRY_WAIT = (10, 30, 60)   # secondi di attesa prima di ogni nuovo tentativo dopo un errore


def rounded(c):
    return [round(c[0], DECIMALS), round(c[1], DECIMALS)] if isinstance(c[0], (int, float)) else [rounded(x) for x in c]


def fetch(rels):
    """Una richiesta a Nominatim per le relation indicate, ripetuta dopo un errore del server o di rete."""
    ids = ",".join(f"R{r}" for r in rels)
    for wait in (*RETRY_WAIT, None):
        try:
            r = requests.get(URL, params={"osm_ids": ids, "format": "geojson", "polygon_geojson": 1,
                                          "polygon_threshold": THRESHOLD}, headers=HEADERS, timeout=180)
            if r.status_code < 500 and r.status_code != 429:
                r.raise_for_status()
                return r.json().get("features", [])
            err = f"HTTP {r.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            err = type(e).__name__
        if wait is None:
            raise RuntimeError(f"Nominatim non risponde ({err}) per {ids}")
        print(f"Nominatim: {err} per {ids}, nuovo tentativo tra {wait} s")
        time.sleep(wait)


def lookup(rels):
    """Confini delle relation indicate, a blocchi di LOOKUP_MAX: {id della relation: feature GeoJSON}."""
    by_rel = {}
    for i in range(0, len(rels), LOOKUP_MAX):
        by_rel.update({int(f["properties"]["osm_id"]): f for f in fetch(rels[i:i + LOOKUP_MAX])
                       if f["properties"].get("osm_type") == "relation"})
        time.sleep(1)   # regole d'uso di Nominatim: al massimo una richiesta al secondo
    return by_rel


def main():
    regions = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))
    # unità da disegnare: le regioni e, una volta sola, le nazioni divise in regioni
    units = [(reg["rel"], {"key": reg["slug"], "name": reg["name"], "group": reg.get("group")})
             for reg in regions if reg.get("rel")]
    groups = {reg["group"]: reg["group_rel"] for reg in regions if reg.get("group") and reg.get("group_rel")}
    units += [(rel, {"key": "g:" + g, "name": g, "group": g, "country": True}) for g, rel in groups.items()]
    # Di norma si riusano i confini già nel file (per chiave) e si scaricano solo quelli nuovi; con --all si
    # riscaricano tutti (per esempio dopo un cambio di confine o di relation in regions.json).
    old = {}
    if "--all" not in sys.argv[1:] and OUT.exists():
        old = {f["properties"]["key"]: f["geometry"] for f in json.loads(OUT.read_text(encoding="utf-8"))["features"]}
    todo = sorted({rel for rel, props in units if props["key"] not in old})
    print(f"{len(units) - len(todo)} confini già presenti, {len(todo)} da scaricare")
    by_rel = lookup(todo)
    feats, missing = [], []
    for rel, props in units:
        if props["key"] in old:
            feats.append({"type": "Feature", "properties": props, "geometry": old[props["key"]]})
            continue
        f = by_rel.get(rel)
        if not f or f["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
            missing.append(props["key"])
            continue
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": f["geometry"]["type"], "coordinates": rounded(f["geometry"]["coordinates"])}})
    if missing:
        sys.exit(f"Confini mancanti per: {', '.join(missing)}. Il file non è stato modificato.")
    feats.sort(key=lambda f: f["properties"]["key"])
    text = json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
        print(f"{len(feats)} confini, nessuna modifica")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"{len(feats)} confini -> {OUT} ({len(text) / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
