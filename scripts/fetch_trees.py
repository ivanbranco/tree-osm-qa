#!/usr/bin/env python3
"""Scarica gli alberi OSM (natural=tree) delle regioni italiane via Overpass, calcola i check QA
e scrive data/regions/<regione>.geojson.gz e data/index.json.

Uso: python scripts/fetch_trees.py [all | regione [regione ...]]
Le regioni (nome, codice ISO, bbox) sono in scripts/regions.json.
"""
import gzip, hashlib, json, math, os, re, sys, time
from pathlib import Path
from urllib.parse import urlparse

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
OUT_DIR = DATA / "regions"
INDEX = DATA / "index.json"
REGIONS = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))

MAX_DEPTH = 3    # livelli massimi di suddivisione in 4 tile (1 -> 4 -> 16 -> 64)
BBOX_PAD = 0.1   # gradi aggiunti alla bbox di ogni regione (il confine esatto lo decide l'area OSM)

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
HEADERS = {
    "User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)",
    "Accept-Encoding": "gzip",
}


def warn(msg):
    print(msg)
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning::{msg}")


def query(region, bbox):
    s, w, n, e = bbox
    if region.get("rel"):
        area = f"area({3600000000 + region['rel']})->.a;"
    else:
        area = f'area["ISO3166-2"="{region["iso"]}"]["admin_level"="4"]->.a;'
    return f"""[out:json][timeout:360];
{area}
node["natural"="tree"](area.a)({s},{w},{n},{e});
out body qt;"""


def run_overpass(q, label, can_split):
    """Prova gli endpoint; restituisce il JSON solo se completo. Se la query è troppo pesante
    (timeout, errore di memoria, 504) e si può dividere, restituisce subito None."""
    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": q}, headers=HEADERS, timeout=420)
                if r.status_code == 504 and can_split:
                    print(f" [!] {url} -> HTTP 504 ({label}): provo a dividere")
                    return None
                if r.status_code in (429, 504):
                    print(f" [!] {url} -> HTTP {r.status_code} ({label}), attendo 60s")
                    time.sleep(60)
                    continue
                r.raise_for_status()
                data = r.json()
                remark = data.get("remark", "")
                if "elements" not in data or "error" in remark.lower():
                    print(f" [!] {url} -> incompleto ({label}): {remark[:150]}")
                    if can_split and "runtime error" in remark.lower():
                        return None
                    break
                return data
            except requests.exceptions.ReadTimeout:
                print(f" [!] {url} timeout ({label})")
                if can_split:
                    return None
            except Exception as e:
                print(f" [!] {url} fallito ({label}, tentativo {attempt + 1}/2): {str(e)[:200]}")
                time.sleep(15)
    return None


def fetch_bbox(region, bbox, depth=0, min_depth=0):
    """Scarica una bbox; se fallisce (o se depth < min_depth) la divide in 4 tile."""
    label = f"{region['slug']} livello {depth}"
    if depth >= min_depth:
        data = run_overpass(query(region, bbox), label, depth < MAX_DEPTH)
        if data or depth >= MAX_DEPTH:
            return data
    print(f" -> divido la bbox in 4 tile (livello {depth + 1})")
    s, w, n, e = bbox
    mlat, mlon = (s + n) / 2, (w + e) / 2
    merged = {}
    for tile in [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]:
        part = fetch_bbox(region, tile, depth + 1, min_depth)
        if part is None:
            return None  # meglio la cache che dati incompleti
        for el in part["elements"]:
            merged[el["id"]] = el
        time.sleep(5)
    return {"elements": list(merged.values())}


# Soglie e valori ammessi
MAX_CIRCUMFERENCE = 43.0   # m
MAX_HEIGHT = 115.92        # m
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json
_RAW_LIMITS = json.loads((Path(__file__).resolve().parent / "species_limits.json").read_text(encoding="utf-8"))
# rapporti medi (mediane) usati quando la specie non ne ha uno proprio
DEFAULT_RATIOS = _RAW_LIMITS.pop("_default", {})
SPECIES_LIMITS = {" ".join(k.split()).lower(): v for k, v in _RAW_LIMITS.items()}


