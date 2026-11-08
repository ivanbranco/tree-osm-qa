#!/usr/bin/env python3
"""Scarica gli alberi OSM (natural=tree e natural=tree_stump) delle regioni di scripts/regions.json via Overpass,
calcola i check QA e scrive data/regions/<regione>.geojson.gz e data/index.json.

Uso: python scripts/fetch_trees.py [all | gruppo | regione ...]   (es. "liguria veneto", "italia", "liguria,bulgaria")
Le regioni (nome, codice ISO, bbox) sono in scripts/regions.json.
"""
import functools, gzip, hashlib, json, math, os, re, sys, time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
OUT_DIR = DATA / "regions"
INDEX = DATA / "index.json"
REGIONS = json.loads((HERE / "regions.json").read_text(encoding="utf-8"))


def load_json(path, key=None):
    """Contenuto di un file JSON (o della sua chiave key); {} se il file manca."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(key, {}) if key else data

MAX_DEPTH = 3    # livelli massimi di suddivisione in 4 tile (1 -> 4 -> 16 -> 64)
BBOX_PAD = 0.1   # gradi aggiunti alla bbox di ogni regione (il confine esatto lo decide l'area OSM)
MIN_KEEP_RATIO = 0.5   # se un download dà meno della metà degli alberi dell'ultima volta, si tengono i dati vecchi

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
# Chioma più larga di un albero vivo a tronco singolo: 60,4 m, record Guinness (Samanea saman "Chamchuri",
# Thailandia, 2018):
# https://www.guinnessworldrecords.com/world-records/635581-widest-crown-on-a-living-tree-single-trunk
# I baniani (Ficus benghalensis), con molti tronchi, possono superarlo.
MAX_CROWN = 60.4           # m
# Tag non documentati sugli alberi: tree=* e type=* (type=palm escluso: va sistemato in blocco)
UNDOC_TAGS = ("tree", "type")
UNDOC_IGNORE = {("type", "palm")}
# Rapporto altezza/circonferenza ("slenderness"): segnalati solo i valori esagerati. Nei dati la mediana è ~6 e
# gli alberi reali più tozzi (olivi monumentali) stanno sopra 0,45.
SLENDER_MAX = 100.0        # oltre: albero troppo slanciato (es. height=24, circumference=0.01)
STOCKY_MIN = 1 / 3         # sotto: albero troppo tozzo (circonferenza più di 3 volte l'altezza)
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json
_RAW_LIMITS = load_json(HERE / "species_limits.json")
# rapporti medi (mediane) usati quando la specie non ne ha uno proprio
DEFAULT_RATIOS = _RAW_LIMITS.pop("_default", {})
SPECIES_LIMITS = {" ".join(k.split()).lower(): v for k, v in _RAW_LIMITS.items()}


def norm_species(s):
    """Nome normalizzato per il confronto con la lista: spazi singoli, 'x' tra i nomi come '×', minuscolo."""
    return " ".join("×" if w in ("x", "X") else w for w in s.split()).casefold()


# Lista di specie della wiki OSM (scripts/update_reference_lists.py): species:wikidata, leaf_type, leaf_cycle
_LIST = load_json(HERE / "species_list.json", "species")
SPECIES_REF = {norm_species(k): v for k, v in _LIST.items()}
QID_TO_SPECIES = {}
for _name, _v in _LIST.items():
    if _v.get("wikidata"):
        QID_TO_SPECIES.setdefault(_v["wikidata"], _name)

# Eccezioni ai check (scripts/whitelist.json): species:wikidata accettati per specie (es. sinonimi) e
# species vere che il check sugli errori di battitura scambierebbe per una specie della lista
_WL = load_json(HERE / "whitelist.json")
WD_WHITELIST = {norm_species(k): set(v) for k, v in _WL.get("wikidata_mismatch", {}).items()}
TYPO_WHITELIST = {norm_species(k) for k in _WL.get("species_typo", [])}


# Lista dei generi della wiki OSM (scripts/update_reference_lists.py): leaf_cycle (uno o entrambi) e leaf_type
_GENERA = load_json(HERE / "genus_list.json", "genera")
GENUS_REF = {k.lstrip("× ").casefold(): (k, v) for k, v in _GENERA.items()}

# Valori di denotation sui nodi con il numero di usi, da Taginfo (scripts/update_reference_lists.py)
DEN_COUNTS = load_json(HERE / "denotation_values.json", "values")
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


# Elementi Wikidata che sono taxon: quelli con un nome scientifico (P225, "taxon name"). È molto più veloce del
# controllo "istanza di taxon (Q16521) o di una sua sottoclasse" (37 s contro 4,5 s per 200 QID) e distingue
# allo stesso modo specie, illustrazioni, frutti e alberi monumentali. Le risposte restano in
# data/wikidata_taxon.json ({"Q…": true|false}): a ogni aggiornamento si interrogano solo i QID nuovi.
# Se Wikidata non risponde, i QID restano sconosciuti e i check relativi non scattano.
TAXON_FILE = DATA / "wikidata_taxon.json"
TAXON = load_json(TAXON_FILE)
WDQS_URL = "https://query.wikidata.org/sparql"
TAXON_KEYS = ("species:wikidata", "genus:wikidata", "wikidata")
TAXON_BATCH = 200


def qids(t, key):
    """QID validi (Q…) di un tag, anche separati da ';'."""
    return [x.strip() for x in str(t.get(key, "")).split(";") if WIKIDATA.match(x.strip())]


# Coordinate (P625) degli elementi di wikidata=*, per il check sulla distanza: data/wikidata_coords.json
# ({"Q…": [[lon, lat], …]}, lista vuota se l'elemento non ha coordinate). Come per i taxon, solo i QID nuovi.
COORDS_FILE = DATA / "wikidata_coords.json"
COORDS = load_json(COORDS_FILE)
WD_MAX_DIST = 1000   # m: oltre, l'albero è "lontano" dall'elemento Wikidata collegato (nei dati il massimo è ~0,7 km)
POINT = re.compile(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)")


def wdqs(sparql):
    """Righe del risultato di una query a Wikidata; solleva un'eccezione se non risponde."""
    r = requests.post(WDQS_URL, data={"query": sparql}, timeout=120,
                      headers={**HEADERS, "Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def ask_wikidata(todo, query, store, label):
    """Interroga Wikidata a blocchi di TAXON_BATCH QID: query(batch) dà lo SPARQL, store(batch, righe) salva.
    Se Wikidata non risponde si ferma: i QID rimasti si verificano al prossimo aggiornamento."""
    for i in range(0, len(todo), TAXON_BATCH):
        batch = todo[i:i + TAXON_BATCH]
        try:
            store(batch, wdqs(query(" ".join("wd:" + q for q in batch))))
        except Exception as e:
            warn(f"Wikidata non risponde ({str(e)[:150]}): {len(todo) - i} QID restano da verificare ({label})")
            return
        time.sleep(1)


def store_taxa(batch, rows):
    taxa = {b["item"]["value"].rsplit("/", 1)[-1] for b in rows}
    TAXON.update({q: q in taxa for q in batch})


def store_coords(batch, rows):
    found = {}
    for b in rows:
        m = POINT.search(b["coord"]["value"])
        if m:
            found.setdefault(b["item"]["value"].rsplit("/", 1)[-1], []).append([float(m.group(1)), float(m.group(2))])
    COORDS.update({q: found.get(q, []) for q in batch})


# Immagine (P18, un file di Commons) degli elementi di wikidata=*: data/wikidata_images.json ({"Q…": "Nome.jpg"},
# stringa vuota se l'elemento non ha immagine). Serve alla scheda quando l'albero non ha altre immagini.
IMAGES_FILE = DATA / "wikidata_images.json"
IMAGES = load_json(IMAGES_FILE)


def store_images(batch, rows):
    found = {}
    for b in rows:   # il valore è un indirizzo .../Special:FilePath/<nome file codificato>
        found.setdefault(b["item"]["value"].rsplit("/", 1)[-1], unquote(b["img"]["value"].rsplit("/", 1)[-1]))
    IMAGES.update({q: found.get(q, "") for q in batch})


def update_wikidata_cache(elements):
    """Chiede a Wikidata, per i QID usati e non ancora in cache, se sono taxon (species:wikidata, genus:wikidata,
    wikidata), le coordinate e l'immagine (solo wikidata); aggiorna e salva le cache."""
    tags = [el.get("tags", {}) for el in elements]
    wd = {q for t in tags for q in qids(t, "wikidata")}
    todo_taxa = sorted({q for t in tags for k in TAXON_KEYS for q in qids(t, k)} - TAXON.keys())
    todo_coords = sorted(wd - COORDS.keys())
    todo_images = sorted(wd - IMAGES.keys())
    # owl:sameAs: QID reindirizzati a un altro elemento
    ask_wikidata(todo_taxa, lambda v: "SELECT DISTINCT ?item WHERE { VALUES ?item { " + v + " } "
                 "{ ?item wdt:P225 [] } UNION { ?item owl:sameAs/wdt:P225 [] } }", store_taxa, "taxon")
    ask_wikidata(todo_coords, lambda v: "SELECT ?item ?coord WHERE { VALUES ?item { " + v + " } "
                 "{ ?item wdt:P625 ?coord } UNION { ?item owl:sameAs/wdt:P625 ?coord } }", store_coords, "coordinate")
    ask_wikidata(todo_images, lambda v: "SELECT ?item ?img WHERE { VALUES ?item { " + v + " } "
                 "{ ?item wdt:P18 ?img } UNION { ?item owl:sameAs/wdt:P18 ?img } }", store_images, "immagini")
    for todo, path, cache in ((todo_taxa, TAXON_FILE, TAXON), (todo_coords, COORDS_FILE, COORDS),
                              (todo_images, IMAGES_FILE, IMAGES)):
        if todo:
            path.write_text(json.dumps(cache, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def wikidata_image(t):
    """File di Commons dell'immagine (P18) dell'elemento di wikidata=*, se non è un taxon (sarebbe la foto della
    specie, non dell'albero); None se manca."""
    for q in qids(t, "wikidata"):
        if TAXON.get(q) is not True and IMAGES.get(q):
            return IMAGES[q]
    return None


def distance_m(a, b):
    """Distanza in metri tra due punti [lon, lat] (formula dell'emisenoverso)."""
    (x1, y1), (x2, y2) = [(math.radians(p[0]), math.radians(p[1])) for p in (a, b)]
    h = math.sin((y2 - y1) / 2) ** 2 + math.cos(y1) * math.cos(y2) * math.sin((x2 - x1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def wikidata_far(t, pos):
    """[(QID, distanza in km)] degli elementi di wikidata=* (non taxon, con coordinate) a più di WD_MAX_DIST
    dall'albero; se l'elemento ha più coordinate conta la più vicina."""
    out = []
    for q in qids(t, "wikidata"):
        if TAXON.get(q) is True or not COORDS.get(q):
            continue
        d = min(distance_m(pos, c) for c in COORDS[q])
        if d > WD_MAX_DIST:
            out.append((q, round(d / 1000, 2)))
    return out


def not_taxon_tags(t):
    """species:wikidata e genus:wikidata che puntano a un elemento che non è un taxon: [(chiave, QID)]."""
    return [(k, q) for k in ("species:wikidata", "genus:wikidata") for q in qids(t, k) if TAXON.get(q) is False]


def taxon_wikidata(t):
    """QID di wikidata=* che sono taxon (wikidata dovrebbe indicare il singolo albero)."""
    return [q for q in qids(t, "wikidata") if TAXON.get(q) is True]


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


def leaf_cycle_estimate(t):
    """leaf_cycle ricavato dalle liste della wiki, come leaf_type_estimate; None se il genere li ammette entrambi."""
    sp = t.get("species")
    if sp and ";" not in sp:
        ref = SPECIES_REF.get(norm_species(sp))
        if ref and ref.get("leaf_cycle"):
            return ref["leaf_cycle"]
    g = genus_ref(t)
    cycles = g[1].get("leaf_cycle") or [] if g else []
    return cycles[0] if len(cycles) == 1 else None


# --- species scritta male: confronto con la lista delle specie ---
# Il nome (genere + epiteto, senza il segno di ibrido ×) si confronta con quelli della lista contando le lettere
# da cambiare (distanza di Levenshtein). Tarato sui dati: con 1 lettera sono quasi sempre errori di battitura
# (Olea europeea, Phoenix dactilifera); con 2 anche specie vere assenti dalla lista (Myrtus communis -> Pyrus
# communis), quindi a 2 si segnala solo se il genere scritto non è un genere noto; da 3 in su troppi falsi positivi.
def binomial(sp):
    """Genere ed epiteto normalizzati, senza ×: 'Citrus × limon L.' -> 'citrus limon'."""
    words = [w for w in norm_species(sp).split() if w != "×"]
    return " ".join(words[:2])


SPECIES_BIN = {}
for _name in _LIST:
    SPECIES_BIN.setdefault(binomial(_name), _name)
KNOWN_GENERA = {b.split()[0] for b in SPECIES_BIN} | set(GENUS_REF)


def levenshtein(a, b, limit):
    """Distanza di Levenshtein tra a e b, o limit + 1 se è maggiore di limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


@functools.lru_cache(maxsize=None)
def species_typo(sp):
    """Specie della lista che species probabilmente voleva scrivere, o None."""
    if ";" in sp or norm_species(sp) in TYPO_WHITELIST:
        return None
    b = binomial(sp)
    if len(b.split()) < 2 or b in SPECIES_BIN:   # una parola: c'è già il check; in lista (anche cultivar): ok
        return None
    best, dist = None, 3
    for cand, name in SPECIES_BIN.items():
        d = levenshtein(b, cand, 2)
        if d < dist:
            best, dist = (cand, name), d
    if best is None:
        return None
    if dist == 2 and b.split()[0] != best[0].split()[0] and b.split()[0] in KNOWN_GENERA:
        return None   # genere diverso ed esistente: probabilmente un'altra specie, non un errore
    return best[1]


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
# e i landmark, più piccolo per parcheggi e ornamentali (di solito alberelli) e per gli alberi agricoli e dei
# frutteti (potati bassi). In caso di più valori vince il primo della lista.
DEFAULT_CROWN_R = 3.0
DEFAULT_STUMP_R = 0.25  # m, raggio della X di un ceppo senza circumference
DEFAULT_CROWN_R_BY_DENOTATION = (("natural_monument", 5.0), ("landmark", 5.0), ("parking", 2.0), ("decoration", 2.0),
                                 ("agricultural", 2.5), ("orchard", 2.5))


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


def comma_tags(t):
    """Tag numerici scritti con la virgola decimale (2,9 invece di 2.9); diameter è controllato anche se ignorato."""
    return [k for k in NUMERIC_TAGS if "," in str(t.get(k, ""))]


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
    return [k for k in NUMERIC_TAGS if k in t and bad_number(t[k])]


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


# Nome della specie in una lingua: species:<codice lingua>, es. species:it, species:pl, species:en, species:zh-Hans
# (2-3 lettere, con eventuale variante). species:wikidata, species:wikipedia e simili non sono lingue.
SPECIES_LANG = re.compile(r"^species:[a-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")


def species_lang_only(t):
    """Nomi della specie in qualche lingua (species:xx) su un albero senza species=*: [(chiave, valore)]."""
    if t.get("species"):
        return []
    return sorted((k, v) for k, v in t.items() if SPECIES_LANG.match(k))


def undocumented_tags(t):
    """Tag non documentati presenti: [(chiave, valore)], esclusi quelli in UNDOC_IGNORE."""
    return [(k, t[k]) for k in UNDOC_TAGS if k in t and (k, t[k].strip()) not in UNDOC_IGNORE]


def slenderness(t):
    """height / circumference (anche da diameter); None per i ceppi o se manca una delle due misure."""
    if t.get("natural") == "tree_stump":
        return None
    c, h = circ_value(t)[0], num(t.get("height"))
    return h / c if c and h and c > 0 and h > 0 else None


def checks(t, pos):
    """Check QA di un albero con tag t in posizione pos ([lon, lat]): (codici degli errori, proprietà per i
    messaggi della scheda), in un solo passaggio.
    Per aggiungere un check: una riga qui con flag(codice, proprietà), e il messaggio in index.html."""
    issues, props = [], {}

    def flag(code, **p):
        issues.append(code)
        props.update(p)

    c, h, cd = circ_value(t)[0], num(t.get("height")), num(t.get("diameter_crown"))
    if c is not None and c > MAX_CIRCUMFERENCE:
        flag("circumference")
    if h is not None and h > MAX_HEIGHT:
        flag("height")
    if cd is not None and cd > MAX_CROWN:
        flag("crown")
    if comma := comma_tags(t):
        flag("comma", comma=";".join(comma))
    lim = species_limit(t) or {}  # limite per specie: solo se non è già scattato quello generico
    if "circumference" in lim and c is not None and "circumference" not in issues and c > lim["circumference"]:
        flag("circumference_species", lim_c=lim["circumference"])
    if "height" in lim and h is not None and "height" not in issues and h > lim["height"]:
        flag("height_species", lim_h=lim["height"])
    r = slenderness(t)  # solo se non è già segnalata la misura sbagliata (circonferenza o altezza oltre i limiti)
    if r is not None and r > SLENDER_MAX and not {"height", "height_species"} & set(issues):
        flag("too_slender", hc_ratio=round(r, 3), hc_circ=round(c, 2))
    if r is not None and r < STOCKY_MIN and not {"circumference", "circumference_species"} & set(issues):
        flag("too_stocky", hc_ratio=round(r, 3), hc_circ=round(c, 2))
    sp = t.get("species")
    if sp is not None and len(sp.split()) == 1:
        flag("species")
    if sp is not None and sp.strip()[:1].islower():
        flag("species_lowercase")
    if sp and SPECIES_BIN and (typo := species_typo(sp)):
        flag("species_typo", sp_sugg=typo)
    if lang := species_lang_only(t):    # "species:it=Leccio" separati da "\n" (i valori possono contenere ";")
        flag("species_lang_only", sp_lang="\n".join(f"{k}={v}" for k, v in lang))
    g = t.get("genus")
    if g is not None and len(g.split()) > 1:
        flag("genus")
    if g is not None and g.strip()[:1].islower():
        flag("genus_lowercase")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        flag("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        flag("leaf_cycle")
    if g is not None and sp is not None and len(g.split()) == 1:
        sg = genus_of_species(sp)
        if sg and g.casefold() != sg.casefold():
            flag("genus_mismatch")
    list_issues, list_props = species_list_mismatches(t)
    nt = not_taxon_tags(t)
    if any(k == "species:wikidata" for k, _ in nt) and "wikidata_mismatch" in list_issues:
        list_issues.remove("wikidata_mismatch")   # stesso tag: basta l'errore più preciso (non è un taxon)
        for k in ("sp_wd", "exp_wd", "exp_sp"):
            list_props.pop(k, None)
    issues.extend(list_issues)
    props.update(list_props)
    if nt:
        flag("wikidata_not_taxon", nt_tags=";".join(f"{k}={q}" for k, q in nt))
    if tx := taxon_wikidata(t):
        flag("wikidata_is_taxon", tx_wd=";".join(tx))
    if far := wikidata_far(t, pos):     # "QID:km;…"
        flag("wikidata_far", wd_far=";".join(f"{q}:{km}" for q, km in far))
    if bad := bad_numeric_tags(t):
        flag("number_invalid", bad_num=";".join(bad))
    if bad := bad_link_tags(t):
        flag("link_format", bad_links=";".join(bad))
    if (src := monument_sources(t)) and "natural_monument" not in t.get("denotation", "").split(";"):
        flag("monument_denotation", mon_src=";".join(src))
    if den := denotation_problems(t):   # valori, motivi e usi, nello stesso ordine
        flag("denotation_value", den_bad=";".join(v for v, _, _ in den), den_why=";".join(w for _, w, _ in den),
             den_n=";".join(str(n) for _, _, n in den))
    if bad := masaf_criteria_bad(t):    # parti separate da "," come nel tag (non possono contenerne)
        flag("masaf_criteria", masaf_bad=",".join(bad))
    if undoc := undocumented_tags(t):  # i tag non sono in KEEP: il valore va nella proprietà tag_<chiave>
        flag("undocumented_tag", **{"tag_" + k: v for k, v in undoc})
    return issues, props


KEEP = ["name", "species", "genus", "taxon",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "diameter_crown", "image", "wikimedia_commons", "wikidata", "start_date",
        "ref:masaf", "masaf:criteria", "ref:rami", "website", "url", "diameter", "image:fotoshi"]


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
        c, from_d = circ_value(t)
        if from_d and c:
            p["diam_used"] = True
        if circ:  # il tronco si disegna solo se c'è una circonferenza (reale o stimata)
            p["tr"] = round(circ / (2 * math.pi), 3)
            if circ_est:
                p["tr_est"] = True
        # leaf_type e leaf_cycle mancanti o non validi: stimati dalle liste (forma e colore della chioma)
        if t.get("leaf_type") not in LEAF_TYPE and (est := leaf_type_estimate(t)):
            p["lt"] = est
        if t.get("leaf_cycle") not in LEAF_CYCLE and (est := leaf_cycle_estimate(t)):
            p["lc"] = est
    if wd_img := wikidata_image(t):   # per la scheda, se l'albero non ha altre immagini
        p["wd_img"] = wd_img
    iss, props = checks(t, [el["lon"], el["lat"]])
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    p.update(props)
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
    # Un server Overpass può rispondere "senza errori" ma con dati mancanti (è successo: 0 alberi in Valle d'Aosta
    # mentre overpass-api.de dava 502). Se gli alberi sono meno di MIN_KEEP_RATIO rispetto all'ultimo aggiornamento,
    # i dati precedenti non si toccano.
    nodes = sum(1 for el in data["elements"] if el.get("type") == "node")
    prev = regions_index.get(slug, {}).get("count")
    if path.exists() and prev and nodes < prev * MIN_KEEP_RATIO:
        warn(f"{slug}: solo {nodes} alberi contro {prev} dell'ultimo aggiornamento: risposta di Overpass probabilmente "
             f"incompleta, mantengo i dati precedenti")
        return True
    update_wikidata_cache(data["elements"])   # prima dei check: servono ai check su Wikidata (taxon e distanza)
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
