#!/usr/bin/env python3
"""Scarica gli alberi OSM (natural=tree e natural=tree_stump) delle regioni di scripts/regions.json via Overpass,
calcola i check QA e scrive data/regions/<regione>.geojson.gz e data/index.json.

Uso: python scripts/fetch_trees.py [all | gruppo | regione ...]   (es. "liguria veneto", "italia", "liguria,bulgaria")
Le regioni (nome, codice ISO, bbox) sono in scripts/regions.json.
"""
import collections, concurrent.futures, datetime, functools, gzip, hashlib, json, math, os, re, sys, time, unicodedata
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
# Mirror Overpass di FairwayMapper (https://www.fairwaymapper.com/api/overpass): si usa per primo solo se c'è la
# chiave nella variabile d'ambiente FAIRWAYMAPPER_KEY (nell'Action viene dal secret omonimo, mai scritta nei file).
# Piano gratuito: 10 richieste al minuto e 2000 al mese. Se la chiave viene rifiutata o il limite è esaurito,
# per il resto dell'esecuzione si usano solo gli altri server.
# La chiave va nell'indirizzo (https://api.fairwaymapper.com/k/<chiave>/api/interpreter, la forma usata anche da
# overpass-turbo e JOSM): nei messaggi l'indirizzo compare come "FairwayMapper" e la chiave è sempre oscurata.
# Il secret può contenere la sola chiave (fm_…) o l'indirizzo intero copiato da overpass-turbo.
FM_URL = "FairwayMapper"   # segnaposto nell'elenco ENDPOINTS
_fm_raw = os.environ.get("FAIRWAYMAPPER_KEY", "").strip()
FM_KEY = (re.search(r"/k/([^/\s]+)", _fm_raw) or re.match(r"(\S*)", _fm_raw)).group(1)
FM_INTERVAL = 6.5          # secondi tra due richieste (massimo 10 al minuto)


def hide_key(s):
    return str(s).replace(FM_KEY, "***") if FM_KEY else str(s)
if FM_KEY:
    ENDPOINTS.insert(0, FM_URL)
_fm_last = 0.0


def fm_disable(reason):
    if FM_URL in ENDPOINTS:
        ENDPOINTS.remove(FM_URL)
        warn(f"FairwayMapper non usato per il resto dell'aggiornamento: {reason}")


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
    global _fm_last
    for url in list(ENDPOINTS):
        for attempt in range(2):
            if url not in ENDPOINTS:     # FairwayMapper disattivato nel frattempo
                break
            try:
                real = url
                if url == FM_URL:
                    time.sleep(max(0.0, _fm_last + FM_INTERVAL - time.time()))
                    _fm_last = time.time()
                    real = f"https://api.fairwaymapper.com/k/{FM_KEY}/api/interpreter"
                r = requests.post(real, data={"data": q}, headers=HEADERS, timeout=420)
                if url == FM_URL and r.status_code in (401, 403):
                    fm_disable(f"chiave rifiutata (HTTP {r.status_code}): {hide_key(r.text[:150])}")
                    break
                if url == FM_URL and r.status_code == 429:
                    fm_disable(f"limite di richieste raggiunto: {hide_key(r.text[:150])}")
                    break
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
                print(f" [!] {url} fallito ({label}, tentativo {attempt + 1}/2): {hide_key(e)[:200]}")
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
# Circonferenza più grande registrata: 43 m, baobab africani (Adansonia digitata), record Guinness:
# https://www.guinnessworldrecords.com/world-records/living-tree-with-greatest-girth
MAX_CIRCUMFERENCE = 43.0   # m
# Albero più alto: Hyperion (Sequoia sempervirens, Redwood National Park, California), 116,22 m:
# https://www.monumentaltrees.com/en/usa/california/delnortecounty/4462_redwoodnationalpark/9104/
MAX_HEIGHT = 116.22        # m
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
# Slancio rispetto alla specie (check too_slender_species, solo per le specie con height_circ_ratio): provato su
# 59.169 alberi, con queste soglie ne segnalava 81, quasi tutti con tronchi di pochi cm su alberi alti
SLENDER_SP_FACTOR = 4      # oltre 4 volte il rapporto tipico della specie
SLENDER_SP_MIN = 40        # ma mai sotto 40 (specie con rapporto basso, come l'olivo: i giovani sono più slanciati)
SLENDER_SP_MIN_H = 10      # m: solo alberi alti almeno 10 m
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json. Sono i record
# della pagina wiki https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/Tree_species_records, presi da
# monumentaltrees.com; "height_source" / "circumference_source" indicano una fonte diversa (es. "MASAF")
_RAW_LIMITS = load_json(HERE / "species_limits.json")
# rapporti medi (mediane) usati quando la specie non ne ha uno proprio
DEFAULT_RATIOS = _RAW_LIMITS.pop("_default", {})
SPECIES_LIMITS = {" ".join(k.split()).lower(): v for k, v in _RAW_LIMITS.items()}


# apici e virgolette (dritti, tipografici, accento grave/acuto) non contano nei nomi: Ulmus 'New Horizon' è
# uguale a Ulmus ‘New Horizon’ e a Ulmus `New Horizon`
QUOTES = str.maketrans("", "", "'‘’`´\"“”")


def norm_species(s):
    """Nome normalizzato per i confronti: senza virgolette, spazi singoli, 'x' tra i nomi come '×', '×' staccato dal
    nome ('Platanus ×acerifolia', come su Wikidata, = 'Platanus × acerifolia'), minuscolo."""
    words = s.translate(QUOTES).replace("×", " × ").split()
    return " ".join("×" if w in ("x", "X") else w for w in words).casefold()


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