def species_limit(t):
    sp = t.get("species")
    return SPECIES_LIMITS.get(" ".join(sp.split()).lower()) if sp else None


LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}

# Raggio di default della chioma (m) quando manca diameter_crown; più grande per gli alberi monumentali
# e i landmark, più piccolo per i parcheggi (di solito alberelli). In caso di più valori vince il primo della lista.
DEFAULT_CROWN_R = 3.0
DEFAULT_CROWN_R_BY_DENOTATION = (("natural_monument", 6.0), ("landmark", 6.0), ("parking", 2.0))


def default_crown_r(t):
    den = [x.strip() for x in str(t.get("denotation", "")).split(";")]
    for name, r in DEFAULT_CROWN_R_BY_DENOTATION:
        if name in den:
            return r
    return DEFAULT_CROWN_R


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


NUMERIC_TAGS = ("circumference", "height", "diameter_crown")


def circ_value(t):
    """Circonferenza in m, e se deriva da diameter. Se manca circumference ma c'è diameter (in m)
    si usa π × diametro; se ci sono entrambi i tag, diameter è ignorato."""
    if "circumference" in t:
        return num(t["circumference"]), False
    d = num(t.get("diameter"))
    return (d * math.pi, True) if d is not None else (None, False)


def numeric_keys(t):
    keys = list(NUMERIC_TAGS)
    if "circumference" not in t and "diameter" in t:
        keys.append("diameter")
    return keys


def comma_tags(t):
    """Tag numerici scritti con la virgola decimale (2,9 invece di 2.9)."""
    return [k for k in numeric_keys(t) if "," in str(t.get(k, ""))]


# --- check su numeri, collegamenti e fonti monumentali ---
UNIT = r"(?:m|metres?|meters?|cm|mm|km|ft|feet|foot|in|inch|inches|'|\")"
NUM_UNIT = re.compile(rf"^\s*([+-]?\d+(?:[.,]\d+)?)\s*{UNIT}?\s*$", re.I)
FEET_INCH = re.compile(r"^\s*(\d+)\s*(?:'|ft)\s*(?:(\d+)\s*(?:\"|in)?)?\s*$", re.I)
WIKIDATA = re.compile(r"^Q[1-9]\d*$")
UUID_START = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:&.*)?$")
URL = re.compile(r"^https?://\S+$", re.I)


def bad_number(v):
    """Vero se il valore non è un numero (con o senza unità: le unità non sono un errore) oppure è <= 0.
    Segnala quindi testo, intervalli, elenchi e valori nulli o negativi."""
    m = NUM_UNIT.match(str(v))
    if m:
        return float(m.group(1).replace(",", ".")) <= 0
    m = FEET_INCH.match(str(v))
    if m:
        return int(m.group(1)) + int(m.group(2) or 0) / 12 <= 0
    return True


def bad_numeric_tags(t):
    return [k for k in numeric_keys(t) if k in t and bad_number(t[k])]


def bad_link_tags(t):
    """Tag di collegamento con formato non valido."""
    bad = []
    if "wikidata" in t and not WIKIDATA.match(t["wikidata"]):
        bad.append("wikidata")
    if "wikimedia_commons" in t:
        parts = [x.strip() for x in t["wikimedia_commons"].split(";")]
        if not all(x.startswith(("File:", "Category:")) for x in parts):
            bad.append("wikimedia_commons")
    if "panoramax" in t:
        parts = [x.strip() for x in t["panoramax"].split(";")]
        if not all(UUID_START.match(x) for x in parts):
            bad.append("panoramax")
    if "image" in t and not URL.match(t["image"].strip()):
        bad.append("image")
    return bad


