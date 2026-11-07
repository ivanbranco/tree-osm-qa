#!/usr/bin/env python3
"""Scarica da Nominatim i confini (semplificati) delle regioni di scripts/regions.json
e li salva in data/regions_boundaries.geojson, usato dal sito per scegliere la regione toccando la mappa.
Dati © contributori di OpenStreetMap (ODbL)."""
import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "regions_boundaries.geojson"
URL = "https://nominatim.openstreetmap.org/lookup"
HEADERS = {"User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)"}
DECIMALS = 3          # circa 100 m: bastano per scegliere la regione
THRESHOLD = 0.005     # semplificazione richiesta a Nominatim (gradi)


def rounded(c):
    return [round(c[0], DECIMALS), round(c[1], DECIMALS)] if isinstance(c[0], (int, float)) else [rounded(x) for x in c]


def build(regions, features_by_rel):
    feats, missing = [], []
    for reg in regions:
        f = features_by_rel.get(reg.get("rel"))
        if not f or f["geometry"]["type"] not in ("Polygon", "MultiPolygon"):
            missing.append(reg["slug"])
            continue
        feats.append({"type": "Feature", "properties": {"slug": reg["slug"], "name": reg["name"]},
                      "geometry": {"type": f["geometry"]["type"], "coordinates": rounded(f["geometry"]["coordinates"])}})
    return feats, missing


def main():
    regions = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))
    ids = ",".join(f"R{r['rel']}" for r in regions if r.get("rel"))
    r = requests.get(URL, params={"osm_ids": ids, "format": "geojson", "polygon_geojson": 1,
                                  "polygon_threshold": THRESHOLD}, headers=HEADERS, timeout=120)
    r.raise_for_status()
    by_rel = {int(f["properties"]["osm_id"]): f for f in r.json().get("features", [])
              if f["properties"].get("osm_type") == "relation"}
    feats, missing = build(regions, by_rel)
    if missing:
        sys.exit(f"Confini mancanti per: {', '.join(missing)}. Il file non è stato modificato.")
    feats.sort(key=lambda f: f["properties"]["slug"])
    text = json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
        print(f"{len(feats)} confini, nessuna modifica")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"{len(feats)} confini -> {OUT} ({len(text) / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