# --- Wikidata: due cache, a ogni aggiornamento si interrogano solo i QID nuovi; se Wikidata non risponde i QID
# restano sconosciuti e i check relativi non scattano. Un elemento è un taxon se ha un nome scientifico (P225): è molto
# più veloce del controllo "istanza di taxon (Q16521) o di una sua sottoclasse" (37 s contro 4,5 s per 200 QID) e
# distingue allo stesso modo specie, illustrazioni, frutti e alberi monumentali. I QID reindirizzati (owl:sameAs) si
# seguono.
# data/wikidata_items.json, elementi di wikidata=* (i singoli alberi): {"Q…": {"taxon": bool, "coords": [[lon, lat]…]
#   (P625), "image": file di Commons (P18), "taxa": [QID di P10241 "individuo del taxon"], "heritage": [[etichetta it,
#   etichetta en] di P1435 "tutela"], "wp": {"it": url, "en": url} (voci di Wikipedia)}}
# data/wikidata_taxa.json, elementi di species:wikidata, genus:wikidata e delle specie di P10241: {"Q…": {"taxon": bool,
#   "names": [P225 nome scientifico], "ranks": [QID di P105 rango], "it": etichetta, "en": etichetta,
#   "alias": {"it": [alias nell'ordine di Wikidata], "en": […]}}}; ci sono anche le specie di species_list.json usate
#   negli alberi, per il nome comune (le etichette italiane delle piante sono quasi sempre il nome scientifico: i
#   nomi comuni stanno negli alias, es. Cercis siliquastrum -> "Siliquastro", "Albero di Giuda")
WD_ITEMS_FILE = DATA / "wikidata_items.json"
WD_ITEMS = load_json(WD_ITEMS_FILE)
WD_TAXA_FILE = DATA / "wikidata_taxa.json"
# le voci scritte prima dell'unificazione delle cache non hanno "taxon": si richiedono di nuovo una volta
WD_TAXA = {q: v for q, v in load_json(WD_TAXA_FILE).items() if "taxon" in v}
WDQS_URL = "https://query.wikidata.org/sparql"
WD_BATCH = 200
WD_MAX_DIST = 1000   # m: oltre, l'albero è "lontano" dall'elemento Wikidata collegato (nei dati il massimo è ~0,7 km)
POINT = re.compile(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)")
WP_SITES = {"it": "https://it.wikipedia.org/", "en": "https://en.wikipedia.org/"}
# ranghi (P105): species:wikidata non deve puntare a un genere o più su, genus:wikidata non a una specie o più giù
RANK_GENUS_OR_ABOVE = {"Q34740", "Q3238261", "Q3181348", "Q5998839", "Q3025161", "Q227936", "Q164280", "Q35409", "Q36602"}
RANK_SPECIES_OR_BELOW = {"Q7432", "Q68947", "Q767728", "Q279749"}


def qids(t, key):
    """QID validi (Q…) di un tag, anche separati da ';'."""
    return [x.strip() for x in str(t.get(key, "")).split(";") if WIKIDATA.match(x.strip())]


def taxon_status(q):
    """True se l'elemento è un taxon, False se non lo è, None se non è ancora stato verificato."""
    for cache in (WD_TAXA, WD_ITEMS):
        if q in cache:
            return cache[q].get("taxon")
    return None


def wdqs(sparql):
    """Righe del risultato di una query a Wikidata; solleva un'eccezione se non risponde."""
    r = requests.post(WDQS_URL, data={"query": sparql}, timeout=120,
                      headers={**HEADERS, "Accept": "application/sparql-results+json"})
    r.raise_for_status()
    return r.json()["results"]["bindings"]


def ask_wikidata(todo, query, store, label):
    """Interroga Wikidata a blocchi di WD_BATCH QID: query(batch) dà lo SPARQL, store(batch, righe) salva.
    Se Wikidata non risponde si ferma: i QID rimasti si verificano al prossimo aggiornamento."""
    for i in range(0, len(todo), WD_BATCH):
        batch = todo[i:i + WD_BATCH]
        try:
            store(batch, wdqs(query(" ".join("wd:" + q for q in batch))))
        except Exception as e:
            warn(f"Wikidata non risponde ({str(e)[:150]}): {len(todo) - i} QID restano da verificare ({label})")
            return
        time.sleep(1)


def qid_of(b, var):
    return b[var]["value"].rsplit("/", 1)[-1]


def add(lst, v):
    if v not in lst:
        lst.append(v)


def store_items(batch, rows):
    found = {q: {"taxon": False, "coords": [], "image": "", "taxa": [], "heritage": [], "wp": {}} for q in batch}
    for b in rows:
        e = found[qid_of(b, "item")]
        e["taxon"] = e["taxon"] or "name" in b
        if "coord" in b and (m := POINT.search(b["coord"]["value"])):
            add(e["coords"], [float(m.group(1)), float(m.group(2))])
        if "img" in b and not e["image"]:   # indirizzo .../Special:FilePath/<nome file codificato>
            e["image"] = unquote(b["img"]["value"].rsplit("/", 1)[-1])
        if "taxon" in b:
            add(e["taxa"], qid_of(b, "taxon"))
        if "her" in b:
            add(e["heritage"], [b.get("herIt", b.get("herEn", {})).get("value", qid_of(b, "her")),
                                b.get("herEn", b.get("herIt", {})).get("value", qid_of(b, "her"))])
        for lang in WP_SITES:
            if "wp" + lang in b:
                e["wp"][lang] = b["wp" + lang]["value"]
    WD_ITEMS.update(found)


def store_taxa_info(batch, rows):
    # gli alias (nomi comuni) si tengono: li aggiorna fetch_aliases solo per le specie che non li hanno
    found = {q: {"taxon": False, "names": [], "ranks": [], **({"alias": WD_TAXA[q]["alias"]} if "alias" in WD_TAXA.get(q, {}) else {})}
             for q in batch}
    for b in rows:
        e = found[qid_of(b, "item")]
        if "name" in b:
            e["taxon"] = True
            add(e["names"], b["name"]["value"])
        if "rank" in b:
            add(e["ranks"], qid_of(b, "rank"))
        for lang in ("it", "en"):
            if lang in b:
                e[lang] = b[lang]["value"]
    WD_TAXA.update(found)


def list_qid(sp):
    """QID della specie nella lista wiki (species_list.json), per un valore di species singolo."""
    sp = (sp or "").strip()
    return SPECIES_REF.get(norm_species(sp), {}).get("wikidata") if sp and ";" not in sp else None


def fetch_aliases(todo):
    """Alias in italiano e inglese, nell'ordine di Wikidata (l'API li dà ordinati, SPARQL no), a blocchi di 50.
    Se Wikidata non risponde si ferma: i QID rimasti si chiedono al prossimo aggiornamento."""
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        try:
            r = requests.get("https://www.wikidata.org/w/api.php", timeout=60, headers=HEADERS, params={
                "action": "wbgetentities", "ids": "|".join(batch), "props": "aliases", "languages": "it|en",
                "format": "json"})
            r.raise_for_status()
            ents = r.json()["entities"]
        except Exception as e:
            warn(f"Wikidata non risponde ({str(e)[:150]}): {len(todo) - i} QID restano senza alias")
            return
        for q in batch:   # i QID reindirizzati compaiono col QID di destinazione: restano senza alias
            al = ents.get(q, {}).get("aliases", {})
            WD_TAXA[q]["alias"] = {lang: [a["value"] for a in al.get(lang, [])] for lang in ("it", "en")}
        time.sleep(1)


def common_name(q, lang, sp):
    """Nome comune di una specie: l'etichetta di Wikidata nella lingua, se non è un nome scientifico, altrimenti il
    primo alias che non lo è (non è un nome scientifico dell'elemento o della species, non inizia con un genere noto,
    non ha il segno di ibrido). Con l'iniziale maiuscola; None se non c'è."""
    e = WD_TAXA.get(q, {})
    sci = {binomial(n) for n in e.get("names", [])} | ({binomial(sp)} if sp else set())
    for c in [e.get(lang)] + e.get("alias", {}).get(lang, []):
        c = (c or "").strip()
        if not c or "×" in c or binomial(c) in sci or c.split()[0].casefold() in KNOWN_GENERA:
            continue
        return c[0].upper() + c[1:]
    return None


