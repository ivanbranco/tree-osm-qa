#!/usr/bin/env python3
"""Scarica da Nominatim i confini (semplificati) delle regioni di scripts/regions.json e delle nazioni divise
in regioni (group_rel) e li salva in data/regions_boundaries.geojson, usato dal sito per scegliere nazione e
regione toccando la mappa. Dati © contributori di OpenStreetMap (ODbL).
Scarica anche i confini delle zone non coperte, in data/uncovered_boundaries.geojson: le nazioni di
scripts/countries.json senza regioni in regions.json e, nelle nazioni coperte in parte ("partial": true), le
suddivisioni (stati USA) senza una regione. Sul sito sono grigie e un clic invita a chiederne l'aggiunta.
Scarica solo i confini che mancano nei file; `--all` li riscarica tutti.

Proprietà di ogni confine: key (identificativo: lo slug per le regioni, "g:<gruppo>" per le nazioni), name,
group (il gruppo della regione, o il gruppo stesso per una nazione) e, per le nazioni, country=true.
Zone non coperte: key "u:<ISO>", name (italiano), name_en e, per le suddivisioni, group (il gruppo della nazione
coperta in parte, così si vedono solo aprendo quella nazione)."""
import json
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "regions_boundaries.geojson"
OUT_UNCOVERED = HERE.parent / "data" / "uncovered_boundaries.geojson"
URL = "https://nominatim.openstreetmap.org/lookup"
HEADERS = {"User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)"}
DECIMALS = 3          # circa 100 m: bastano per scegliere la regione
THRESHOLD = 0.005     # semplificazione richiesta a Nominatim (gradi)
LOOKUP_MAX = 10      # id al massimo per ogni richiesta: con blocchi da 50 Nominatim andava in timeout (503)
RETRY_WAIT = (10, 30, 60)   # secondi di attesa prima di ogni nuovo tentativo dopo un errore
# Zone non coperte: servono solo a zoom basso, quindi più semplificate (circa 100 KB compressi per 177 nazioni).
# La soglia scende per i poligoni piccoli (diagonale / UNC_DIV), così micro-stati e isole non spariscono;
# sotto 0,01° le coordinate tengono 3 decimali invece di 2.
UNC_EPS = 0.05
UNC_DIV = 20


def rounded(c):
    return [round(c[0], DECIMALS), round(c[1], DECIMALS)] if isinstance(c[0], (int, float)) else [rounded(x) for x in c]


