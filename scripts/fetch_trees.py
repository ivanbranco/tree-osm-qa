#!/usr/bin/env python3
"""Scarica gli alberi OSM (natural=tree) delle regioni italiane via Overpass, calcola i check QA
e scrive data/regions/<regione>.geojson.gz e data/index.json.

Uso: python scripts/fetch_trees.py [all | gruppo | regione ...]   (es. "liguria veneto", "italia", "liguria,bulgaria")
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
(
  node["natural"="tree"](area.a)({s},{w},{n},{e});
  node["natural"="tree_stump"](area.a)({s},{w},{n},{e});
);
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
# Rapporto altezza/circonferenza ("slenderness"): segnalati solo i valori esagerati. Nei dati la mediana è ~6 e
# gli alberi reali più tozzi (olivi monumentali) stanno sopra 0,45.
SLENDER_MAX = 100.0        # oltre: albero troppo slanciato (es. height=24, circumference=0.01)
STOCKY_MIN = 1 / 3         # sotto: albero troppo tozzo (circonferenza più di 3 volte l'altezza)
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json
_RAW_LIMITS = json.loads((Path(__file__).resolve().parent / "species_limits.json").read_text(encoding="utf-8"))
# rapporti medi (mediane) usati quando la specie non ne ha uno proprio
DEFAULT_RATIOS = _RAW_LIMITS.pop("_default", {})
SPECIES_LIMITS = {" ".join(k.split()).lower(): v for k, v in _RAW_LIMITS.items()}


def norm_species(s):
    """Nome normalizzato per il confronto con la lista: spazi singoli, 'x' tra i nomi come '×', minuscolo."""
    return " ".join("×" if w in ("x", "X") else w for w in s.split()).casefold()


# Lista di specie della wiki OSM (scripts/update_species_list.py): species:wikidata, leaf_type, leaf_cycle
_LIST_FILE = Path(__file__).resolve().parent / "species_list.json"
_LIST = json.loads(_LIST_FILE.read_text(encoding="utf-8")).get("species", {}) if _LIST_FILE.exists() else {}
SPECIES_REF = {norm_species(k): v for k, v in _LIST.items()}
QID_TO_SPECIES = {}
for _name, _v in _LIST.items():
    if _v.get("wikidata"):
        QID_TO_SPECIES.setdefault(_v["wikidata"], _name)

# Eccezioni ai check (scripts/whitelist.json): species:wikidata accettati per specie (es. sinonimi)
_WL_FILE = Path(__file__).resolve().parent / "whitelist.json"
_WL = json.loads(_WL_FILE.read_text(encoding="utf-8")) if _WL_FILE.exists() else {}
WD_WHITELIST = {norm_species(k): set(v) for k, v in _WL.get("wikidata_mismatch", {}).items()}


# Lista dei generi della wiki OSM (scripts/update_species_list.py): leaf_cycle (uno o entrambi) e leaf_type
_GENUS_FILE = Path(__file__).resolve().parent / "genus_list.json"
_GENERA = json.loads(_GENUS_FILE.read_text(encoding="utf-8")).get("genera", {}) if _GENUS_FILE.exists() else {}
GENUS_REF = {k.lstrip("× ").casefold(): (k, v) for k, v in _GENERA.items()}

# Valori di denotation sui nodi con il numero di usi, da Taginfo (scripts/update_species_list.py)
_DEN_FILE = Path(__file__).resolve().parent / "denotation_values.json"
DEN_COUNTS = json.loads(_DEN_FILE.read_text(encoding="utf-8")).get("values", {}) if _DEN_FILE.exists() else {}
DEN_RARE_MAX = 10      # valore raro: usato al massimo 10 volte
DEN_COMMON_MIN = 1000  # valore comune: usato almeno 1000 volte (es. urban, avenue, park)
DEN_FORMAT = re.compile(r"^[a-z][a-z0-9_]*$")   # minuscole, cifre e _, come i valori OSM
# valori comuni, dal più lungo: "parking" va provato prima di "park"
DEN_COMMON = sorted((v for v, n in DEN_COUNTS.items() if n >= DEN_COMMON_MIN and DEN_FORMAT.match(v)), key=len, reverse=True)


def denotation_problems(t):
    """Valori di denotation sospetti (ogni parte di un valore multiplo separatamente): (valore, motivo, usi).
    format: caratteri non ammessi (=, :, /, spazi, maiuscole, …) o non inizia con una lettera;
    glued:<comune>: un valore comune con altro testo attaccato senza _ (es. urbanheidialhoranta);
    rare: usato al massimo DEN_RARE_MAX volte. Gli ultimi due richiedono denotation_values.json."""
    out = []
    for v in (x.strip() for x in str(t.get("denotation", "")).split(";")):
        if not v:
            continue
        n = DEN_COUNTS.get(v, 0)
        if not DEN_FORMAT.match(v):
            out.append((v, "format", n))
        elif not DEN_COUNTS:
            continue
        elif n < DEN_COMMON_MIN and (pre := next((c for c in DEN_COMMON
                                                  if len(v) > len(c) and v.startswith(c) and v[len(c)] != "_"), None)):
            out.append((v, "glued:" + pre, n))
        elif n <= DEN_RARE_MAX:
            out.append((v, "rare", n))
    return out


CYCLE_GROUP = {"deciduous": "d", "semi_deciduous": "d", "evergreen": "e", "semi_evergreen": "e"}


def cycle_ok(tag, expected):
    """leaf_cycle compatibile con quelli attesi: deciduous e semi_deciduous (come evergreen e semi_evergreen) sono equivalenti."""
    return CYCLE_GROUP[tag] in {CYCLE_GROUP[e] for e in expected}


def type_ok(tag, expected):
    """leaf_type compatibile: leafless non si confronta con la lista; palm vale come broadleaved."""
    return tag == expected or tag == "leafless" or {tag, expected} == {"palm", "broadleaved"}


def genus_ref(t):
    """(nome, voce) del genere nella lista: dal tag genus se è di una sola parola, altrimenti dalla species."""
    g = t.get("genus")
    g = g.strip() if g and len(g.split()) == 1 else (genus_of_species(t["species"]) if t.get("species") else "")
    return GENUS_REF.get(g.lstrip("× ").casefold()) if g else None


def leaf_type_estimate(t):
    """leaf_type ricavato dalle liste della wiki: dalla specie; se non è nella lista, dal genere."""
    sp = t.get("species")
    if sp and ";" not in sp:
        ref = SPECIES_REF.get(norm_species(sp))
        if ref and ref.get("leaf_type"):
            return ref["leaf_type"]
    g = genus_ref(t)
    return g[1].get("leaf_type") if g else None


def species_list_mismatches(t):
    """Incoerenze con le liste della wiki OSM: (codici, proprietà per i messaggi). species:wikidata si
    confronta con la lista delle specie; leaf_type e leaf_cycle con la specie e, se non è in lista, con il genere."""
    issues, props = [], {}
    sp = t.get("species")
    if sp and ";" in sp:
        return issues, props
    ref = SPECIES_REF.get(norm_species(sp)) if sp and SPECIES_REF else None
    wd = (t.get("species:wikidata") or "").strip()
    if sp and SPECIES_REF and wd and ";" not in wd and wd not in WD_WHITELIST.get(norm_species(sp), ()):
        if ref and ref.get("wikidata") and wd != ref["wikidata"]:
            issues.append("wikidata_mismatch")
            props.update(sp_wd=wd, exp_wd=ref["wikidata"])
        elif not ref and wd in QID_TO_SPECIES:  # QID noto, ma assegnato a un'altra specie
            issues.append("wikidata_mismatch")
            props.update(sp_wd=wd, exp_sp=QID_TO_SPECIES[wd])
    if ref:
        exp_for, exp_lt = sp.strip(), ref.get("leaf_type")
        exp_lc = [ref["leaf_cycle"]] if ref.get("leaf_cycle") else []
    else:
        g = genus_ref(t)
        exp_for, exp_lt, exp_lc = (f"il genere {g[0]}", g[1].get("leaf_type"), g[1].get("leaf_cycle") or []) if g else (None, None, [])
    lt, lc = t.get("leaf_type"), t.get("leaf_cycle")
    if exp_for and lt in LEAF_TYPE and exp_lt and not type_ok(lt, exp_lt):
        issues.append("leaf_type_mismatch")
        props.update(exp_lt=exp_lt, exp_for=exp_for)
    if exp_for and lc in LEAF_CYCLE and exp_lc and not cycle_ok(lc, exp_lc):
        issues.append("leaf_cycle_mismatch")
        props.update(exp_lc=" o ".join(exp_lc), exp_for=exp_for)
    return issues, props


def species_limit(t):
    sp = t.get("species")
    return SPECIES_LIMITS.get(" ".join(sp.split()).lower()) if sp else None


LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}

# Raggio di default della chioma (m) quando manca diameter_crown; più grande per gli alberi monumentali
# e i landmark, più piccolo per i parcheggi (di solito alberelli). In caso di più valori vince il primo della lista.
DEFAULT_CROWN_R = 3.0
DEFAULT_STUMP_R = 0.25  # m, raggio della X di un ceppo senza circumference
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


NUMERIC_TAGS = ("circumference", "height", "diameter_crown", "diameter")


def circ_value(t):
    """Circonferenza in m, e se deriva da diameter. Se manca circumference ma c'è diameter (in m)
    si usa π × diametro; se ci sono entrambi i tag, diameter è ignorato."""
    if "circumference" in t:
        return num(t["circumference"]), False
    d = num(t.get("diameter"))
    return (d * math.pi, True) if d is not None else (None, False)


def numeric_keys(t):
    return list(NUMERIC_TAGS)  # diameter incluso: il formato viene controllato anche quando è ignorato


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


# Criteri di monumentalità MASAF (https://wiki.openstreetmap.org/wiki/IT:Key:ref:masaf): codici separati da virgole, senza spazi
MASAF_CRITERIA = {"a", "a1", "a2", "a2.1", "a2.2", "a2.3", "b", "c", "d", "e", "f", "g"}


def masaf_criteria_bad(t):
    """Parti di masaf:criteria che non sono codici validi (anche vuote, con spazi, maiuscole o separate da ';')."""
    if "masaf:criteria" not in t:
        return []
    return [x for x in t["masaf:criteria"].split(",") if x not in MASAF_CRITERIA]


def genus_of_species(sp):
    """Prima parola di species, saltando il segno di ibrido (× o x iniziale)."""
    words = sp.split(";")[0].split()
    while words and words[0] in ("×", "x", "X"):
        words = words[1:]
    return words[0] if words else ""


def slenderness(t):
    """height / circumference (anche da diameter); None per i ceppi o se manca una delle due misure."""
    if t.get("natural") == "tree_stump":
        return None
    c, h = circ_value(t)[0], num(t.get("height"))
    return h / c if c and h and c > 0 and h > 0 else None


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
    r = slenderness(t)  # solo se non è già segnalata la misura sbagliata (circonferenza o altezza oltre i limiti)
    if r is not None and r > SLENDER_MAX and not {"height", "height_species"} & set(issues):
        issues.append("too_slender")
    if r is not None and r < STOCKY_MIN and not {"circumference", "circumference_species"} & set(issues):
        issues.append("too_stocky")
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
    issues.extend(species_list_mismatches(t)[0])
    if bad_numeric_tags(t):
        issues.append("number_invalid")
    if bad_link_tags(t):
        issues.append("link_format")
    if monument_sources(t) and "natural_monument" not in t.get("denotation", "").split(";"):
        issues.append("monument_denotation")
    if denotation_problems(t):
        issues.append("denotation_value")
    if masaf_criteria_bad(t):
        issues.append("masaf_criteria")
    return issues


KEEP = ["name", "species", "genus", "taxon",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "diameter_crown", "image", "wikimedia_commons", "wikidata", "start_date",
        "ref:masaf", "masaf:criteria", "ref:rami", "website", "url", "diameter"]


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

    crown = cd if cd and cd > 0 else None   # la chioma non si stima: le stime provate risultavano troppo grandi
    return crown, circ, circ_est


JITTER = 0.15   # variazione massima (±15%) del raggio di ripiego della chioma


NO_JITTER_DENOTATION = {"natural_monument", "landmark"}   # questi tengono il raggio di ripiego esatto


def jitter(osm_id, t):
    """Fattore tra 1 - JITTER e 1 + JITTER, casuale ma sempre uguale per lo stesso albero (dall'id OSM):
    varia il raggio di ripiego della chioma, solo per l'aspetto della mappa. 1 per monumentali e landmark."""
    if NO_JITTER_DENOTATION & {x.strip() for x in str(t.get("denotation", "")).split(";")}:
        return 1.0
    return 1 - JITTER + 2 * JITTER * ((osm_id * 2654435761) % 2**32) / 2**32


def to_feature(el):
    t = el.get("tags", {})
    p = {k: t[k] for k in KEEP if k in t}
    p["id"] = el["id"]
    pano = panoramax_ids(t)
    if pano:
        p["panoramax"] = ";".join(pano)
    if t.get("natural") == "tree_stump":
        # ceppo: una X grande quanto la circonferenza (se manca, raggio di default e X più chiara)
        c, from_d = circ_value(t)
        p["stump"] = 1
        if c and c > 0:
            p["sr"] = round(c / (2 * math.pi), 3)
            if from_d:
                p["diam_used"] = True
        else:
            p["sr"], p["sr_est"] = DEFAULT_STUMP_R, True
    else:
        crown, circ, circ_est = sizes(t)
        # chioma: diameter_crown, altrimenti raggio di ripiego con variazione casuale (non per monumentali e landmark)
        p["cr"] = round(crown / 2, 2) if crown else round(default_crown_r(t) * jitter(el["id"], t), 2)
        p["cr_est"] = crown is None
        if circ_value(t)[1] and circ_value(t)[0]:
            p["diam_used"] = True
        if circ:  # il tronco si disegna solo se c'è una circonferenza (reale o stimata)
            p["tr"] = round(circ / (2 * math.pi), 3)
            if circ_est:
                p["tr_est"] = True
        lt = t.get("leaf_type")
        if lt not in LEAF_TYPE:  # leaf_type mancante o non valido: forma della chioma stimata dalla lista
            est = leaf_type_estimate(t)
            if est:
                p["lt"] = est
    iss = checks(t)
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    if "comma" in iss:
        p["comma"] = ";".join(comma_tags(t))
    p.update(species_list_mismatches(t)[1])
    if "number_invalid" in iss:
        p["bad_num"] = ";".join(bad_numeric_tags(t))
    if "link_format" in iss:
        p["bad_links"] = ";".join(bad_link_tags(t))
    if "monument_denotation" in iss:
        p["mon_src"] = ";".join(monument_sources(t))
    if "denotation_value" in iss:  # per la scheda: valori, motivi e usi, nello stesso ordine
        den = denotation_problems(t)
        p["den_bad"] = ";".join(v for v, _, _ in den)
        p["den_why"] = ";".join(w for _, w, _ in den)
        p["den_n"] = ";".join(str(n) for _, _, n in den)
    if "too_slender" in iss or "too_stocky" in iss:  # per la scheda: rapporto e circonferenza usata (anche da diameter)
        p["hc_ratio"] = round(slenderness(t), 3)
        p["hc_circ"] = round(circ_value(t)[0], 2)
    if "masaf_criteria" in iss:  # parti non valide, separate da "," come nel tag (non possono contenerne)
        p["masaf_bad"] = ",".join(masaf_criteria_bad(t))
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
    errors = sum(1 for f in fc["features"] if f["properties"]["issues"] != "|")  # alberi con almeno un errore
    if regions_index.get(slug, {}).get("sha1") == digest and path.exists():
        regions_index[slug]["errors"] = errors  # per gli indici scritti prima che esistesse il conteggio
        print(f"{slug}: nessuna modifica ({len(fc['features'])} alberi)")
        return True
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:  # mtime=0 e filename vuoto: file identico se i dati sono identici
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(raw)
    regions_index[slug] = {"name": region["name"], "count": len(fc["features"]), "errors": errors,
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha1": digest}
    print(f"{slug}: {len(fc['features'])} alberi -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return True


def save_index(regions_index):
    DATA.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"regions": regions_index}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")


def main(argv):
    # regioni separate da spazi o virgole; "all" = tutte; il nome di un gruppo (es. "italia") = tutte le sue regioni
    names = [x.lower() for a in argv for x in re.split(r"[\s,]+", a) if x] or ["all"]
    by_slug = {r["slug"]: r for r in REGIONS}
    groups = {}
    for r in REGIONS:
        if r.get("group"):
            groups.setdefault(r["group"].lower(), []).append(r)
    if "all" in names:
        todo = REGIONS
    else:
        unknown = [x for x in names if x not in by_slug and x not in groups]
        if unknown:
            sys.exit(f"Regione sconosciuta: {', '.join(unknown)}. Disponibili: all, {', '.join([*groups, *by_slug])}")
        todo = list({r["slug"]: r for x in names for r in (groups.get(x) or [by_slug[x]])}.values())  # senza doppioni
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