# percorso che segue anche i QID reindirizzati (owl:sameAs)
def wd_prop(p):
    return f"(wdt:{p}|owl:sameAs/wdt:{p})"


def update_wikidata_cache(elements):
    """Chiede a Wikidata i dati dei QID usati dalla regione: per wikidata=* (alberi) se è un taxon,
    coordinate, immagine, specie (P10241), tutela (P1435) e voci di Wikipedia; per species:wikidata, genus:wikidata e
    le specie di P10241 nome scientifico, rango ed etichette. Aggiorna e salva le due cache.
    I QID si rileggono a ogni aggiornamento, anche se sono già in cache: così le correzioni fatte su Wikidata
    arrivano nei check (le cache sono piccole, poche richieste). Se Wikidata non risponde resta la cache."""
    tags = [el.get("tags", {}) for el in elements]
    wd = {q for t in tags for q in qids(t, "wikidata")}
    todo_items = sorted(wd)
    ask_wikidata(todo_items, lambda v: "SELECT ?item ?name ?coord ?img ?taxon ?her ?herIt ?herEn ?wpit ?wpen WHERE { "
                 "VALUES ?item { " + v + " } "
                 f"OPTIONAL {{ ?item {wd_prop('P225')} ?name }} OPTIONAL {{ ?item {wd_prop('P625')} ?coord }} "
                 f"OPTIONAL {{ ?item {wd_prop('P18')} ?img }} OPTIONAL {{ ?item wdt:P10241 ?taxon }} "
                 "OPTIONAL { ?item wdt:P1435 ?her . OPTIONAL { ?her rdfs:label ?herIt FILTER(LANG(?herIt) = 'it') } "
                 "OPTIONAL { ?her rdfs:label ?herEn FILTER(LANG(?herEn) = 'en') } } "
                 "OPTIONAL { ?wpit schema:about ?item ; schema:isPartOf <" + WP_SITES["it"] + "> } "
                 "OPTIONAL { ?wpen schema:about ?item ; schema:isPartOf <" + WP_SITES["en"] + "> } }",
                 store_items, "alberi")
    # specie: quelle dei tag, quelle indicate da P10241 degli alberi e quelle della lista wiki per i valori di species
    # (per il nome comune)
    listed = {q for t in tags if (q := list_qid(t.get("species")))}
    todo_taxa = sorted(({q for t in tags for k in ("species:wikidata", "genus:wikidata") for q in qids(t, k)}
                        | {x for q in wd for x in WD_ITEMS.get(q, {}).get("taxa", [])} | listed))
    ask_wikidata(todo_taxa, lambda v: "SELECT ?item ?name ?rank ?it ?en WHERE { VALUES ?item { " + v + " } "
                 f"OPTIONAL {{ ?item {wd_prop('P225')} ?name }} OPTIONAL {{ ?item {wd_prop('P105')} ?rank }} "
                 "OPTIONAL { ?item rdfs:label ?it FILTER(LANG(?it) = 'it') } "
                 "OPTIONAL { ?item rdfs:label ?en FILTER(LANG(?en) = 'en') } }", store_taxa_info, "specie")
    # alias (nomi comuni) delle specie: species:wikidata e lista wiki
    todo_alias = sorted(q for q in ({q for t in tags for q in qids(t, "species:wikidata")} | listed)
                        if q in WD_TAXA and "alias" not in WD_TAXA[q])
    fetch_aliases(todo_alias)
    for todo, path, cache in ((todo_items, WD_ITEMS_FILE, WD_ITEMS), (todo_taxa or todo_alias, WD_TAXA_FILE, WD_TAXA)):
        if todo:
            path.write_text(json.dumps(cache, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def taxon_names(q):
    """Nomi scientifici (P225) di un elemento Wikidata, dalla cache."""
    return WD_TAXA.get(q, {}).get("names", [])


def rank_problem(k, q):
    """Rango sbagliato per la chiave: un genere o più su per species:wikidata, una specie o più giù per genus:wikidata."""
    wrong = RANK_GENUS_OR_ABOVE if k == "species:wikidata" else RANK_SPECIES_OR_BELOW
    return next((r for r in WD_TAXA.get(q, {}).get("ranks", []) if r in wrong), None)


def wikidata_rank_problems(t):
    """[(chiave, QID, QID del rango)] di species:wikidata e genus:wikidata con il rango sbagliato."""
    return [(k, q, r) for k in ("species:wikidata", "genus:wikidata") for q in qids(t, k) if (r := rank_problem(k, q))]


def genus_key(g):
    return g.replace("×", " ").split()[0].casefold() if g.replace("×", " ").split() else ""


def wikidata_name_problems(t, skip_species):
    """[(chiave, QID, nome su Wikidata)] quando il nome scientifico (P225) dell'elemento di species:wikidata o
    genus:wikidata non corrisponde a species o genus. skip_species: species:wikidata già segnalato dalla lista wiki."""
    out = []
    for k, tag in (("species:wikidata", "species"), ("genus:wikidata", "genus")):
        val = (t.get(tag) or "").strip()
        if not val or ";" in val or (k == "species:wikidata" and skip_species):
            continue
        for q in qids(t, k):
            names = taxon_names(q)
            if not names or rank_problem(k, q):      # niente nome (non è un taxon) o rango sbagliato: altri check
                continue
            if k == "species:wikidata":
                ok = q in WD_WHITELIST.get(norm_species(val), ()) or binomial(val) in {binomial(n) for n in names}
            else:
                ok = genus_key(val) in {genus_key(n) for n in names}
            if not ok:
                out.append((k, q, names[0]))
    return out


def wikidata_tree_species(t):
    """[(QID, nome)] quando la specie dell'albero su Wikidata (P10241 dell'elemento di wikidata=*) non corrisponde
    a species:wikidata né a species (si confrontano i QID e i nomi scientifici)."""
    out = []
    sw, sp = qids(t, "species:wikidata"), (t.get("species") or "").strip()
    own = {binomial(n) for q2 in sw for n in taxon_names(q2)} | ({binomial(sp)} if sp and ";" not in sp else set())
    for q in qids(t, "wikidata"):
        taxa = WD_ITEMS.get(q, {}).get("taxa", [])
        names = [n for tx in taxa for n in taxon_names(tx)]
        if taxon_status(q) is True or not names or set(sw) & set(taxa) or not own:
            continue
        if not own & {binomial(n) for n in names}:
            out.append((q, names[0]))
    return out


def wikidata_heritage(t):
    """[(QID, [etichetta it, etichetta en])] se l'elemento di wikidata=* ha una tutela (P1435) e manca
    denotation=natural_monument."""
    if "natural_monument" in [x.strip() for x in str(t.get("denotation", "")).split(";")]:
        return []
    return [(q, WD_ITEMS[q]["heritage"][0]) for q in qids(t, "wikidata")
            if taxon_status(q) is not True and WD_ITEMS.get(q, {}).get("heritage")]


def wikidata_extras(t, wd_ok=True):
    """Proprietà per la scheda: nome comune della specie in it/en (vedi common_name) e voci di Wikipedia dell'albero
    (elemento di wikidata=*). La specie è quella di species:wikidata se è corretto (wd_ok), altrimenti quella della
    lista wiki per il valore di species."""
    p = {}
    sp = (t.get("species") or "").strip()
    q = (qids(t, "species:wikidata")[:1] if wd_ok else []) or [list_qid(sp)]
    if q[0]:
        for lang in ("it", "en"):
            if name := common_name(q[0], lang, sp):
                p["sp_" + lang] = name
    for q in qids(t, "wikidata")[:1]:
        for lang, url in WD_ITEMS.get(q, {}).get("wp", {}).items():
            p["wp_" + lang] = url
    return p


# --- Link rotti: image, website, url (indirizzi http) e file di Commons (wikimedia_commons=File:…) ---
# Stato in data/url_status.json: {"<url o commons:File:…>": {"s": stato, "d": "AAAA-MM-GG"}}; ogni link si
# ricontrolla dopo URL_RECHECK_DAYS giorni. Si segnalano solo i link sicuramente rotti (404, 410, dominio
# inesistente, file di Commons mancante): 403, 429, errori del server e timeout sono spesso blocchi verso i
# programmi automatici, non link rotti.
URL_FILE = DATA / "url_status.json"
URL_STATUS = load_json(URL_FILE)
URL_RECHECK_DAYS = 30
URL_KEYS = ("image", "website", "url")
URL_BROKEN = {"404", "410", "dns", "missing"}
URL_WORKERS = 8        # siti controllati in parallelo; i link dello stesso sito uno alla volta
URL_PAUSE = 0.5        # s tra due link dello stesso sito
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def link_urls(t):
    """[(chiave, url)] degli indirizzi http in image, website e url (anche più valori separati da ';')."""
    return [(k, u) for k in URL_KEYS for u in (x.strip() for x in str(t.get(k, "")).split(";")) if URL.match(u)]


def commons_files(t):
    """File di Commons in wikimedia_commons (le categorie no)."""
    return [x.strip() for x in str(t.get("wikimedia_commons", "")).split(";") if x.strip().startswith("File:")]


def url_state(u):
    """Stato di un indirizzo: il codice HTTP finale, "dns" se il dominio non esiste, oppure timeout/conn/err."""
    try:
        r = requests.head(u, allow_redirects=True, timeout=15, headers=HEADERS)
        if r.status_code in (400, 403, 405, 501):   # alcuni siti non accettano HEAD: si riprova con GET
            r = requests.get(u, allow_redirects=True, timeout=15, headers=HEADERS, stream=True)
            r.close()
        return str(r.status_code)
    except requests.exceptions.Timeout:
        return "timeout"
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        return "dns" if any(x in msg for x in ("NameResolution", "Failed to resolve", "getaddrinfo failed",
                                                "Name or service not known", "nodename nor servname")) else "conn"
    except Exception:
        return "err"


def update_url_cache(elements):
    """Controlla i link e i file di Commons usati e non ancora verificati (o verificati da più di
    URL_RECHECK_DAYS giorni); aggiorna e salva data/url_status.json."""
    today = datetime.date.today()
    stale = lambda key: key not in URL_STATUS or (today - datetime.date.fromisoformat(URL_STATUS[key]["d"])).days >= URL_RECHECK_DAYS
    tags = [el.get("tags", {}) for el in elements]
    urls = sorted({u for t in tags for _, u in link_urls(t) if stale(u)})
    files = sorted({f for t in tags for f in commons_files(t) if stale("commons:" + f)})
    by_host = collections.defaultdict(list)
    for u in urls:
        by_host[urlparse(u).hostname or ""].append(u)

    def check_host(host_urls):
        out = {}
        for u in host_urls:
            out[u] = url_state(u)
            time.sleep(URL_PAUSE)
        return out

    with concurrent.futures.ThreadPoolExecutor(URL_WORKERS) as ex:
        for res in ex.map(check_host, by_host.values()):
            URL_STATUS.update({u: {"s": s, "d": today.isoformat()} for u, s in res.items()})
    for i in range(0, len(files), 50):   # file di Commons: 50 titoli per richiesta all'API
        batch = files[i:i + 50]
        try:
            r = requests.get(COMMONS_API, headers=HEADERS, timeout=60, params={
                "action": "query", "titles": "|".join(batch), "format": "json", "formatversion": 2})
            r.raise_for_status()
            q = r.json()["query"]
            norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
            missing = {p["title"] for p in q.get("pages", []) if p.get("missing") or p.get("invalid")}
            for f in batch:
                URL_STATUS["commons:" + f] = {"s": "missing" if norm.get(f, f) in missing else "ok", "d": today.isoformat()}
        except Exception as e:
            warn(f"Commons non risponde ({str(e)[:150]}): {len(files) - i} file restano da verificare")
            break
    if urls or files:
        broken = sum(1 for k in urls + ["commons:" + f for f in files] if URL_STATUS.get(k, {}).get("s") in URL_BROKEN)
        print(f" link controllati: {len(urls)} indirizzi, {len(files)} file di Commons ({broken} rotti)")
        URL_FILE.write_text(json.dumps(URL_STATUS, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# Autore e licenza delle foto di Fotoshi (image:fotoshi): dalla descrizione della pagina della foto
# ("… · Photo by @utente · CC BY 4.0"). Il sito non può leggerla direttamente (Fotoshi non permette le richieste
# da altri siti), quindi si legge qui e resta in data/fotoshi.json ({"<id>": {"author": …, "license": …}}).
FOTOSHI_FILE = DATA / "fotoshi.json"
FOTOSHI = load_json(FOTOSHI_FILE)
FOTOSHI_ID = re.compile(r"^[A-Za-z0-9_-]{10,}$")
FOTOSHI_CREDIT = re.compile(r"Photo by\s+([^·<\"]+?)\s*·\s*([^<\"]+?)\s*\"")


def update_fotoshi_cache(elements):
    """Legge autore e licenza delle foto di Fotoshi non ancora in cache; aggiorna e salva data/fotoshi.json."""
    ids = sorted({t["image:fotoshi"].strip() for t in (el.get("tags", {}) for el in elements)
                  if FOTOSHI_ID.match(t.get("image:fotoshi", "").strip())} - FOTOSHI.keys())
    for pid in ids:
        try:
            r = requests.get(f"https://fotoshi.com/photo/{pid}", headers=HEADERS, timeout=30)
            if r.status_code == 404:
                FOTOSHI[pid] = {}
                continue
            r.raise_for_status()
            m = FOTOSHI_CREDIT.search(r.text)
            FOTOSHI[pid] = {"author": m.group(1), "license": m.group(2)} if m else {}
        except Exception as e:
            warn(f"Fotoshi non risponde ({str(e)[:120]}): {pid} resta da leggere")
        time.sleep(URL_PAUSE)
    if ids:
        FOTOSHI_FILE.write_text(json.dumps(FOTOSHI, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def broken_links(t):
    """[(chiave, valore, stato)] dei link sicuramente rotti dell'albero."""
    out = [(k, u, URL_STATUS[u]["s"]) for k, u in link_urls(t) if URL_STATUS.get(u, {}).get("s") in URL_BROKEN]
    out += [("wikimedia_commons", f, "missing") for f in commons_files(t)
            if URL_STATUS.get("commons:" + f, {}).get("s") == "missing"]
    return out


def wikidata_image(t):
    """File di Commons dell'immagine (P18) dell'elemento di wikidata=*, se non è un taxon (sarebbe la foto della
    specie, non dell'albero); None se manca."""
    for q in qids(t, "wikidata"):
        if taxon_status(q) is not True and WD_ITEMS.get(q, {}).get("image"):
            return WD_ITEMS[q]["image"]
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
        coords = WD_ITEMS.get(q, {}).get("coords")
        if taxon_status(q) is True or not coords:
            continue
        d = min(distance_m(pos, c) for c in coords)
        if d > WD_MAX_DIST:
            out.append((q, round(d / 1000, 2)))
    return out


def not_taxon_tags(t):
    """species:wikidata e genus:wikidata che puntano a un elemento che non è un taxon: [(chiave, QID)]."""
    return [(k, q) for k in ("species:wikidata", "genus:wikidata") for q in qids(t, k) if taxon_status(q) is False]


def taxon_wikidata(t):
    """QID di wikidata=* che sono taxon (wikidata dovrebbe indicare il singolo albero)."""
    return [q for q in qids(t, "wikidata") if taxon_status(q) is True]


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
    g = genus_words(g)[0] if g and len(genus_words(g)) == 1 else (genus_of_species(t["species"]) if t.get("species") else "")
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
        exp_for, exp_lt, exp_lc = (g[0], g[1].get("leaf_type"), g[1].get("leaf_cycle") or []) if g else (None, None, [])
        if g:
            props["exp_genus"] = True   # exp_for è un genere: il sito scrive "il genere X" / "the genus X"
    lt, lc = t.get("leaf_type"), t.get("leaf_cycle")
    if exp_for and lt in LEAF_TYPE and exp_lt and not type_ok(lt, exp_lt):
        issues.append("leaf_type_mismatch")
        props.update(exp_lt=exp_lt, exp_for=exp_for)
    if exp_for and lc in LEAF_CYCLE and exp_lc and not cycle_ok(lc, exp_lc):
        issues.append("leaf_cycle_mismatch")
        props.update(exp_lc=";".join(exp_lc), exp_for=exp_for)   # più cicli separati da ";": li unisce il sito
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


# diameter: senza unità è in millimetri (https://wiki.openstreetmap.org/wiki/Key:diameter), a differenza di
# circumference e diameter_crown, che sono in metri
DIAM_UNITS = {"mm": 0.001, "cm": 0.01, "m": 1, "metre": 1, "metres": 1, "meter": 1, "meters": 1,
              "ft": 0.3048, "feet": 0.3048, "foot": 0.3048, "in": 0.0254, "inch": 0.0254, "inches": 0.0254}
DIAM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*([a-zA-Z]*)\s*$")
DIAM_MM_MIN = 10   # sotto: diameter senza unità troppo piccolo per i millimetri (tronco < 1 cm), check diameter_unit
# circumference e diameter insieme: segnalati se la circonferenza si scosta più del 20% da π × diametro (check
# circ_diam_mismatch). Il margine lascia passare le imprecisioni di misura (tronchi non rotondi, quote diverse);
# nei dati gli scarti sono quasi tutti sotto il 10% o molto grandi
CIRC_DIAM_TOL = 0.2


def diameter_small(t):
    """Vero se diameter non ha unità e vale meno di DIAM_MM_MIN (es. diameter=0.5): in millimetri sarebbe un
    tronco di meno di 1 cm. Lo zero è già segnalato da number_invalid."""
    m = DIAM_RE.match(str(t.get("diameter", "")))
    return bool(m) and not m.group(2) and 0 < float(m.group(1).replace(",", ".")) < DIAM_MM_MIN


def diameter_m(t):
    """Diametro del tronco in metri da diameter=*: senza unità in mm, altrimenti con l'unità scritta
    (mm, cm, m, ft, in). None se manca, non è leggibile o è troppo piccolo per i millimetri (diameter_small)."""
    m = DIAM_RE.match(str(t.get("diameter", "")))
    if not m or diameter_small(t):
        return None
    f = DIAM_UNITS.get(m.group(2).lower() or "mm")
    return float(m.group(1).replace(",", ".")) * f if f else None


def circ_value(t):
    """Circonferenza in m, e se deriva da diameter. Se manca circumference ma c'è diameter si usa
    π × diametro (vedi diameter_m); se ci sono entrambi i tag, diameter è ignorato."""
    if "circumference" in t:
        return num(t["circumference"]), False
    d = diameter_m(t)
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


# --- Forma di species: specie non identificata (sp., spp.) e autore del nome (L., (L.) Kuntze, Merr.…) ---
UNIDENTIFIED = re.compile(r"^\s*(×\s*)?(\S+)\s+(sp|spp)\.?\s*$", re.I)
INFRA = {"subsp.", "ssp.", "var.", "subvar.", "f.", "forma", "cv.", "nothosubsp.", "nothovar."}
# autore: iniziale maiuscola e punto (L., Mill., J.), oppure tra parentesi ((L.), (Lindl.))
AUTHOR = re.compile(r"^\(?[A-ZÀ-Ý][\w'’.-]*\.\)?$|^\([^)]*[A-ZÀ-Ý][^)]*\)$|^\([A-ZÀ-Ý]")
AUTHOR_LINK = {"ex", "&", "et", "in"}   # parole che uniscono due autori: "Kuntze ex Bureau"


def species_unidentified(sp):
    """Il genere, se species è "Genere sp." o "Genere spp." (specie non identificata); altrimenti None."""
    m = UNIDENTIFIED.match(sp or "")
    return m.group(2) if m else None


EQ_KEYS = ("species", "genus", "taxon", "name", "start_date", "ref:masaf", "ref:rami")   # check tag_in_value


def not_latin(v):
    """True se il valore contiene lettere di un alfabeto non latino (cirillico, greco…); le lettere accentate latine vanno bene."""
    return any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in str(v or ""))
SCI_NAME = re.compile(r"^(×\s*)?[A-Z][a-zë-]+\s+(×\s*)?[a-zë-]+$")   # forma di un nome scientifico: Genere epiteto


def species_common_name(sp):
    """Se species non ha la forma di un nome scientifico perché la seconda parola è maiuscola (nomi comuni come
    "Black Matipo", "Pino Marittimo", cultivar senza virgolette come "Malus Golden Hornet"):
    il nome scientifico se è scritto tra parentesi ("Black Matipo (Pittosporum tenuifolium)"), altrimenti "".
    None se species ha la forma di un nome scientifico. Le cultivar tra virgolette e gli autori non contano."""
    if not sp or ";" in sp:
        return None
    words = [w for w in sp.split() if w != "×"]
    if len(words) < 2:
        return None                              # una parola sola: c'è già il check apposito
    second = words[1]
    if not second[:1].isupper() or second[:1] in "'‘`\"“(" or AUTHOR.match(second):
        return None
    m = re.search(r"\(([^)]*)\)", sp)
    return m.group(1).strip() if m and SCI_NAME.match(m.group(1).strip()) else ""


def species_epithet_case(sp):
    """Se species è un nome scientifico scritto con l'epiteto maiuscolo: il nome corretto. Vale per i nomi della
    lista ("Catalpa Bignonioides" -> "Catalpa bignonioides") e per i nomi di due parole con un genere noto
    ("Wisteria Sinensis" -> "Wisteria sinensis"); altrimenti None."""
    if species_common_name(sp) is None:
        return None
    if binomial(sp) in SPECIES_BIN:
        return SPECIES_BIN[binomial(sp)]
    words = [w for w in sp.split() if w != "×"]
    if len(words) == 2 and words[0].casefold() in KNOWN_GENERA:
        return f"{words[0]} {words[1].lower()}"
    return None


def species_without_author(sp):
    """species senza l'autore del nome, se c'è (es. "Camellia sinensis (L.) Kuntze" -> "Camellia sinensis");
    restano genere, epiteto, i ranghi infraspecifici (var., subsp. …) e le cultivar tra virgolette. None se non c'è
    autore."""
    if not sp or ";" in sp or species_unidentified(sp):
        return None
    words = sp.split()
    base = 3 if "×" in words[:2] else 2          # genere ed epiteto (con il segno di ibrido)
    if len(words) <= base or not words[base - 1][:1].islower():
        return None                               # epiteto maiuscolo: non è un nome scientifico (es. "Black Matipo (…)")
    kept, found, in_author, quoted, i = words[:base], False, False, False, base
    while i < len(words):
        w = words[i]
        if quoted or w[:1] in "'‘`\"“":             # cultivar tra virgolette: si tiene tutta
            kept.append(w)
            quoted = not (w[-1:] in "'’`\"”" and (len(w) > 1 or quoted))
            in_author = False
        elif w.lower() in INFRA and i + 1 < len(words):
            kept += [w, words[i + 1]]
            i += 1
            in_author = False
        elif AUTHOR.match(w) or (in_author and (w[:1].isupper() or w in AUTHOR_LINK)):
            found = in_author = True
        else:
            kept.append(w)
            in_author = False
        i += 1
    return " ".join(kept) if found else None


def genus_of_species(sp):
    """Prima parola di species, saltando il segno di ibrido (× o x iniziale)."""
    words = sp.split(";")[0].split()
    while words and words[0] in ("×", "x", "X"):
        words = words[1:]
    return words[0] if words else ""


def genus_words(g):
    """Parole di genus senza il segno di ibrido iniziale: "× Sycoparrotia" e "x Sycoparrotia" sono una parola sola
    (generi ibridi intergenerici); anche "×Sycoparrotia" attaccato perde il segno."""
    words = g.split()
    while words and words[0] in ("×", "x", "X"):
        words = words[1:]
    if words and words[0].startswith("×"):
        words[0] = words[0][1:]
    return words


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
    if diameter_small(t):   # come comma, vale anche se c'è circumference e diameter non si usa
        flag("diameter_unit")
    if "circumference" in t and c and c > 0 and (d := diameter_m(t)) and abs(c / (math.pi * d) - 1) > CIRC_DIAM_TOL:
        flag("circ_diam_mismatch", cd_circ=round(math.pi * d, 2))   # circonferenza che corrisponde a diameter
    lim = species_limit(t) or {}  # limite per specie: solo se non è già scattato quello generico
    if "circumference" in lim and c is not None and "circumference" not in issues and c > lim["circumference"]:
        flag("circumference_species", lim_c=lim["circumference"],
             **({"lim_c_src": lim["circumference_source"]} if "circumference_source" in lim else {}))
    if "height" in lim and h is not None and "height" not in issues and h > lim["height"]:
        flag("height_species", lim_h=lim["height"], **({"lim_h_src": lim["height_source"]} if "height_source" in lim else {}))
    r = slenderness(t)  # solo se non è già segnalata la misura sbagliata (circonferenza o altezza oltre i limiti)
    if r is not None and r > SLENDER_MAX and not {"height", "height_species"} & set(issues):
        flag("too_slender", hc_ratio=round(r, 3), hc_circ=round(c, 2))
    # troppo slanciato per la specie: oltre SLENDER_SP_FACTOR volte il rapporto tipico (height_circ_ratio), e almeno
    # SLENDER_SP_MIN; solo da SLENDER_SP_MIN_H m di altezza, perché gli alberelli appena piantati sono sottili
    r0 = lim.get("height_circ_ratio")
    if (r is not None and r0 and h is not None and h >= SLENDER_SP_MIN_H and r <= SLENDER_MAX
            and r > max(SLENDER_SP_FACTOR * r0, SLENDER_SP_MIN) and not {"height", "height_species"} & set(issues)):
        flag("too_slender_species", hc_ratio=round(r, 3), hc_circ=round(c, 2), sp_ratio=r0)
    if r is not None and r < STOCKY_MIN and not {"circumference", "circumference_species"} & set(issues):
        flag("too_stocky", hc_ratio=round(r, 3), hc_circ=round(c, 2))
    # un tag scritto dentro un valore (es. genus=leaf_type=broadleaved); i link (image, website, url) non contano,
    # perché negli indirizzi "=" è normale. Con "=" in species i check sulla sua forma non si ripetono
    if eq := [k for k in EQ_KEYS if "=" in str(t.get(k, ""))]:
        flag("tag_in_value", eq_keys=";".join(eq))
    # species o genus in un alfabeto non latino (es. genus=череша): con questo errore non si ripetono i check sulla forma
    if nl := [k for k in ("species", "genus") if k not in eq and not_latin(t.get(k))]:
        flag("not_latin", nl_keys=";".join(nl))
    sp = t.get("species")
    spf = None if "species" in eq or "species" in nl else sp   # species per i check sulla forma del nome
    one_word = spf is not None and len(spf.split()) == 1
    if one_word:
        flag("species")
    # con una sola parola basta l'errore precedente: la correzione è la stessa, minuscola o no
    if spf is not None and not one_word and spf.strip()[:1].islower():
        flag("species_lowercase")
    if spf and (g_un := species_unidentified(spf)):
        flag("species_unidentified", sp_genus=g_un)
    if spf and (ec := species_epithet_case(spf)):                # nome della lista con l'epiteto maiuscolo
        flag("species_epithet_case", sp_sci=ec)
    elif spf and (common := species_common_name(spf)) is not None:  # "" se non c'è un nome scientifico da proporre
        flag("species_common_name", **({"sp_sci": common} if common else {}))
    if spf and (no_auth := species_without_author(spf)):
        flag("species_author", sp_clean=no_auth)
    # nessun nome scientifico contiene parentesi; non si ripete se le spiegano già l'autore o il nome comune
    if spf and ("(" in spf or ")" in spf) and not {"species_author", "species_common_name"} & set(issues):
        flag("species_parentheses")
    if und := [k for k in ("species", "genus") if "_" in str(t.get(k, "")) and k not in eq]:
        flag("underscore", und_keys=";".join(und))
    if spf and SPECIES_BIN and (typo := species_typo(spf)):
        flag("species_typo", sp_sugg=typo)
    if lang := species_lang_only(t):    # "species:it=Leccio" separati da "\n" (i valori possono contenere ";")
        flag("species_lang_only", sp_lang="\n".join(f"{k}={v}" for k, v in lang))
    g = t.get("genus")
    gf = None if "genus" in eq or "genus" in nl else g   # genus per i check sulla forma del nome
    gw = genus_words(gf) if gf is not None else None    # senza il segno di ibrido (× Sycoparrotia)
    if gw is not None and len(gw) > 1:
        flag("genus")
    if gw and gw[0][:1].islower():
        flag("genus_lowercase")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        flag("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        flag("leaf_cycle")
    # species di una sola parola minuscola (es. species=alba): è un epiteto, non un genere da confrontare
    sp_epithet = sp is not None and len(sp.split()) == 1 and sp.strip()[:1].islower()
    if g is not None and sp is not None and len(gw := genus_words(g)) == 1 and not sp_epithet and not nl:
        sg = genus_of_species(sp).lstrip("×")   # il segno di ibrido non conta, né nel genus né nella species
        if sg and gw[0].casefold() != sg.casefold():
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
    # dati di Wikidata (cache di update_wikidata_cache): valori "chiave=QID:valore" separati da ";"
    if rk := wikidata_rank_problems(t):
        flag("wikidata_rank", wd_rank=";".join(f"{k}={q}:{r}" for k, q, r in rk))
    if nm := wikidata_name_problems(t, "wikidata_mismatch" in list_issues):
        flag("wikidata_name_mismatch", wd_name=";".join(f"{k}={q}:{n}" for k, q, n in nm))
    if ts := wikidata_tree_species(t):
        flag("wikidata_tree_species", wd_tree_sp=";".join(f"{q}:{n}" for q, n in ts))
    if hr := wikidata_heritage(t):
        q, (label_it, label_en) = hr[0]
        flag("wikidata_heritage", wd_her_q=q, wd_her_it=label_it, wd_her_en=label_en)
    if far := wikidata_far(t, pos):     # "QID:km;…"
        flag("wikidata_far", wd_far=";".join(f"{q}:{km}" for q, km in far))
    if bad := bad_numeric_tags(t):
        flag("number_invalid", bad_num=";".join(bad))
    if bad := bad_link_tags(t):
        flag("link_format", bad_links=";".join(bad))
    if bl := broken_links(t):           # righe "chiave=valore (stato)" separate da "\n" (i valori possono contenere ";")
        flag("broken_link", bad_url="\n".join(f"{k}={v} ({s})" for k, v, s in bl))
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
        "ref:masaf", "masaf:criteria", "ref:rami", "website", "url", "diameter", "image:fotoshi", "ref:zapis", "sacred_tree", "religion", "multi_stemmed"]

# Alberi da frutto (rendering: puntini colorati sulla chioma): per ora solo Malus domestica (le mele). Solo specie
# coltivate per i frutti, non i generi interi (molti Malus e Prunus sono ornamentali). Colore del frutto per specie.
FRUIT_SPECIES = {"Malus domestica": "#d63a2f"}
FRUIT_BY_NAME = {}   # riempito alla prima chiamata, con i nomi normalizzati (norm_species)


def fruit_color(t):
    """Colore dei frutti da disegnare, dalla specie (FRUIT_SPECIES); None se l'albero non è tra quelli da frutto."""
    if not FRUIT_BY_NAME:
        FRUIT_BY_NAME.update({norm_species(k): v for k, v in FRUIT_SPECIES.items()})
    return FRUIT_BY_NAME.get(norm_species(t.get("species") or ""))


def stem_radii(t, trunk_r):
    """Raggi (m) dei tronchi di un albero con multi_stemmed=*, oppure None. yes (o un valore non numerico) = 2 tronchi,
    un intero = quel numero di tronchi (dividendo l'area del tronco), più valori con ";" o decimali = circonferenze dei
    singoli tronchi. Senza un tronco da disegnare (né circonferenze), None."""
    v = str(t.get("multi_stemmed", "")).strip().lower()
    if not v or v == "no":
        return None
    parts = [x.strip() for x in v.split(";") if x.strip()]
    nums = [num(x) for x in parts]
    if len(parts) > 1 or (len(parts) == 1 and nums[0] is not None and not parts[0].isdigit()):
        circs = [x for x in nums if x and x > 0]
        return [c / (2 * math.pi) for c in circs] if len(circs) > 1 else None
    n = int(parts[0]) if len(parts) == 1 and parts[0].isdigit() and 2 <= int(parts[0]) <= 12 else 2
    return [trunk_r / math.sqrt(n)] * n if trunk_r else None


def is_sacred(t):
    """Albero sacro: zapis serbo (ref:zapis=*), place_of_worship=sacred_tree o la sua sottocategoria sacred_tree=*
    (es. sacred_tree=rag_tree, i rag tree di Irlanda e Scozia), goshinboku giapponese (religion=shinto,
    https://wiki.openstreetmap.org/wiki/Tag:religion%3Dshinto)."""
    return ("ref:zapis" in t or t.get("place_of_worship") == "sacred_tree" or "sacred_tree" in t
            or t.get("religion") == "shinto")


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
    if is_sacred(t):   # contorno azzurro sulla mappa
        p["sacred"] = 1
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
        # decorazioni disegnate da zoom 16 (index.html): tronchi multipli, edera, frutti
        if stems := stem_radii(t, p.get("tr")):
            p["stems"] = ";".join(f"{r:.3f}" for r in stems)
        if t.get("ivyclad") == "yes":
            p["ivy"] = 1
        if fc := fruit_color(t):
            p["fruit"] = fc
        # leaf_type e leaf_cycle mancanti o non validi: stimati dalle liste (forma e colore della chioma)
        if t.get("leaf_type") not in LEAF_TYPE and (est := leaf_type_estimate(t)):
            p["lt"] = est
        if t.get("leaf_cycle") not in LEAF_CYCLE and (est := leaf_cycle_estimate(t)):
            p["lc"] = est
    if wd_img := wikidata_image(t):   # per la scheda, se l'albero non ha altre immagini
        p["wd_img"] = wd_img
    if (fs := FOTOSHI.get(t.get("image:fotoshi", "").strip())) and fs.get("author"):   # crediti della foto
        p["fs_author"], p["fs_license"] = fs["author"], fs.get("license", "")
    iss, props = checks(t, [el["lon"], el["lat"]])
    # nome comune della specie e voci di Wikipedia, per la scheda; se species:wikidata è sbagliato il nome comune
    # viene dalla lista wiki
    wd_bad = {"wikidata_mismatch", "wikidata_not_taxon", "wikidata_rank", "wikidata_name_mismatch"} & set(iss)
    p.update(wikidata_extras(t, not wd_bad))
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    p.update(props)
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


BIG_TRUNK = {"circumference", "circumference_species"}   # tronchi già segnalati come troppo grandi: niente confronto


def neighbour_checks(feats):
    """Check tra alberi vicini (non i ceppi), dopo quelli del singolo albero:
    same_point = stesse identiche coordinate di un altro albero (sp_nodes: gli altri id);
    trunk_overlap = distanza tra i nodi minore della somma dei raggi dei tronchi misurati (non stimati, non già troppo
    grandi); un albero senza tronco misurato conta come un punto, segnalato se cade dentro il tronco di un altro.
    to_nodes: "id:distanza:raggi:tipo" separati da ";", tipo o = tronchi sovrapposti (raggi = somma dei due),
    i = questo nodo è dentro il tronco dell'altro, c = questo tronco contiene l'altro nodo (raggi = raggio del tronco)."""
    trees = [f for f in feats if not f["properties"].get("stump")]
    same, overlap = collections.defaultdict(list), collections.defaultdict(list)
    by_pt = collections.defaultdict(list)
    for f in trees:
        by_pt[tuple(f["geometry"]["coordinates"])].append(f["properties"]["id"])
    for ids in by_pt.values():
        for i in ids:
            same[i] += [j for j in ids if j != i]
    # alberi in celle di 15 m: i tronchi plausibili (< 43 m di circonferenza) non superano 6,9 m di raggio. Raggio 0 per
    # chi non ha un tronco misurato (stimato, mancante o già troppo grande): conta come un punto
    CELL = 15.0
    grid = collections.defaultdict(list)
    for f in trees:
        p = f["properties"]
        measured = p.get("tr") and not p.get("tr_est") and not BIG_TRUNK & set(p["issues"].strip("|").split("|"))
        lon, lat = f["geometry"]["coordinates"]
        x, y = lon * 111320 * math.cos(math.radians(lat)), lat * 111320
        grid[(int(x // CELL), int(y // CELL))].append((p["id"], x, y, p["tr"] if measured else 0))
    for (cx, cy), cell in grid.items():
        near = [t for dx in (-1, 0, 1) for dy in (-1, 0, 1) for t in grid.get((cx + dx, cy + dy), [])]
        for a in cell:
            for b in near:
                if b[0] <= a[0] or not (a[3] or b[3]) or b[0] in same.get(a[0], ()):
                    continue
                d = math.hypot(a[1] - b[1], a[2] - b[2])
                if d < a[3] + b[3]:
                    if a[3] and b[3]:
                        overlap[a[0]].append(f"{b[0]}:{d:.2f}:{a[3] + b[3]:.2f}:o")
                        overlap[b[0]].append(f"{a[0]}:{d:.2f}:{a[3] + b[3]:.2f}:o")
                    else:   # uno solo ha il tronco: l'altro nodo ci cade dentro
                        t, pt = (a, b) if a[3] else (b, a)
                        overlap[pt[0]].append(f"{t[0]}:{d:.2f}:{t[3]:.2f}:i")
                        overlap[t[0]].append(f"{pt[0]}:{d:.2f}:{t[3]:.2f}:c")
    for f in trees:
        p, extra = f["properties"], []
        if same.get(p["id"]):
            extra.append("same_point")
            p["sp_nodes"] = ";".join(map(str, sorted(same[p["id"]])))
        if overlap.get(p["id"]):
            extra.append("trunk_overlap")
            p["to_nodes"] = ";".join(overlap[p["id"]])
        if extra:
            p["issues"] = "|" + "|".join([*p["issues"].strip("|").split("|"), *extra]).lstrip("|") + "|"


def build(data):
    feats = [to_feature(e) for e in data["elements"] if e.get("type") == "node"]
    feats.sort(key=lambda f: f["properties"]["id"])  # ordine stabile: se i dati non cambiano, il file resta identico
    neighbour_checks(feats)
    return {"type": "FeatureCollection", "features": feats}


def write_gz(path, raw):
    """Scrive un file .gz identico se i dati sono identici (mtime=0, nessun nome di file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(raw)


def minimal(fc):
    """File ridotto per il caricamento in due fasi: solo gli alberi visibili a zoom basso (con errori o
    monumentali). Il sito lo carica e lo mostra subito, mentre il file completo si carica in secondo piano."""
    feats = [f for f in fc["features"] if f["properties"]["issues"] != "|"
             or "natural_monument" in str(f["properties"].get("denotation", "")).split(";")]
    return json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    update_url_cache(data["elements"])        # e al check sui link rotti
    update_fotoshi_cache(data["elements"])    # autore e licenza delle foto di Fotoshi, per la scheda
    fc = build(data)
    raw = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    errors = sum(1 for f in fc["features"] if f["properties"]["issues"] != "|")  # alberi con almeno un errore
    min_path = OUT_DIR / f"{slug}.min.geojson.gz"
    if regions_index.get(slug, {}).get("sha1") == digest and path.exists():
        regions_index[slug]["errors"] = errors  # per gli indici scritti prima che esistesse il conteggio
        regions_index[slug]["diam"] = "mm"      # vedi sotto
        if not min_path.exists():               # regioni scaricate prima che esistesse il file ridotto
            write_gz(min_path, minimal(fc))
        print(f"{slug}: nessuna modifica ({len(fc['features'])} alberi)")
        return True
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_gz(path, raw)
    write_gz(min_path, minimal(fc))
    # "diam": "mm" = dati scritti leggendo diameter senza unità in millimetri; senza, il sito corregge da sé i dati
    # vecchi (fixDiameter in index.html)
    regions_index[slug] = {"name": region["name"], "count": len(fc["features"]), "errors": errors,
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha1": digest, "diam": "mm"}
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
            groups.setdefault(r["group"].lower().replace(" ", "-"), []).append(r)   # "Regno Unito" -> regno-unito
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
    # fixDiameter in index.html corregge le regioni scritte quando diameter si leggeva in metri: quando tutte le
    # regioni sono state riscaricate non serve più (va tolto insieme al campo "diam")
    if all(regions_index.get(r["slug"], {}).get("diam") == "mm" for r in REGIONS):
        warn("Tutte le regioni hanno \"diam\": \"mm\" in data/index.json: si può togliere fixDiameter da index.html "
             "(e il campo \"diam\" da fetch_trees.py)")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