def douglas_peucker(pts, eps):
    """Semplificazione di una linea: toglie i punti a meno di eps dalla corda (iterativa, niente ricorsione)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        n = (dx * dx + dy * dy) ** .5
        best, idx = -1.0, 0
        for i in range(a + 1, b):
            x, y = pts[i]
            d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / n if n else ((x - x1) ** 2 + (y - y1) ** 2) ** .5
            if d > best:
                best, idx = d, i
        if best > eps:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def coarse(geom):
    """Geometria (Multi)Polygon semplificata per le zone non coperte (UNC_EPS, UNC_DIV); None se non resta niente."""
    def diag(ring):
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** .5

    def ring(r, eps, dec):
        out = []
        for x, y in douglas_peucker(r, eps):
            p = [round(x, dec), round(y, dec)]
            if not out or out[-1] != p:
                out.append(p)
        return out if len(out) >= 4 else None

    def simple(r):
        """Contorno valido: nessun punto ripetuto e nessun lato che ne tocca un altro non adiacente. Un poligono
        che si interseca da solo (Kentucky e Indiana con la prima semplificazione) fa sparire in MapLibre tutto
        quello che sta nella stessa tile."""
        if len({tuple(p) for p in r[:-1]}) < len(r) - 1:
            return False

        def o(p, q, s):
            return (q[0] - p[0]) * (s[1] - p[1]) - (q[1] - p[1]) * (s[0] - p[0])
        segs = list(zip(r, r[1:]))
        boxes = [(min(a[0], b[0]), max(a[0], b[0]), min(a[1], b[1]), max(a[1], b[1])) for a, b in segs]
        for i, (a, b) in enumerate(segs):
            for j in range(i + 2, len(segs)):
                if i == 0 and j == len(segs) - 1:
                    continue
                bi, bj = boxes[i], boxes[j]
                if bi[1] < bj[0] or bj[1] < bi[0] or bi[3] < bj[2] or bj[3] < bi[2]:
                    continue
                c, d = segs[j]
                d1, d2, d3, d4 = o(a, b, c), o(a, b, d), o(c, d, a), o(c, d, b)
                if d1 * d2 <= 0 and d3 * d4 <= 0:
                    return False   # si toccano o si incrociano (con i riquadri che si sovrappongono)
        return True

    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    res = []
    for poly in polys:
        eps = min(UNC_EPS, diag(poly[0]) / UNC_DIV)
        rings = None
        # soglia dimezzata finché tutti i contorni sono validi; sotto 0,001° si tiene la geometria di Nominatim
        while eps >= 0.001:
            dec = 3 if eps < 0.01 else 2
            rings = [ring(r, eps, dec) for r in poly]
            if all(simple(r) for r in rings if r):
                break
            eps /= 2
        else:
            rings = [ring(r, 0, 3) for r in poly]
        if rings[0]:
            res.append([rings[0]] + [r for r in rings[1:] if r])
    return {"type": "MultiPolygon", "coordinates": res} if res else None


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
    build(units, OUT, lambda g: {"type": g["type"], "coordinates": rounded(g["coordinates"])}, required=True)
    build(uncovered_units(regions), OUT_UNCOVERED, coarse, required=False)


def build(units, out, shape, required):
    """Scrive in out i confini delle unità [(relation, proprietà)]; shape trasforma la geometria di Nominatim.
    Unità senza confine: se required il file non si tocca, altrimenti restano fuori con un avviso."""
    # Di norma si riusano i confini già nel file (per chiave) e si scaricano solo quelli nuovi; con --all si
    # riscaricano tutti (per esempio dopo un cambio di confine o di relation in regions.json).
    old = {}
    if "--all" not in sys.argv[1:] and out.exists():
        old = {f["properties"]["key"]: f["geometry"] for f in json.loads(out.read_text(encoding="utf-8"))["features"]}
    todo = sorted({rel for rel, props in units if props["key"] not in old})
    print(f"{out.name}: {len(units) - len(todo)} confini già presenti, {len(todo)} da scaricare")
    by_rel = lookup(todo)
    feats, missing = [], []
    for rel, props in units:
        if props["key"] in old:
            feats.append({"type": "Feature", "properties": props, "geometry": old[props["key"]]})
            continue
        f = by_rel.get(rel)
        g = shape(f["geometry"]) if f and f["geometry"]["type"] in ("Polygon", "MultiPolygon") else None
        if not g:
            missing.append(props["key"])
            continue
        feats.append({"type": "Feature", "properties": props, "geometry": g})
    if missing and required:
        sys.exit(f"Confini mancanti per: {', '.join(missing)}. Il file non è stato modificato.")
    if missing:
        print(f"{out.name}: confini mancanti (zone lasciate fuori): {', '.join(missing)}")
    feats.sort(key=lambda f: f["properties"]["key"])
    if not required:
        feats = drop_nested(feats, out.name)
    text =json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")) + "\n"
    if out.exists() and out.read_text(encoding="utf-8") == text:
        print(f"{out.name}: {len(feats)} confini, nessuna modifica")
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"{len(feats)} confini -> {out} ({len(text) / 1e3:.0f} KB)")


def in_ring(x, y, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def drop_nested(feats, name):
    """Toglie le zone contenute in un'altra più grande (Åland nella Finlandia, Hong Kong nella Cina): disegnate due
    volte sarebbero di un grigio più scuro. Contenuta = almeno l'80% dei suoi punti dentro l'altra (i confini
    nazionali comprendono le acque territoriali, quindi le coste di un territorio sono all'interno)."""
    def polys(f):
        return f["geometry"]["coordinates"]

    def bbox(f):
        pts = [p for poly in polys(f) for p in poly[0]]
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def area(f):
        return sum(abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly[0], poly[0][1:]))) / 2 for poly in polys(f))

    boxes = {f["properties"]["key"]: bbox(f) for f in feats}
    areas = {f["properties"]["key"]: area(f) for f in feats}
    drop = set()
    for a in feats:
        ka, (w, s, e, n) = a["properties"]["key"], boxes[a["properties"]["key"]]
        pts = [p for poly in polys(a) for p in poly[0]]
        for b in feats:
            kb = b["properties"]["key"]
            bw, bs, be, bn = boxes[kb]
            if kb == ka or kb in drop or areas[kb] <= areas[ka] or w < bw or s < bs or e > be or n > bn:
                continue
            inside = sum(1 for x, y in pts if any(in_ring(x, y, poly[0]) and not any(in_ring(x, y, h) for h in poly[1:])
                                                  for poly in polys(b)))
            if inside >= 0.8 * len(pts):
                print(f"{name}: {ka} è dentro {kb}, lasciata fuori")
                drop.add(ka)
                break
    # dalla più grande alla più piccola: la pagina le disegna in quest'ordine, quindi una zona che si sovrappone in
    # parte a una più grande (Sahara Occidentale nel Marocco) sta sopra e vince il clic
    return sorted((f for f in feats if f["properties"]["key"] not in drop), key=lambda f: -areas[f["properties"]["key"]])


def uncovered_units(regions):
    """Zone non coperte: nazioni di countries.json senza regioni e, nelle nazioni coperte in parte, le suddivisioni
    senza regione. Coperta = codice ISO delle regioni (IT-25 → IT) o relation di una regione o di una nazione."""
    path = HERE / "countries.json"
    if not path.exists():
        return []
    countries = json.loads(path.read_text(encoding="utf-8"))
    with_iso = [reg for reg in regions if reg.get("iso")]
    covered = {reg["iso"][:2].upper() for reg in with_iso if not reg.get("partial")}
    partial = {reg["iso"][:2].upper(): reg.get("group") for reg in with_iso if reg.get("partial")}
    sub_iso = {reg["iso"].upper() for reg in with_iso}
    rels = {reg.get("rel") for reg in regions} | {reg.get("group_rel") for reg in regions}
    units = []
    for c in countries:
        props = {"key": "u:" + c["iso"], "name": c["name"], "name_en": c["name_en"]}
        if c.get("parent"):
            if c["parent"] not in partial or c["iso"] in sub_iso or c["rel"] in rels:
                continue
            props["group"] = partial[c["parent"]]
        elif c["iso"] in covered or c["iso"] in partial or c["rel"] in rels:
            continue
        units.append((c["rel"], props))
    return units


if __name__ == "__main__":
    main()
