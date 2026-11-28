#!/usr/bin/env python3
"""Mappa mondiale di DendrOSM (monumental-trees/): scarica da Overpass tutti gli alberi monumentali
(denotation=natural_monument o monument=tree) del mondo, calcola gli stessi check del tool QA e scrive
data/world/<mappa>.geojson.gz e data/world/index.json (stessa forma di data/index.json).

Ogni albero che cade in una regione del tool QA con dati riceve la proprietà "qa" (lo slug della regione): la
scheda della mappa mondiale mostra allora, se l'albero ha errori, il solo link «vedi nel tool QA».

Uso: python scripts/fetch_world.py [monumental]   (senza argomenti: tutte le mappe di MAPS)
I link (image, website, url) non si ricontrollano qui: il check broken_link usa solo la cache dei link del tool QA."""
import gzip
import hashlib
import json
import math
import sys
import time

import fetch_trees as ft

WORLD_DIR = ft.DATA / "world"
WORLD_INDEX = WORLD_DIR / "index.json"
BOUNDARIES = ft.DATA / "regions_boundaries.geojson"

# La query si fa a riquadri ({bbox} = sud,ovest,nord,est): in una sola query mondiale (circa 90.000 nodi, novembre
# 2026) Overpass superava il tempo massimo durante la stampa dei risultati ("Query timed out in print").
# Nei riquadri solo il valore esatto denotation=natural_monument, senza ["natural"="tree"]: con quel filtro anche i
# riquadri quasi vuoti superavano i 120 s ("timed out in query"); natural=tree si controlla dopo, in Python. I valori
# multipli (natural_monument;landmark, una manciata di nodi su Taginfo a novembre 2026) arrivano da "multi", una
# query mondiale a parte che esclude il valore esatto. monument=tree vale come denotation=natural_monument (issue #95,
# is_natural_monument di fetch_trees.py): nei riquadri anche il valore esatto monument=tree, nella query "multi" i valori
# multipli (monument=tree;…).
MAPS = {
    "monumental": {
        "name": "Alberi monumentali",
        "query": """[out:json][timeout:300];
(node["denotation"="natural_monument"]({bbox});
 node["monument"="tree"]({bbox}););
out body qt;""",
        "multi": """[out:json][timeout:300];
(node["denotation"~"(^|;)natural_monument(;|$)"]["denotation"!="natural_monument"];
 node["monument"~"(^|;)tree(;|$)"]["monument"!="tree"];);
out body qt;""",
    },
}
# Riquadri: si parte dal mondo e si divide in 4 (come fetch_bbox di fetch_trees.py) finché un riquadro ha al massimo
# TILE_MAX alberi nei dati dell'aggiornamento precedente (al massimo TILE_MAX_DEPTH livelli); un riquadro che non
# risponde si divide ancora, al massimo TILE_EXTRA_DEPTH livelli in più (Overpass risponde 504 anche quando è solo
# sovraccarico: così le richieste non si moltiplicano). Senza dati precedenti si parte dai 4 quadranti del mondo.
TILE_MAX = 15000           # con i dati di novembre 2026: 22 riquadri
TILE_MAX_DEPTH = 9
TILE_EXTRA_DEPTH = 2
TILE_HTTP_TIMEOUT = 360    # secondi di attesa della risposta (la query ha [timeout:300])


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
    panoramax, image:fotoshi, image con un indirizzo o con un file di Commons (File:…), immagine dell'elemento
    Wikidata), più la foto di iNaturalist solo con una licenza aperta (inat_lic in INAT_OPEN), come «Foto» in
    Completezza."""
    return bool(p.get("wikimedia_commons") or ft.panoramax_ids(p) or p.get("image:fotoshi")
                or str(p.get("image", "")).startswith(("http://", "https://", "File:")) or p.get("wd_img")
                or p.get("inat_lic") in ft.INAT_OPEN)


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


# Alberi dell'elenco MASAF (scripts/masaf_trees.json, da update_masaf.py) non ancora su OpenStreetMap: la mappa dei
# monumentali li mostra con il contorno tratteggiato. Un albero dell'elenco è già su OSM se un nodo (in Italia, anche
# non monumentale), una via o una relazione (es. una coppia di glicini mappata come relazione) ha il suo codice in
# ref:masaf, oppure se un albero OSM monumentale dello stesso genere è entro MASAF_NEAR metri (mappato senza ref:masaf;
# non i membri di un insieme omogeneo monumentali solo per il gruppo, group_only: il codice del gruppo conta già).
MASAF_TREES = ft.HERE / "masaf_trees.json"
MASAF_NEAR = 25
MASAF_CROWN_R = 5.0        # m, come CROWN_R_BY_DEN per natural_monument in index.html
MASAF_CROWN_R_WIDE = 6.0   # m, per gli alberi iscritti anche per l'ampiezza della chioma (criterio a2.3)
# ref:masaf su vie e relazioni (i dati del tool hanno solo i nodi): una query leggera, solo i tag
MASAF_OTHER_QUERY = """[out:json][timeout:300];
(
  way["ref:masaf"];
  relation["ref:masaf"];
);
out tags;"""


def masaf_codes_other():
    """Codici di ref:masaf su vie e relazioni; insieme vuoto (con un avviso) se Overpass non risponde."""
    data = ft.run_overpass(MASAF_OTHER_QUERY, "ref:masaf su vie e relazioni", False)
    if not data:
        ft.warn("MASAF: Overpass non risponde, i ref:masaf su vie e relazioni non sono stati letti")
        return set()
    return {ft.masaf_code(r) for el in data["elements"] for r in str(el.get("tags", {}).get("ref:masaf", "")).split(";")
            if r.strip()}


# Regione di un albero dell'elenco: l'ultima parte del codice (001/A005/LC/03) è il codice ISTAT della regione; il
# Trentino-Alto Adige è diviso nelle due province autonome (21 Bolzano, 22 Trento). Slug come in regions.json.
MASAF_REGIONS = {"01": "piemonte", "02": "valle-d-aosta", "03": "lombardia", "05": "veneto", "06": "friuli-venezia-giulia",
                 "07": "liguria", "08": "emilia-romagna", "09": "toscana", "10": "umbria", "11": "marche", "12": "lazio",
                 "13": "abruzzo", "14": "molise", "15": "campania", "16": "puglia", "17": "basilicata", "18": "calabria",
                 "19": "sicilia", "20": "sardegna", "21": "trentino-alto-adige", "22": "trentino-alto-adige"}


def masaf_missing(fc):
    """(FeatureCollection degli alberi MASAF non ancora su OSM, alberi iscritti, {slug della regione: [già su OSM,
    iscritti]}), per la mappa e per la scheda Completezza; tutto vuoto se manca scripts/masaf_trees.json."""
    data = ft.load_json(MASAF_TREES)
    if not data.get("trees"):
        return {"type": "FeatureCollection", "features": []}, 0, {}
    codes, mon = masaf_codes_other(), []
    print(f"MASAF: {len(codes)} codici su vie e relazioni")
    italy = [r["slug"] for r in ft.REGIONS if r.get("group") == "Italia"]
    for slug in italy:                     # ref:masaf anche sugli alberi non monumentali
        path = ft.OUT_DIR / f"{slug}.geojson.gz"
        if path.exists():
            for f in json.loads(gzip.decompress(path.read_bytes()))["features"]:
                codes.update(ft.masaf_code(r) for r in str(f["properties"].get("ref:masaf", "")).split(";") if r.strip())
    for f in fc["features"]:
        p = f["properties"]
        codes.update(ft.masaf_code(r) for r in str(p.get("ref:masaf", "")).split(";") if r.strip())
        if group_only(p):                  # monumentale solo come membro di un insieme omogeneo: niente vicinanza
            continue
        genus = (p.get("genus") or str(p.get("species", "")).split(" ")[0]).lower()
        mon.append((f["geometry"]["coordinates"], genus))
    grid = {}
    for (lon, lat), g in mon:
        grid.setdefault((int(lat * 1000), int(lon * 1000)), []).append(([lon, lat], g))   # celle di ~100 m

    def near_same_genus(t):
        cx, cy = int(t["lat"] * 1000), int(t["lon"] * 1000)
        return any(g and g == t.get("g", "").lower() and ft.distance_m(pos, [t["lon"], t["lat"]]) <= MASAF_NEAR
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for pos, g in grid.get((cx + dx, cy + dy), []))

    feats, regions = [], {}
    for t in data["trees"]:
        on_osm = t["c"] in codes or near_same_genus(t)
        if slug := MASAF_REGIONS.get(t["c"].rsplit("/", 1)[-1]):
            r = regions.setdefault(slug, [0, 0])
            r[0] += on_osm
            r[1] += 1
        if on_osm:
            continue
        words = t.get("s", "").split()
        sp = " ".join(words[:2]) if len(words) > 1 and words[1][:1].islower() else ""   # senza l'autore
        # specie da mostrare senza l'autore (es. "Wisteria sinensis (Sims) Sweet" -> "Wisteria sinensis"), con i ranghi
        # infraspecifici (subsp., var.) come in species_without_author di fetch_trees.py
        props = {"masaf": t["c"], **{k: t[k] for k in ("v", "co", "pr", "lo", "t", "h", "ci") if k in t},
                 "genus": t.get("g", ""), "sp_full": ft.species_without_author(t.get("s", "")) or t.get("s", ""),
                 "cr": ",".join(t.get("cr", []))}
        if sp:
            props["species"] = sp
        # tipo e ciclo delle foglie stimati dalle liste della wiki (specie, o genere), per disegnare la chioma come gli
        # alberi OSM: colore da lc, forma (lobi o stella) da lt (issue #17)
        est = {"species": sp, "genus": t.get("g", "")}
        if lt := ft.leaf_type_estimate(est):
            props["lt"] = lt
        if lc := ft.leaf_cycle_estimate(est):
            props["lc"] = lc
        # chioma: come un monumentale OSM senza diameter_crown (5 m di raggio), 6 m se l'albero è iscritto anche per
        # l'ampiezza della chioma (criterio a2.3). "crr" = raggio della chioma ("cr" qui sono i criteri)
        props["crr"] = MASAF_CROWN_R_WIDE if "a2.3" in t.get("cr", []) else MASAF_CROWN_R
        # tronco come per gli alberi OSM (sizes di fetch_trees.py): dalla circonferenza dell'elenco, altrimenti stimato
        # dall'altezza con i rapporti della specie (tr_est); "tr" = raggio del tronco
        _, circ, circ_est = ft.sizes({**est, "denotation": "natural_monument",
                                     **({"height": str(t["h"])} if t.get("h") else {}),
                                     **({"circumference": str(t["ci"])} if t.get("ci") else {})})
        if circ:
            props["tr"] = round(circ / (2 * math.pi), 3)
            if circ_est:
                props["tr_est"] = True
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [t["lon"], t["lat"]]}})
    print(f"MASAF: {len(data['trees'])} alberi iscritti, {len(feats)} non ancora su OSM")
    return {"type": "FeatureCollection", "features": feats}, len(data["trees"]), regions


# Alberi monumentali secondo altre fonti ma senza denotation=natural_monument su OSM (issue #13): quelli che nel tool
# QA hanno il check monument_denotation (ref:masaf, ref:rami, ref:CRFOP, ref:gid, ref:usop, monumentaltrees.com, heritage:operator, P9704 o Q811534
# o tutela P1435 su Wikidata; wikidata_heritage è il vecchio codice della tutela, nei dati non ancora aggiornati). Si prendono dai dati delle regioni del tool QA, già
# controllati, e sulla mappa si disegnano come monumentali; "imp" = 1, e la scheda spiega da dove viene la monumentalità
# (mon_src, wd_her_*).
# Si aggiungono, solo in Italia, gli alberi con il consiglio hint_ms (circonferenza almeno pari alla soglia MASAF della
# specie, senza denotation=natural_monument/landmark né ref:masaf): la scheda lo dice con una nota a parte. Non quelli
# con un errore sulla circonferenza (BAD_CIRC), perché la misura non è affidabile.
IMPLICIT_CHECKS = ("|monument_denotation|", "|wikidata_heritage|")
# Membri di un insieme omogeneo MASAF (proprietà "mg" dei dati delle regioni italiane, vedi MASAF_GROUPS in
# fetch_trees.py): si prendono anche loro dalle regioni del tool QA e sulla mappa sono monumentali come gli altri, senza
# "imp" (la scheda li spiega con la riga del gruppo, non con la nota delle altre fonti), anche quando hanno un ref:masaf
# loro senza natural_monument. A quelli che hanno già denotation=natural_monument (arrivati dalla query) si aggiunge solo
# "mg". Quelli monumentali solo per il gruppo (group_only) non contano nel confronto per vicinanza con l'elenco MASAF
# (masaf_missing) né nella divisione in riquadri (plan_tiles).


def group_only(p):
    """Vero per un membro di un insieme omogeneo MASAF senza denotation=natural_monument (né monument=tree)."""
    return bool(p.get("mg")) and not ft.is_natural_monument(p)


def implicit_monuments():
    """Feature degli alberi monumentali solo per altre fonti o per la soglia MASAF, dalle regioni del tool QA con dati
    (con "qa" e "imp"), e dei membri di insiemi omogenei MASAF (con "qa"); restituisce (feature, quanti solo per la
    soglia MASAF, {id: feature dei membri})."""
    feats, n_ms, members = [], 0, {}
    for slug in ft.load_json(ft.INDEX, "regions"):
        path = ft.OUT_DIR / f"{slug}.geojson.gz"
        if not path.exists():
            continue
        for f in json.loads(gzip.decompress(path.read_bytes()))["features"]:
            p = f["properties"]
            if p.get("stump"):
                continue
            if p.get("mg"):
                p["qa"] = slug
                members[p["id"]] = f
                continue
            # monument=tree ha l'errore monument_denotation (anche senza altre fonti), ma arriva già dalla query
            # (issue #95): qui sarebbe un doppione
            if ft.is_natural_monument(p):
                continue
            other = any(c in p.get("issues", "") for c in IMPLICIT_CHECKS)
            if p.get("hint_ms") and BAD_CIRC & set(p.get("issues", "").strip("|").split("|")):
                del p["hint_ms"]                   # circonferenza non affidabile: niente nota sulla soglia
            if other or p.get("hint_ms"):
                p["imp"], p["qa"] = 1, slug
                feats.append(f)
                n_ms += not other
    return feats, n_ms, members


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


WORLD_BBOX = (-90.0, -180.0, 90.0, 180.0)   # sud, ovest, nord, est


def split4(b):
    s, w, n, e = b
    ms, mw = (s + n) / 2, (w + e) / 2
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def plan_tiles(key):
    """[(bbox, livello)] da scaricare: divisione del mondo in base agli alberi dell'aggiornamento precedente (solo
    quelli della query, senza i monumentali impliciti "imp"); i riquadri senza alberi restano, per quelli nuovi."""
    path = WORLD_DIR / f"{key}.geojson.gz"
    pts = []
    if path.exists():
        pts = [f["geometry"]["coordinates"] for f in json.loads(gzip.decompress(path.read_bytes()))["features"]
               if not f["properties"].get("imp") and not group_only(f["properties"])]
    if not pts:
        return [(t, 1) for t in split4(WORLD_BBOX)]

    def rec(b, pts, depth):
        if len(pts) <= TILE_MAX or depth >= TILE_MAX_DEPTH:
            return [(b, depth)]
        out = []
        for t in split4(b):
            s, w, n, e = t
            out += rec(t, [p for p in pts if w <= p[0] <= e and s <= p[1] <= n], depth + 1)
        return out
    return rec(WORLD_BBOX, pts, 0)


def fetch_tile(key, query, b, depth, max_depth):
    """Elementi di un riquadro; se non risponde lo divide in 4 (fino a max_depth). None se una parte manca."""
    bbox = ",".join(f"{round(x, 6):g}" for x in b)
    data = ft.run_overpass(query.replace("{bbox}", bbox), f"{key} {bbox}", depth < max_depth,
                           http_timeout=TILE_HTTP_TIMEOUT)
    if data is not None:
        return data["elements"]
    if depth >= max_depth:
        return None
    print(f" -> divido il riquadro {bbox} in 4 (livello {depth + 1})", flush=True)
    out = []
    for t in split4(b):
        part = fetch_tile(key, query, t, depth + 1, max_depth)
        if part is None:
            return None      # meglio i dati precedenti che dati incompleti
        out += part
        time.sleep(3)
    return out


def fetch_map(key, spec):
    """{"elements": [...]} degli alberi (natural=tree) di tutti i riquadri e della query "multi", senza doppioni sui
    bordi; None se un riquadro non risponde. Se non risponde "multi" si continua con un avviso."""
    tiles = plan_tiles(key)
    print(f"{key}: {len(tiles)} riquadri", flush=True)
    merged = {}
    for i, (b, depth) in enumerate(tiles, 1):
        part = fetch_tile(key, spec["query"], b, depth, depth + TILE_EXTRA_DEPTH)
        if part is None:
            return None
        for el in part:
            merged[(el["type"], el["id"])] = el
        print(f" riquadro {i}/{len(tiles)}: {len(part)} elementi", flush=True)
        time.sleep(3)
    if spec.get("multi"):
        data = ft.run_overpass(spec["multi"], f"{key} valori multipli", False, http_timeout=TILE_HTTP_TIMEOUT)
        if data is None:
            ft.warn(f"{key}: Overpass non risponde, mancano gli alberi con denotation a valori multipli")
        for el in data["elements"] if data else []:
            merged[(el["type"], el["id"])] = el
    trees = [el for el in merged.values() if el.get("tags", {}).get("natural") == "tree"]
    print(f"{key}: {len(merged)} elementi, {len(trees)} con natural=tree", flush=True)
    return {"elements": trees}


def update_map(key, index):
    spec = MAPS[key]
    print(f"== {spec['name']}", flush=True)
    data = fetch_map(key, spec)
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
    ft.update_gphotos_cache(data["elements"])   # foto di Google Foto in image=*, per la scheda
    ft.update_inat_cache(data["elements"])      # osservazioni e foto di iNaturalist, per la scheda
    fc = ft.build(data)
    regions = qa_regions()
    for f in fc["features"]:
        slug = region_of(*f["geometry"]["coordinates"], regions)
        if slug:
            f["properties"]["qa"] = slug
    implicit, n_ms, members = implicit_monuments() if key == "monumental" else ([], 0, {})
    for f in fc["features"]:                   # membri con natural_monument: solo le righe del gruppo
        if m := members.pop(f["properties"]["id"], None):
            f["properties"]["mg"] = m["properties"]["mg"]
    groups = sorted(members.values(), key=lambda f: f["properties"]["id"])
    fc["features"] += implicit + groups
    print(f"{key}: {len(implicit)} alberi monumentali solo per altre fonti (dalle regioni del tool QA), "
          f"{n_ms} dei quali solo per la soglia MASAF; {len(groups)} solo come membri di un insieme omogeneo MASAF")
    n_low = add_min_zoom(fc)
    raw = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    path = WORLD_DIR / f"{key}.geojson.gz"
    ft.write_gz(path, raw)
    low = {"type": "FeatureCollection", "features": [f for f in fc["features"] if "mz" in f["properties"]]}
    low_path = WORLD_DIR / f"{key}.low.geojson.gz"
    ft.write_gz(low_path, json.dumps(low, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    print(f"{key}: {n_low} alberi visibili sotto zoom {FULL_Z} -> {low_path} ({low_path.stat().st_size / 1e3:.0f} KB)")
    # alberi dell'elenco MASAF non ancora su OSM, e conteggi per la scheda Completezza
    masaf, masaf_total, masaf_regions = masaf_missing(fc) if key == "monumental" else (None, 0, {})
    if masaf is not None:
        ft.write_gz(WORLD_DIR / "masaf.geojson.gz", json.dumps(masaf, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    errors = sum(1 for f in fc["features"] if f["properties"]["issues"] != "|")
    index[key] = {"name": spec["name"], "count": len(fc["features"]), "errors": errors,
                  "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "sha1": hashlib.sha1(raw).hexdigest()}
    index[key]["implicit"] = len(implicit)
    index[key]["masaf_size"] = n_ms
    if masaf is not None:
        index[key]["masaf"] = len(masaf["features"])
        index[key]["masaf_total"] = masaf_total        # alberi iscritti; masaf_regions: {slug: [già su OSM, iscritti]}
        index[key]["masaf_regions"] = masaf_regions
        index[key]["masaf_version"] = ft.load_json(MASAF_TREES).get("version", "")
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