def is_monumentaltrees(v):
    for part in str(v or "").split(";"):
        part = part.strip()
        if not part:
            continue
        host = urlparse(part if "://" in part else "https://" + part.lstrip("/")).hostname or ""
        if host == "monumentaltrees.com" or host.endswith(".monumentaltrees.com"):
            return True
    return False


def monument_sources(t):
    """Fonti che indicano un albero monumentale: ref:masaf, ref:rami, link a monumentaltrees.com."""
    src = [k for k in ("ref:masaf", "ref:rami") if t.get(k)]
    if is_monumentaltrees(t.get("website")) or is_monumentaltrees(t.get("url")):
        src.append("monumentaltrees.com")
    return src


def genus_of_species(sp):
    """Prima parola di species, saltando il segno di ibrido (× o x iniziale)."""
    words = sp.split(";")[0].split()
    while words and words[0] in ("×", "x", "X"):
        words = words[1:]
    return words[0] if words else ""


def checks(t):
    issues = []
    c, h = circ_value(t)[0], num(t.get("height"))
    if c is not None and c > MAX_CIRCUMFERENCE:
        issues.append("circumference")
    if h is not None and h > MAX_HEIGHT:
        issues.append("height")
    if comma_tags(t):
        issues.append("comma")
    lim = species_limit(t) or {}  # limite per specie: solo se non è già scattato quello generico
    if "circumference" in lim and c is not None and "circumference" not in issues and c > lim["circumference"]:
        issues.append("circumference_species")
    if "height" in lim and h is not None and "height" not in issues and h > lim["height"]:
        issues.append("height_species")
    sp = t.get("species")
    if sp is not None and len(sp.split()) == 1:
        issues.append("species")
    if sp is not None and sp.strip()[:1].islower():
        issues.append("species_lowercase")
    g = t.get("genus")
    if g is not None and len(g.split()) > 1:
        issues.append("genus")
    if g is not None and g.strip()[:1].islower():
        issues.append("genus_lowercase")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        issues.append("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        issues.append("leaf_cycle")
    if g is not None and sp is not None and len(g.split()) == 1:
        sg = genus_of_species(sp)
        if sg and g.casefold() != sg.casefold():
            issues.append("genus_mismatch")
    if bad_numeric_tags(t):
        issues.append("number_invalid")
    if bad_link_tags(t):
        issues.append("link_format")
    if monument_sources(t) and "natural_monument" not in t.get("denotation", "").split(";"):
        issues.append("monument_denotation")
    return issues


KEEP = ["name", "species", "genus", "taxon",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "diameter_crown", "image", "wikimedia_commons", "wikidata", "start_date",
        "ref:masaf", "ref:rami", "website", "url", "diameter"]


def sizes(t):
    """Diametro chioma (solo se il tag c'è) e circonferenza per il rendering. Se manca la circonferenza
    (anche da diameter), la stima da height o da diameter_crown con i rapporti della specie
    (height_circ_ratio = altezza/circonferenza, crown_circ_ratio = diametro chioma/circonferenza) o,
    se la specie non ne ha, con quelli di "_default" in species_limits.json. Se il rapporto della specie
    dà una circonferenza impossibile si usa quello di default. Le stime servono solo al rendering.
    """
    lim = species_limit(t) or {}
    h, c, cd = num(t.get("height")), circ_value(t)[0], num(t.get("diameter_crown"))
    max_c, max_h = lim.get("circumference", MAX_CIRCUMFERENCE), lim.get("height", MAX_HEIGHT)
    rh = [r for r in (lim.get("height_circ_ratio"), DEFAULT_RATIOS.get("height_circ_ratio")) if r]
    rc = [r for r in (lim.get("crown_circ_ratio"), DEFAULT_RATIOS.get("crown_circ_ratio")) if r]

    circ, circ_est = (c, False) if c and c > 0 else (None, False)
    if circ is None:
        est = None
        if h and 0 < h <= max_h:
            est = next((h / r for r in rh if h / r <= max_c), None)
        if est is None and cd and cd > 0:
            est = next((cd / r for r in rc if cd / r <= max_c), None)
        if est:
            circ, circ_est = est, True

    crown = cd if cd and cd > 0 else None   # la chioma non si stima dalla circonferenza
    return crown, circ, circ_est


def to_feature(el):
    t = el.get("tags", {})
    p = {k: t[k] for k in KEEP if k in t}
    p["id"] = el["id"]
    pano = panoramax_ids(t)
    if pano:
        p["panoramax"] = ";".join(pano)
    crown, circ, circ_est = sizes(t)
    p["cr"] = round(crown / 2, 2) if crown else default_crown_r(t)
    p["cr_est"] = crown is None
    if circ_value(t)[1] and circ_value(t)[0]:
        p["diam_used"] = True
    if circ:  # il tronco si disegna solo se c'è una circonferenza (reale o stimata)
        p["tr"] = round(circ / (2 * math.pi), 3)
        if circ_est:
            p["tr_est"] = True
    iss = checks(t)
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    if "comma" in iss:
        p["comma"] = ";".join(comma_tags(t))
    if "number_invalid" in iss:
        p["bad_num"] = ";".join(bad_numeric_tags(t))
    if "link_format" in iss:
        p["bad_links"] = ";".join(bad_link_tags(t))
    if "monument_denotation" in iss:
        p["mon_src"] = ";".join(monument_sources(t))
    lim = species_limit(t)
    if "height_species" in iss:
        p["lim_h"] = lim["height"]
    if "circumference_species" in iss:
        p["lim_c"] = lim["circumference"]
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


def build(data):
    feats = [to_feature(e) for e in data["elements"] if e.get("type") == "node"]
    feats.sort(key=lambda f: f["properties"]["id"])  # ordine stabile: se i dati non cambiano, il file resta identico
    return {"type": "FeatureCollection", "features": feats}


def update_region(region, regions_index):
    slug = region["slug"]
    s, w, n, e = region["bbox"]
    bbox = (s - BBOX_PAD, w - BBOX_PAD, n + BBOX_PAD, e + BBOX_PAD)
    area = (n - s) * (e - w)
    data = fetch_bbox(region, bbox, 0, region.get("split", 1 if area > 3 else 0))
    path = OUT_DIR / f"{slug}.geojson.gz"
    if not data:
        if path.exists():
            warn(f"{slug}: download fallito, mantengo i dati precedenti")
            return True
        print(f"{slug}: download fallito e nessuna cache disponibile")
        return False
    fc = build(data)
    raw = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    if regions_index.get(slug, {}).get("sha1") == digest and path.exists():
        print(f"{slug}: nessuna modifica ({len(fc['features'])} alberi)")
        return True
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:  # mtime=0 e filename vuoto: file identico se i dati sono identici
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(raw)
    regions_index[slug] = {"name": region["name"], "count": len(fc["features"]),
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha1": digest}
    print(f"{slug}: {len(fc['features'])} alberi -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return True


def save_index(regions_index):
    DATA.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"regions": regions_index}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")


def main(argv):
    names = argv or ["all"]
    by_slug = {r["slug"]: r for r in REGIONS}
    if "all" in names:
        todo = REGIONS
    else:
        unknown = [x for x in names if x not in by_slug]
        if unknown:
            sys.exit(f"Regione sconosciuta: {', '.join(unknown)}. Disponibili: all, {', '.join(by_slug)}")
        todo = [by_slug[x] for x in names]
    regions_index = json.loads(INDEX.read_text(encoding="utf-8")).get("regions", {}) if INDEX.exists() else {}
    ok = True
    for i, region in enumerate(todo, 1):
        print(f"== {region['name']} ({i}/{len(todo)})", flush=True)
        ok &= update_region(region, regions_index)
        save_index(regions_index)
        time.sleep(5)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
