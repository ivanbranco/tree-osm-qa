#!/usr/bin/env python3
"""Scarica gli alberi OSM (natural=tree e natural=tree_stump) delle regioni di scripts/regions.json via Overpass,
calcola i check QA e scrive data/regions/<regione>.geojson.gz e data/index.json.

Uso: python scripts/fetch_trees.py [all | gruppo | regione ...]   (es. "liguria veneto", "italia", "liguria,bulgaria")
Le regioni (nome, codice ISO, bbox) sono in scripts/regions.json.
"""
import calendar, collections, concurrent.futures, datetime, functools, gzip, hashlib, json, math, os, re, sys, time, unicodedata
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


def area_of(region):
    """Area OSM della regione per Overpass, nel set .a."""
    if region.get("rel"):
        return f"area({3600000000 + region['rel']})->.a;"
    return f'area["ISO3166-2"="{region["iso"]}"]["admin_level"="4"]->.a;'


def query(region, bbox):
    s, w, n, e = bbox
    area = area_of(region)
    return f"""[out:json][timeout:360];
{area}
(
  node["natural"="tree"](area.a)({s},{w},{n},{e});
  node["natural"="tree_stump"](area.a)({s},{w},{n},{e});
  node["xmas:feature"="tree"](area.a)({s},{w},{n},{e});
);
out meta qt;
way["natural"="tree_row"](area.a)({s},{w},{n},{e});
out geom qt;"""   # meta: serve solo la data dell'ultima modifica (to_feature, "ts"); utente e changeset non si salvano
    # filari (issue #56, tree_rows): con geom ogni way ha i tag, gli id dei nodi ("nodes") e le coordinate ("geometry")


def run_overpass(q, label, can_split, http_timeout=420):
    """Prova gli endpoint; restituisce il JSON solo se completo. Se la query è troppo pesante
    (timeout, errore di memoria, 504) e si può dividere, restituisce subito None. http_timeout: secondi di attesa
    della risposta (le query mondiali di fetch_world.py ne chiedono di più)."""
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
                r = requests.post(real, data={"data": q}, headers=HEADERS, timeout=http_timeout)
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
    merged, bases, newest = {}, [], []
    for tile in [(s, w, mlat, mlon), (s, mlon, mlat, e), (mlat, w, n, mlon), (mlat, mlon, n, e)]:
        part = fetch_bbox(region, tile, depth + 1, min_depth)
        if part is None:
            return None  # meglio la cache che dati incompleti
        for el in part["elements"]:
            merged[(el.get("type"), el["id"])] = el   # nodi e way (filari) hanno numerazioni separate
        bases.append(osm_base(part))
        newest.append(part["osm3s"].get("newest") or bases[-1])
        time.sleep(5)
    # la data dei dati di una regione divisa è quella del tile più vecchio (i tile possono venire da server diversi);
    # se un tile non la dice, non si conosce. "newest": la data del tile più nuovo, per l'avviso di update_region
    known = all(bases)
    return {"elements": list(merged.values()),
            "osm3s": {"timestamp_osm_base": min(bases) if known else "", "newest": max(newest) if known else ""}}


# Data dei dati OSM di una risposta di Overpass: "osm3s.timestamp_osm_base" (AAAA-MM-GGTHH:MM:SSZ, si confronta come
# testo). I server pubblici non sono aggiornati tutti allo stesso momento: update_region non sostituisce i dati di una
# regione con dati più vecchi (campo "osm_base" di data/index.json), e avvisa se i tile di una regione divisa hanno
# date che distano più di BASE_SPREAD_MAX
OSM_BASE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
BASE_SPREAD_MAX = 6 * 3600   # secondi


def osm_base(data):
    """Data dei dati OSM della risposta (vedi OSM_BASE); "" se manca o non è nella forma attesa."""
    v = str((data.get("osm3s") or {}).get("timestamp_osm_base") or "")
    return v if OSM_BASE.match(v) else ""


def base_seconds(v):
    return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%SZ"))


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
# Rapporto altezza/circonferenza ("slenderness"): segnalati solo i valori esagerati. Nei dati la mediana è ~12
# (11,8 su 368.958 alberi con le due misure mappate, novembre 2026; "_default" in species_limits.json) e gli alberi
# reali più tozzi (olivi monumentali) stanno sopra 0,45.
SLENDER_MAX = 100.0        # oltre: albero troppo slanciato (es. height=24, circumference=0.01)
STOCKY_MIN = 1 / 3         # sotto: albero troppo tozzo (circonferenza più di 3 volte l'altezza)
# Slancio rispetto alla specie (check too_slender_species, solo per le specie con height_circ_ratio): provato su
# 59.169 alberi, con queste soglie ne segnalava 81, quasi tutti con tronchi di pochi cm su alberi alti
SLENDER_SP_FACTOR = 4      # oltre 4 volte il rapporto tipico della specie
SLENDER_SP_MIN = 40        # ma mai sotto 40 (specie con rapporto basso, come l'olivo: i giovani sono più slanciati)
SLENDER_SP_MIN_H = 10      # m: solo alberi alti almeno 10 m
SLENDER_CHECKS = {"too_slender", "too_slender_species", "too_stocky"}   # con questi niente consiglio hint_ms
# Albero monumentale molto piccolo (check small_monument, issue #34): soglie basse di proposito, misure di una piccola
# pianta o di una canna di bambù, per non segnalare alberi monumentali veri; le eccezioni vanno in whitelist.json.
# Provato a novembre 2026 su 58.855 alberi con denotation=natural_monument: 14 sotto 1,5 m, 28 sotto 0,2 m
SMALL_MON_HEIGHT = 1.5     # m: altezza sotto questo valore
SMALL_MON_CIRC = 0.2       # m: circonferenza sotto questo valore (anche da diameter)
# Limiti massimi per specie (altezza e circonferenza, in m), modificabili in species_limits.json. Sono i record
# della pagina wiki https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/Tree_species_records, presi da
# monumentaltrees.com; "height_source" / "circumference_source" indicano una fonte diversa (es. "MASAF")
_RAW_LIMITS = load_json(HERE / "species_limits.json")
# rapporti medi (mediane) usati quando la specie non ne ha uno proprio
DEFAULT_RATIOS = _RAW_LIMITS.pop("_default", {})
SPECIES_LIMITS = {" ".join(k.split()).lower(): v for k, v in _RAW_LIMITS.items()}
# Rapporto altezza/circonferenza per fasce d'altezza (issue #39), solo per stimare la circonferenza da disegnare quando
# la specie non ha un rapporto proprio (o quando la sua voce dice di usarle prima, vedi height_ratios):
# "height_circ_ratio_bands" in "_default", righe [altezza minima in m, rapporto],
# estremo inferiore incluso (6 m sta nella fascia 6–8), l'ultima fascia senza limite superiore. Sono valori fissi: le
# mediane di 351.785 alberi di data/regions con altezza e circonferenza mappate e senza denotation=natural_monument
# (novembre 2026), non ricalcolate agli aggiornamenti delle regioni. I monumentali sono esclusi perché tra gli alberi
# che ricevono la stima (altezza senza circonferenza) sono lo 0,3%, mentre tra quelli misurati sopra i 20 m sono il
# 36% e abbassano la mediana del 14%. Fasce: 0–6 (69.933 alberi, 15,4), 6–8 (59.248, 13,2), 8–15 (150.834, 11,1),
# 15–20 (46.372, 11,3), 20–30 (22.792, 12,0), 30+ (2.606, 11,4). Minimo per fidarsi di una fascia: 1.000 alberi
# (una fascia con meno va unita a una vicina). I confini a 6 e 8 m (invece di 5 e 10) seguono i dati: il rapporto
# è 15–17 fino a 6 m (anche a 5 m esatti) e scende a 11 da 8 m. Le soglie dei check non usano queste fasce.
HEIGHT_BANDS = sorted((float(lo), float(r)) for lo, r in DEFAULT_RATIOS.get("height_circ_ratio_bands", []))


def band_ratio(h, bands=None):
    """Rapporto altezza/circonferenza della fascia d'altezza di h (m), nelle fasce generali o in quelle date (righe
    [altezza minima, rapporto] ordinate); None senza altezza o sotto la prima fascia."""
    if not h or h <= 0:
        return None
    return next((r for lo, r in reversed(HEIGHT_BANDS if bands is None else bands) if h >= lo), None)


# Fasce d'altezza per specie (issue #42), sempre solo per disegnare il tronco: "height_circ_ratio_bands" nella voce
# della specie in species_limits.json. Una lista di righe [altezza minima, rapporto] = fasce proprie della specie, che
# vengono prima del suo rapporto unico; "default" = per questa specie le fasce generali vengono prima del rapporto
# unico, che sul campione sbaglia di più. Senza la chiave vale l'ordine di sempre: rapporto unico, poi fasce generali.
# Stesso campione e stessa misura dell'errore delle fasce generali (352.042 alberi, novembre 2026); valori fissi, non
# ricalcolati agli aggiornamenti. Una specie ha fasce proprie solo con almeno 1.000 alberi e 3 regioni da 100 alberi,
# fasce da almeno 300 alberi sui confini generali, e solo se, calcolando le fasce senza una regione e misurandole su
# quella (a turno per tutte), l'errore mediano è più basso sia delle fasce generali sia del rapporto unico, nella
# maggioranza delle regioni e anche fuori dalla regione con più alberi. Criteri e numeri in docs/dati.md.
# height_circ_ratio resta com'è: lo usano il check too_slender_species e la finestra «Tronco a confronto».
def height_ratios(lim, h):
    """Rapporti altezza/circonferenza da provare in ordine per stimare la circonferenza di un albero alto h (m) con
    la voce lim di species_limits.json ({} se la specie non ne ha una)."""
    own, single, general = lim.get("height_circ_ratio_bands"), lim.get("height_circ_ratio"), band_ratio(h)
    if isinstance(own, list):
        order = (band_ratio(h, sorted((float(lo), float(r)) for lo, r in own)), single, general)
    elif own == "default":
        order = (general, single)
    else:
        order = (single, general)
    return [r for r in (*order, DEFAULT_RATIOS.get("height_circ_ratio")) if r]


# apici e virgolette (dritti, tipografici, accento grave/acuto) non contano nei nomi: Ulmus 'New Horizon' è
# uguale a Ulmus ‘New Horizon’ e a Ulmus `New Horizon`
QUOTES = str.maketrans("", "", "'‘’`´\"“”")
HYBRID_SIGNS = ("×", "x", "X")   # segno di ibrido scritto come parola a sé: "x Chitalpa tashkentensis"


def name_words(sp):
    """Parole di species senza il segno di ibrido: "×" ovunque, "x" o "X" solo all'inizio ("x Chitalpa tashkentensis",
    "x carnea"). In mezzo al nome "x" e "X" restano una parola: "Platanus X hispanica" è segnalato per la maiuscola
    (species_inner_case)."""
    words = [w for w in sp.split() if w != "×"]
    return words[1:] if words and words[0] in HYBRID_SIGNS else words


# lru_cache sulle funzioni che dipendono solo dal valore di species: in una regione gli stessi nomi si ripetono
# centinaia di migliaia di volte
@functools.lru_cache(maxsize=None)
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
TYPO_WHITELIST = {" ".join(w for w in norm_species(k).split() if w != "×") for k in _WL.get("species_typo", [])}
# sinonimi: nome alternativo -> nome della lista wiki (es. "Crataegus × lavallei" -> "Crataegus × lavalleei"); il
# nome alternativo vale come quello della lista in tutti i controlli (errori di battitura, species:wikidata, nome su
# Wikidata, stime di leaf_type e leaf_cycle)
SYNONYMS = {norm_species(k): v for k, v in _WL.get("synonyms", {}).items()}
for _alt, _name in SYNONYMS.items():
    if norm_species(_name) in SPECIES_REF:
        SPECIES_REF.setdefault(_alt, SPECIES_REF[norm_species(_name)])
# un nome e i suoi sinonimi sono una sola specie: tutti accettano i species:wikidata di tutti (quelli della whitelist
# di ogni nome e quello della lista wiki), così il sito non propone di sostituire il QID di un nome con quello dell'altro
_GROUPS = collections.defaultdict(set)
for _alt, _name in SYNONYMS.items():
    _GROUPS[norm_species(_name)] |= {norm_species(_name), _alt}
for _members in _GROUPS.values():
    _qids = {q for m in _members for q in WD_WHITELIST.get(m, ())}
    _qids |= {SPECIES_REF[m]["wikidata"] for m in _members if m in SPECIES_REF and SPECIES_REF[m].get("wikidata")}
    for m in _members:
        WD_WHITELIST[m] = set(_qids)
# nome della specie per statistiche e filtro del sito ("sp_key" in to_feature): il nome della lista wiki, per i
# sinonimi e per le scritture diverse dello stesso nome ("x" al posto di "×")
GROUP_NAME = {m: _name for _alt, _name in SYNONYMS.items() for m in (norm_species(_name), _alt)}


# Lista dei generi della wiki OSM (scripts/update_reference_lists.py): leaf_cycle (uno o entrambi) e leaf_type
_GENERA = load_json(HERE / "genus_list.json", "genera")
GENUS_REF = {k.lstrip("× ").casefold(): (k, v) for k, v in _GENERA.items()}
# nomi comuni dei generi (colonne genus:en, genus:fr della lista e genus_names di whitelist.json per le altre lingue,
# es. pl) -> genere, per il consiglio di species_lang_only
# (issue #35): {("fr", "tilleul"): "Tilia"}. Valgono solo le coppie nome-genere che sono anche in genus_names di
# whitelist.json (nomi senza dubbi); per sicurezza un nome di più generi nella lista wiki non dà comunque consigli
_GN = {}
for _g, _v in _GENERA.items():
    for _lang, _names in _v.get("names", {}).items():
        for _n in _names:
            _GN.setdefault((_lang, _n.casefold()), set()).add(_g)
_GN_OK = {(lang, n.casefold()): g for lang, d in _WL.get("genus_names", {}).items() for n, g in d.items()}
GENUS_NAMES = {k: g for k, v in _GN.items() if len(v) == 1 and _GN_OK.get(k) == (g := next(iter(v)))}
# i nomi di whitelist.json che la lista wiki non ha per nessun genere (species:pl=Dąb -> Quercus, nelle lingue senza
# una colonna nella lista; species:en=Plane -> Platanus, che nella lista non ha il nome inglese) valgono da soli
GENUS_NAMES.update({k: g for k, g in _GN_OK.items() if k not in _GN})
# nomi dei generi che in alcune regioni o nazioni indicano anche altro (genus_names_except di whitelist.json): lì non
# danno consigli. {("en", "plane"): {"GB-SCT"}}: in Scozia «plane» è anche Acer pseudoplatanus. Codici ISO di
# scripts/regions.json, di una regione (GB-SCT) o di una nazione (GB)
GENUS_NAMES_EXCEPT = {(lang, n.casefold()): set(isos) for lang, d in _WL.get("genus_names_except", {}).items()
                      for n, isos in d.items()}


def genus_name_here(key):
    """Vero se la coppia (lingua, nome) di GENUS_NAMES vale nella regione che si sta aggiornando (REGION_ISO)."""
    ex = GENUS_NAMES_EXCEPT.get(key)
    return not ex or not ({REGION_ISO.upper(), REGION_ISO[:2].upper()} & ex)
# nomi comuni delle specie (species_names di whitelist.json, scritti a mano): {("pl", "klon zwyczajny"): "Acer
# platanoides"}; per questi si propone species=<la specie>
SPECIES_NAMES = {(lang, n.casefold()): s for lang, d in _WL.get("species_names", {}).items() for n, s in d.items()}
# gli stessi nomi scritti come in whitelist.json ({("en", "northern red oak"): "Northern Red Oak"}): la correzione con
# JOSM sistema le maiuscole di species:xx
SPECIES_NAME_FORM = {(lang, n.casefold()): n for lang, d in _WL.get("species_names", {}).items() for n in d}
# generi con una sola specie (whitelist.json): per i loro nomi si propone species=<la specie> (Cognassier -> Cydonia oblonga)
SINGLE_SPECIES = _WL.get("single_species_genera", {})
# piante che non sono alberi (whitelist.json): {"Agave americana": "Q161115"}, species -> QID (check not_a_tree)
NOT_TREES = _WL.get("not_trees", {})
# generi per cui leaf_type_mismatch accetta più leaf_type (whitelist.json, "leaf_types"): genere minuscolo -> valori
LEAF_TYPE_GENERA = {k.casefold(): set(v) for k, v in _WL.get("leaf_types", {}).items()}
# alberi monumentali piccoli davvero (id dei nodi), da non segnalare con small_monument
SMALL_MON_WHITELIST = {int(k) for k in _WL.get("small_monument", {})}

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
    space: spazi prima o dopo il valore (urban; natural_monument): i valori si separano con ";" senza spazi, e lo
    stile della mappa (hasDen in index.html) cerca ";valore;" senza toglierli;
    glued:<comune>: un valore comune con altro testo attaccato senza _ (es. urbanheidialhoranta);
    rare: usato al massimo DEN_RARE_MAX volte. Gli ultimi due richiedono denotation_values.json."""
    out = []
    if "denotation" not in t:
        return out
    for raw in str(t["denotation"]).split(";"):
        v = raw.strip()
        if not v:
            continue
        n = DEN_COUNTS.get(v, 0)
        if not DEN_FORMAT.match(v):
            out.append((v, "format", n))
        elif raw != v:
            out.append((v, "space", n))
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
#   (P625), "image": file di Commons (P18), "winter": file della veduta invernale (P5252, se c'è), "taxa": [QID di P10241 "individuo del taxon"], "heritage": [[etichetta it,
#   etichetta en] di P1435 "tutela"], "wp": {"it": url, "en": url} (voci di Wikipedia), "mt": id di monumentaltrees.com
#   (P9704), "mon": true se è istanza diretta (P31) di "pianta monumentale" (Q811534)}}
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
COMMONS_MAX_DIST = 500   # m: oltre, l'albero è "lontano" dalle coordinate del file di Commons collegato (la
                         # posizione di scatto può distare qualche decina di metri dall'albero)
POINT = re.compile(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)")
WP_SITES = {"it": "https://it.wikipedia.org/", "en": "https://en.wikipedia.org/"}
# ranghi (P105): species:wikidata non deve puntare a un genere o più su, genus:wikidata non a una specie o più giù
RANK_GENUS_OR_ABOVE = {"Q34740", "Q3238261", "Q3181348", "Q5998839", "Q3025161", "Q227936", "Q164280", "Q35409", "Q36602"}
RANK_SPECIES_OR_BELOW = {"Q7432", "Q68947", "Q767728", "Q279749"}


def qids(t, key):
    """QID validi (Q…) di un tag, anche separati da ';'."""
    v = t.get(key)
    if not v:   # quasi tutti gli alberi non hanno il tag: si esce subito
        return []
    return [x.strip() for x in str(v).split(";") if WIKIDATA.match(x.strip())]


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


def ask_wikidata(todo, query, store, label, done=None):
    """Interroga Wikidata a blocchi di WD_BATCH QID: query(batch) dà lo SPARQL, store(batch, righe) salva.
    Se Wikidata non risponde si ferma: i QID rimasti si verificano al prossimo aggiornamento.
    done: insieme a cui si aggiungono i QID dei blocchi salvati."""
    for i in range(0, len(todo), WD_BATCH):
        batch = todo[i:i + WD_BATCH]
        try:
            store(batch, wdqs(query(" ".join("wd:" + q for q in batch))))
        except Exception as e:
            warn(f"Wikidata non risponde ({str(e)[:150]}): {len(todo) - i} QID restano da verificare ({label})")
            return
        if done is not None:
            done.update(batch)
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
        if "wimg" in b and not e.get("winter"):   # veduta invernale (P5252)
            e["winter"] = unquote(b["wimg"]["value"].rsplit("/", 1)[-1])
        if "taxon" in b:
            add(e["taxa"], qid_of(b, "taxon"))
        if "her" in b:
            add(e["heritage"], [b.get("herIt", b.get("herEn", {})).get("value", qid_of(b, "her")),
                                b.get("herEn", b.get("herIt", {})).get("value", qid_of(b, "her"))])
        for lang in WP_SITES:
            if "wp" + lang in b:
                e["wp"][lang] = b["wp" + lang]["value"]
        if "mt" in b:
            e["mt"] = b["mt"]["value"]
        if "mon" in b:
            e["mon"] = True
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
        if "bas" in b:
            add(e.setdefault("basionyms", []), b["bas"]["value"])
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


# data/wikipedia_qids.json: voci di species:wikipedia e wikipedia -> QID dell'elemento Wikidata collegato (o null se la
# voce non esiste o non ha un elemento), per i consigli su species:wikidata e species e per il check
# wikipedia_is_taxon: {"en:Quercus robur": "Q165145", …}
WP_QIDS_FILE = DATA / "wikipedia_qids.json"
WP_QIDS = load_json(WP_QIDS_FILE)
WP_VALUE = re.compile(r"^\s*([a-z][a-z-]{1,11})\s*:\s*(\S.*?)\s*$")
# Specie (QID) e voci di Wikipedia già lette in questa esecuzione: le regioni aggiornate insieme hanno quasi le
# stesse specie, e una lettura per esecuzione basta a far arrivare le correzioni fatte su Wikidata. Gli elementi
# di wikidata=* (i singoli alberi) sono diversi da regione a regione e si chiedono sempre
WD_TAXA_ASKED = set()
WP_ASKED = set()


def wp_key(v):
    """'en:Quercus_robur' -> 'en:Quercus robur' (chiave di WP_QIDS); None se il valore non è lingua:titolo o ha più voci."""
    m = WP_VALUE.match(str(v or ""))
    if not m or ";" in m.group(2) or "://" in str(v):
        return None
    title = m.group(2).replace("_", " ")
    return f"{m.group(1)}:{title[:1].upper() + title[1:]}"


def resolve_wikipedia(tags):
    """QID degli elementi Wikidata delle voci di species:wikipedia e wikipedia, dall'API della Wikipedia di ogni lingua
    (pageprops wikibase_item, a blocchi di 50 titoli; si seguono normalizzazioni e redirect). Si rileggono a ogni
    aggiornamento, come i QID, ma una volta sola per esecuzione (WP_ASKED); se Wikipedia non risponde resta la
    cache. Restituisce i QID trovati, da aggiungere alle specie da chiedere a Wikidata."""
    keys = sorted({k for t in tags for key in ("species:wikipedia", "wikipedia") if (k := wp_key(t.get(key)))})
    todo = [k for k in keys if k not in WP_ASKED]
    by_lang = {}
    for k in todo:
        lang, title = k.split(":", 1)
        by_lang.setdefault(lang, []).append(title)
    for lang, titles in by_lang.items():
        for i in range(0, len(titles), 50):
            batch = titles[i:i + 50]
            try:
                r = requests.get(f"https://{lang}.wikipedia.org/w/api.php", timeout=60, headers=HEADERS, params={
                    "action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "redirects": 1,
                    "titles": "|".join(batch), "format": "json", "formatversion": 2})
                r.raise_for_status()
                res = r.json().get("query", {})
            except Exception as e:
                warn(f"Wikipedia ({lang}) non risponde ({str(e)[:150]}): {len(titles) - i} voci di species:wikipedia "
                     "restano da risolvere")
                break
            moved = {x["from"]: x["to"] for x in res.get("normalized", []) + res.get("redirects", [])}
            qid = {p["title"]: p.get("pageprops", {}).get("wikibase_item") for p in res.get("pages", [])}
            for title in batch:
                final = title
                for _ in range(3):   # normalizzazione, poi redirect
                    final = moved.get(final, final)
                WP_QIDS[f"{lang}:{title}"] = qid.get(final)
                WP_ASKED.add(f"{lang}:{title}")
            time.sleep(1)
    if todo:
        WP_QIDS_FILE.write_text(json.dumps(WP_QIDS, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return {q for k in keys if (q := WP_QIDS.get(k))}


def wikipedia_qid(t):
    """QID dell'elemento Wikidata della voce di species:wikipedia (dalla cache), o None."""
    return WP_QIDS.get(wp_key(t.get("species:wikipedia")) or "")


def wikipedia_taxon(t):
    """QID dell'elemento Wikidata della voce di wikipedia=* se è un taxon (wikipedia dovrebbe indicare la voce del
    singolo albero, come wikidata), altrimenti None."""
    if "wikipedia" not in t:
        return None
    q = WP_QIDS.get(wp_key(t["wikipedia"]) or "")
    return q if q and taxon_status(q) is True else None


def update_wikidata_cache(elements):
    """Chiede a Wikidata i dati dei QID usati dalla regione: per wikidata=* (alberi) se è un taxon,
    coordinate, immagine, specie (P10241), tutela (P1435), voci di Wikipedia, id di monumentaltrees.com (P9704) e se è
    una pianta monumentale (P31 = Q811534); per species:wikidata, genus:wikidata e
    le specie di P10241 nome scientifico, rango ed etichette. Aggiorna e salva le due cache.
    I QID si rileggono a ogni aggiornamento, anche se sono già in cache: così le correzioni fatte su Wikidata
    arrivano nei check (le cache sono piccole, poche richieste). Le specie si chiedono una volta sola per esecuzione
    (WD_TAXA_ASKED): le regioni aggiornate insieme hanno quasi le stesse. Se Wikidata non risponde resta la cache."""
    tags = [el.get("tags", {}) for el in elements]
    wd = {q for t in tags for q in qids(t, "wikidata")}
    todo_items = sorted(wd)
    ask_wikidata(todo_items, lambda v: "SELECT ?item ?name ?coord ?img ?wimg ?taxon ?her ?herIt ?herEn ?wpit ?wpen ?mt ?mon WHERE { "
                 "VALUES ?item { " + v + " } "
                 # monumentali: id di monumentaltrees.com (P9704) e "istanza di" (solo diretta) pianta monumentale
                 "OPTIONAL { ?item wdt:P9704 ?mt } OPTIONAL { ?item wdt:P31 ?mon FILTER(?mon = wd:Q811534) } "
                 f"OPTIONAL {{ ?item {wd_prop('P225')} ?name }} OPTIONAL {{ ?item {wd_prop('P625')} ?coord }} "
                 f"OPTIONAL {{ ?item {wd_prop('P18')} ?img }} OPTIONAL {{ ?item {wd_prop('P5252')} ?wimg }} "
                 "OPTIONAL { ?item wdt:P10241 ?taxon } "
                 "OPTIONAL { ?item wdt:P1435 ?her . OPTIONAL { ?her rdfs:label ?herIt FILTER(LANG(?herIt) = 'it') } "
                 "OPTIONAL { ?her rdfs:label ?herEn FILTER(LANG(?herEn) = 'en') } } "
                 "OPTIONAL { ?wpit schema:about ?item ; schema:isPartOf <" + WP_SITES["it"] + "> } "
                 "OPTIONAL { ?wpen schema:about ?item ; schema:isPartOf <" + WP_SITES["en"] + "> } }",
                 store_items, "alberi")
    # specie: quelle dei tag, quelle indicate da P10241 degli alberi e quelle della lista wiki per i valori di species
    # (per il nome comune)
    listed = {q for t in tags if (q := list_qid(t.get("species")))}
    # elementi delle voci di species:wikipedia (per i consigli) e di wikipedia (check wikipedia_is_taxon)
    from_wp = resolve_wikipedia(tags)
    todo_taxa = sorted(({q for t in tags for k in ("species:wikidata", "genus:wikidata") for q in qids(t, k)}
                        | {x for q in wd for x in WD_ITEMS.get(q, {}).get("taxa", [])} | listed | from_wp)
                       - WD_TAXA_ASKED)
    ask_wikidata(todo_taxa, lambda v: "SELECT ?item ?name ?rank ?it ?en ?bas WHERE { VALUES ?item { " + v + " } "
                 f"OPTIONAL {{ ?item {wd_prop('P225')} ?name }} OPTIONAL {{ ?item {wd_prop('P105')} ?rank }} "
                 f"OPTIONAL {{ ?item {wd_prop('P566')} ?b . ?b wdt:P225 ?bas }} "   # basionimo (nome scientifico)
                 "OPTIONAL { ?item rdfs:label ?it FILTER(LANG(?it) = 'it') } "
                 "OPTIONAL { ?item rdfs:label ?en FILTER(LANG(?en) = 'en') } }", store_taxa_info, "specie",
                 WD_TAXA_ASKED)
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


def wd_names(q):
    """Nomi accettati per un elemento Wikidata, come binomi: nome scientifico (P225) e basionimo (P566)."""
    return {binomial(n) for n in taxon_names(q) + WD_TAXA.get(q, {}).get("basionyms", [])}


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
                alts = {binomial(val)} | ({binomial(SYNONYMS[norm_species(val)])} if norm_species(val) in SYNONYMS else set())
                # accettato anche il QID che la lista wiki associa a species (la lista può usare un altro nome)
                ok = q in WD_WHITELIST.get(norm_species(val), ()) or bool(alts & wd_names(q)) or list_qid(val) == q
                # QID della specie che species voleva scrivere: basta l'errore di battitura (species_typo)
                if not ok and (typo := species_typo(val)):
                    ok = binomial(typo) in wd_names(q) or same_species(typo, QID_TO_SPECIES.get(q, ""))
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
        # anche i sinonimi di species (whitelist.json): Platanus acerifolia = Platanus × hispanica
        if sp and ";" not in sp and not own & {binomial(n) for n in names}:
            g = group_name(sp)
            own |= {binomial(m) for m, n in GROUP_NAME.items() if norm_species(n) == g}
        if not own & {binomial(n) for n in names} and not wd_tree_genus_only(names, own):
            out.append((q, names[0]))
    return out


def wd_tree_genus_only(names, own):
    """Il genere, se l'elemento Wikidata dell'albero indica solo il genere della specie taggata (nomi di P10241 tutti
    di una parola, quel genere): non è un errore su OSM, ma un consiglio per Wikidata (hint_wg)."""
    genera = {o.split()[0] for o in own if o}
    if names and all(len(binomial(n).split()) == 1 and binomial(n) in genera for n in names):
        return names[0]
    return None


def species_name(n):
    """Il nome, se può essere un valore di species: almeno due parole (un genere come «Pinus» non lo è)."""
    return n if len(n.replace("×", " ").split()) >= 2 else None


def hint_props(t, osm_id=None):
    """Dati per i consigli della scheda (ⓘ: informazioni, non errori; index.html li mostra e non li segnala sulla
    mappa). Solo quelli che la pagina non può ricavare da sola:
    hint_sp: nessun species né genus, ma l'elemento di wikidata=* indica la specie (P10241, non un genere o più su)
    -> nome scientifico;
    hint_wg: species, ma l'elemento di wikidata=* indica solo il suo genere (P10241) -> "QID|genere": su Wikidata si
    potrebbe precisare la specie, con una fonte compatibile con la sua licenza (i dati di OSM non lo sono);
    hint_sw: species (dalla lista wiki) senza species:wikidata -> QID della specie nella lista;
    hint_sn: species:wikidata (un taxon, non di rango genere o superiore) senza species -> nome scientifico
    dell'elemento (P225), solo se di almeno due parole (vale anche per hint_sp e hint_wn);
    hint_it: species:it con l'iniziale minuscola -> lo stesso valore con la maiuscola (su OSM il 98% dei valori la ha,
    ma non è una regola scritta: per questo è un consiglio e non un errore), ogni parte separata da ";";
    hint_wp: species:wikipedia senza species:wikidata (e senza il QID della lista, che dà già hint_sw) -> QID
    dell'elemento Wikidata della voce, se è una specie;
    hint_wn: species:wikipedia senza species (e senza hint_sn o hint_sp) -> nome scientifico (P225) di quell'elemento;
    hint_tx: taxon=* che nelle liste wiki è una specie o un genere (taxon_precise) -> "species|nome" o "genus|nome",
    o che non è nella lista ma ha la forma di una specie (genere noto ed epiteto) -> "species|valore|b": la correzione
    con JOSM lo sposta in species o genus (il consiglio su taxon=* c'è comunque, index.html);
    hint_txl: con hint_tx, i taxon:xx dell'albero ("taxon:en=Rowan" separati da "\n"): la correzione li sposta in
    species:xx o genus:xx. Se quel tag c'è già con un altro valore, né hint_tx né hint_txl;
    hint_ne: name:en con l'iniziale minuscola -> il valore (index.html propone la maiuscola);
    hint_imp_u: source=* di un import non documentato (scripts/imports.json) -> il valore;
    hint_imp_d: source=* di un import documentato -> "valore|pagina della wiki";
    hint_mc: ref:masaf senza masaf:criteria -> i criteri dell'elenco MASAF (masaf_trees.json) per quei codici;
    hint_ip: image=* è una pagina web di un sito di IMAGE_PAGE_SITES, non il file dell'immagine -> il sito;
    hint_mly: quella pagina è una foto di Mapillary e manca mapillary=* -> l'identificativo della foto (la correzione
    con JOSM lo sposta in mapillary);
    hint_ms: solo nelle regioni italiane, albero senza denotation=natural_monument/landmark né ref:masaf con la
    circonferenza (taggata, o da diameter) almeno pari alla soglia MASAF della specie -> "circonferenza|soglia" in m;
    non per i membri di un insieme omogeneo MASAF (osm_id in MASAF_GROUPS), che valgono già come monumentali;
    sp_base, sp_var, sp_cv, sp_abbr: species con una varietà ("var. epiteto"), una cultivar (tra virgolette, o senza
    dopo una specie della lista) o il genere abbreviato (species_infra, issue #68) -> species senza varietà e cultivar
    e con il genere per esteso, la varietà, la cultivar, 1 se il genere era abbreviato (le ultime tre solo se ci
    sono). La scheda mostra varietà e cultivar in righe a parte e consiglia taxon:variety e taxon:cultivar
    (index.html). sp_base_wd, sp_base_lt, sp_base_lc: species:wikidata, leaf_type e leaf_cycle che mancano, per
    sp_base (species_lang_extras): la correzione li aggiunge insieme. sp_gen al posto di sp_base: species è un genere
    della lista con la sola cultivar ("Magnolia 'Susan'") -> il genere; la correzione toglie species. sp_var_ok: 1 se
    la varietà è un taxon di rango varietà di wikidata_taxa.json (known_variety); senza, la scheda dà il consiglio con
    l'invito a controllare che non sia una cultivar, e nessuna correzione."""
    out = {}
    if infra := species_infra(str(t.get("species") or "").strip(), str(t.get("genus") or "").strip()):
        only_genus = len(name_words(infra[0])) == 1
        out["sp_gen" if only_genus else "sp_base"] = infra[0]
        out.update({k: v for k, v in (("sp_var", infra[1]), ("sp_cv", infra[2]), ("sp_abbr", int(infra[3]))) if v})
        if infra[1] and known_variety(infra[0], infra[1]):
            out["sp_var_ok"] = 1
        if not only_genus:
            out.update(species_lang_extras(t, infra[0], "sp_base_"))
    if ip := image_page(t):
        out["hint_ip"] = ip[0]
        if ip[1] and "mapillary" not in t:
            out["hint_mly"] = ip[1]
    if imp := import_source(t):
        if imp[0] == "d":
            out["hint_imp_d"] = f"{imp[1]}|{imp[2]}"
        else:
            out["hint_imp_u"] = imp[1]
    den = {x.strip() for x in str(t.get("denotation", "")).split(";")}
    sp_bin = binomial(t.get("species") or "") if ";" not in str(t.get("species", "")) else ""
    if (IN_ITALY and t.get("natural") == "tree" and not t.get("ref:masaf") and not den & {"natural_monument", "landmark"}
            and not masaf_member(osm_id) and (min_cm := MASAF_MIN_CIRC.get(sp_bin))):
        c, _ = circ_value(t)
        if c is not None and c * 100 >= min_cm:
            out["hint_ms"] = f"{round(c, 2):g}|{min_cm / 100:g}"
    if t.get("ref:masaf") and "masaf:criteria" not in t:
        cr = {c for raw in str(t["ref:masaf"]).split(";") for c in MASAF_CRITERIA_OF.get(masaf_code(raw), [])}
        if cr:
            out["hint_mc"] = ",".join(sorted(cr, key=MASAF_ORDER.index))
    sit = [x.strip() for x in str(t.get("species:it", "")).split(";") if x.strip()]
    if any(x[:1].islower() for x in sit):
        out["hint_it"] = ";".join(x[:1].upper() + x[1:] for x in sit)
    if (ne := str(t.get("name:en") or "")).lstrip(" ×")[:1].islower():
        out["hint_ne"] = ne
    if tp := taxon_precise(t):
        # taxon:xx (nome in una lingua) resterebbe senza taxon: passa in species:xx / genus:xx. Se quel tag c'è già con
        # un altro valore la proposta non sarebbe certa: nessun consiglio
        lang = [(k, str(v).strip()) for k, v in t.items() if k.startswith("taxon:") and lang_key(TAXON_LANG, k)]
        if not any(str(t.get(f"{tp[0]}:{k[6:]}") or "").strip() not in ("", v) for k, v in lang):
            out["hint_tx"] = "|".join(tp)
            if lang:
                out["hint_txl"] = "\n".join(f"{k}={v}" for k, v in sorted(lang))
    sp = (t.get("species") or "").strip()
    if not sp and not t.get("genus"):
        for q in qids(t, "wikidata"):
            names = [n for tx in WD_ITEMS.get(q, {}).get("taxa", []) if not rank_problem("species:wikidata", tx)
                     for n in taxon_names(tx) if species_name(n)]
            if taxon_status(q) is not True and names:
                out["hint_sp"] = names[0]
                break
    # l'elemento Wikidata dell'albero indica solo il genere della specie taggata: lo si potrebbe precisare su Wikidata
    if sp and ";" not in sp and species_name(sp):
        for q in qids(t, "wikidata"):
            names = [n for tx in WD_ITEMS.get(q, {}).get("taxa", []) for n in taxon_names(tx)]
            if taxon_status(q) is not True and (g := wd_tree_genus_only(names, {binomial(sp)})):
                out["hint_wg"] = f"{q}|{g}"
                break
    # elemento Wikidata della voce di species:wikipedia, se è una specie (un taxon non di rango genere o superiore)
    wq = wikipedia_qid(t) if t.get("species:wikipedia") else None
    if wq and (taxon_status(wq) is not True or rank_problem("species:wikidata", wq)):
        wq = None
    if sp and "species:wikidata" not in t and (q := list_qid(sp)):
        out["hint_sw"] = q
    elif wq and "species:wikidata" not in t:
        out["hint_wp"] = wq
    if not sp:
        for q in qids(t, "species:wikidata"):
            # un elemento di rango genere o superiore non dà un valore per species (lo segnala già wikidata_rank)
            if (taxon_status(q) is True and not rank_problem("species:wikidata", q)
                    and (names := [n for n in taxon_names(q) if species_name(n)])):
                out["hint_sn"] = names[0]
                break
        if wq and "hint_sn" not in out and "hint_sp" not in out and (names := [n for n in taxon_names(wq) if species_name(n)]):
            out["hint_wn"] = names[0]
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
    if "image" not in t and "website" not in t and "url" not in t:
        return []
    return [(k, u) for k in URL_KEYS for u in (x.strip() for x in str(t.get(k, "")).split(";")) if URL.match(u)]


def commons_files(t):
    """File di Commons in wikimedia_commons (le categorie no)."""
    if "wikimedia_commons" not in t:
        return []
    return [x.strip() for x in str(t.get("wikimedia_commons", "")).split(";") if x.strip().startswith("File:")]


# Pagina di un file su Commons in image=* (https://commons.wikimedia.org/wiki/File:…, anche con il prefisso
# File: in un'altra lingua)
COMMONS_FILE_URL = re.compile(r"^https?://commons\.(?:m\.)?wikimedia\.org/wiki/[^/:?#]+:([^?#]+)", re.I)
# Indirizzo del file su upload.wikimedia.org, anche di una miniatura (…/commons/thumb/a/ab/<file>/800px-<file>): come
# COMMONS_UPLOAD in index.html (consiglio su wikimedia_commons)
COMMONS_UPLOAD_URL = re.compile(r"^https?://upload\.wikimedia\.org/wikipedia/commons/(?:thumb/)?[0-9a-f]/[0-9a-f]{2}/([^/?#]+)",
                                re.I)
# image=File:… : il nome di un solo file di Commons (come COMMONS_NAME e imageInfo in index.html)
COMMONS_IMAGE_NAME = re.compile(r"^\s*File:([^;]+?)\s*$")


def commons_photo_files(t):
    """File di Commons collegati all'albero: wikimedia_commons=File:…, image=File:… e image=* che punta alla pagina
    di un file su Commons o al file stesso su upload.wikimedia.org; titoli "File:…" con gli spazi al posto dei
    trattini bassi, senza doppioni."""
    if "wikimedia_commons" not in t and "image" not in t:
        return []
    out =[f.replace("_", " ") for f in commons_files(t)]
    if m := COMMONS_IMAGE_NAME.match(str(t.get("image", ""))):
        out.append("File:" + m.group(1).replace("_", " "))
    for u in str(t.get("image", "")).split(";"):
        if m := COMMONS_FILE_URL.match(u.strip()) or COMMONS_UPLOAD_URL.match(u.strip()):
            out.append("File:" + unquote(m.group(1)).replace("_", " ").strip())
    return list(dict.fromkeys(out))


# Siti che in image=* danno una pagina web e non il file dell'immagine (consiglio hint_ip). Si confronta il nome del
# sito senza «www.» né «m.»; quelli con il punto davanti valgono anche per i sottodomini (it.wikiloc.com). Non
# i.imgur.com, live.staticflickr.com, images.mapillary.com, drive.google.com/uc: danno il file. Google Foto
# (photos.app.goo.gl, photos.google.com) è accettato: è il valore più usato negli alberi, con la foto condivisa
IMAGE_PAGE_SITES = ("drive.google.com", "goo.gl", "maps.app.goo.gl",
                    "mapillary.com", "flickr.com", "geograph.org.uk", "imgur.com", ".wikiloc.com", "twitter.com",
                    "x.com", "facebook.com", "instagram.com", "1drv.ms", "onedrive.live.com", ".wikipedia.org")
# Pagina di una foto di Mapillary: …/map/im/<chiave> o …/app/?…pKey=<id> (consiglio hint_mly)
MAPILLARY_PAGE = re.compile(r"^https?://(?:www\.)?mapillary\.com/(?:map/im/([A-Za-z0-9_-]+)|app/?\?(?:[^#]*&)?pKey=([A-Za-z0-9_-]+))",
                            re.I)


def image_page(t):
    """(sito, identificativo della foto di Mapillary o "") se image=* (un solo valore) è una pagina di un sito di
    IMAGE_PAGE_SITES, altrimenti None."""
    v = str(t.get("image") or "").strip()
    if not v or ";" in v or not URL.match(v):
        return None
    u = urlparse(v)
    host = re.sub(r"^(?:www\.|m\.)", "", (u.hostname or "").lower())
    if host == "drive.google.com" and u.path.startswith("/uc"):
        return None
    for s in IMAGE_PAGE_SITES:
        if host == s.lstrip(".") or (s.startswith(".") and host.endswith(s)):
            m = MAPILLARY_PAGE.match(v)
            return host, (m.group(1) or m.group(2)) if m else ""
    return None


# Voce di Wikipedia in website=* o url=*: https://<lingua>.wikipedia.org/wiki/<titolo> (anche il sito per cellulari)
WIKIPEDIA_URL = re.compile(r"^https?://([a-z][a-z-]{1,11})\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)", re.I)


def website_wikipedia(t):
    """(chiave, "lingua:Titolo") se website=* o url=* (un solo valore) è una voce di Wikipedia (check
    website_wikipedia: per le voci c'è wikipedia=*), altrimenti None."""
    for k in ("website", "url"):
        v = str(t.get(k) or "").strip()
        if ";" not in v and (m := WIKIPEDIA_URL.match(v)):
            title = unquote(m.group(2)).replace("_", " ").strip()
            if title and "|" not in title:
                return k, f"{m.group(1).lower()}:{title}"
    return None


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
    # coordinate dei file di Commons collegati (check commons_far): "geo:File:…" → {"c": [lat, lon, tipo] o
    # [], "d": data}; tipo "object" (posizione del soggetto, se c'è) o "camera" (posizione di scatto)
    # anche le foto (P18) degli elementi di wikidata=* senza coordinate proprie (check wikidata_photo_far)
    geo = sorted({f for t in tags for f in commons_photo_files(t) + [f for _, f in wikidata_photos(t)] if stale("geo:" + f)})
    for i in range(0, len(geo), 50):
        batch = geo[i:i + 50]
        try:
            r = requests.get(COMMONS_API, headers=HEADERS, timeout=60, params={
                "action": "query", "titles": "|".join(batch), "prop": "coordinates", "coprimary": "all",
                "colimit": "max", "format": "json", "formatversion": 2})
            r.raise_for_status()
            q = r.json()["query"]
            back = {n["to"]: n["from"] for n in q.get("normalized", [])}
            found = {}
            for pg in q.get("pages", []):
                cs = pg.get("coordinates", [])
                c = next((x for x in cs if not x.get("primary")), None) or next(iter(cs), None)
                if c:
                    found[back.get(pg["title"], pg["title"])] = [c["lat"], c["lon"], "camera" if c.get("primary") else "object"]
            for f in batch:
                URL_STATUS["geo:" + f] = {"c": found.get(f, []), "d": today.isoformat()}
        except Exception as e:
            warn(f"Commons non risponde ({str(e)[:150]}): coordinate di {len(geo) - i} file restano da leggere")
            break
        time.sleep(URL_PAUSE)
    if geo:
        print(f" coordinate lette per {len(geo)} file di Commons")
    if urls or files or geo:
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


# Foto di Google Foto in image=* (photos.app.goo.gl, photos.google.com, un solo valore): il sito non può leggere la
# pagina condivisa (Google non permette le richieste da altri siti), quindi l'indirizzo della foto si legge qui. È
# og:image della pagina, su lh3.googleusercontent.com, senza la parte con la dimensione ("=w600-h315-p-k", che la
# ritaglia): index.html aggiunge la sua. Resta in data/gphotos.json ({"<link>": {"i": indirizzo o "", "d":
# "AAAA-MM-GG"}}) e si rilegge dopo URL_RECHECK_DAYS giorni (una condivisione può essere tolta). "" = la pagina non
# indica una foto (condivisione tolta o privata). La pagina pesa circa 1,2 MB e og:image è verso la fine
GPHOTOS_FILE = DATA / "gphotos.json"
GPHOTOS = load_json(GPHOTOS_FILE)
GPHOTOS_URL = re.compile(r"^https?://(?:photos\.app\.goo\.gl|photos\.google\.com)/[^\s;]+$", re.I)
GPHOTOS_OG = re.compile(r'<meta property="og:image" content="(https://lh3\.googleusercontent\.com/[^"=]+)')


def gphotos_link(t):
    """Il link di Google Foto in image=*, se è un solo valore; altrimenti None."""
    v = str(t.get("image") or "").strip()
    return v if GPHOTOS_URL.match(v) else None


def update_gphotos_cache(elements):
    """Legge l'indirizzo delle foto di Google Foto non ancora in cache (o lette da più di URL_RECHECK_DAYS giorni);
    aggiorna e salva data/gphotos.json."""
    today = datetime.date.today()
    stale = lambda u: u not in GPHOTOS or (today - datetime.date.fromisoformat(GPHOTOS[u]["d"])).days >= URL_RECHECK_DAYS
    links = sorted({u for el in elements if (u := gphotos_link(el.get("tags", {}))) and stale(u)})
    for u in links:
        try:
            r = requests.get(u, headers=HEADERS, timeout=30)
            if r.status_code not in (404, 410):
                r.raise_for_status()
            m = GPHOTOS_OG.search(r.text) if r.ok else None
            GPHOTOS[u] = {"i": m.group(1) if m else "", "d": today.isoformat()}
        except Exception as e:
            warn(f"Google Foto non risponde ({str(e)[:120]}): {u} resta da leggere")
        time.sleep(URL_PAUSE)
    if links:
        print(f" foto di Google Foto lette: {len(links)} ({sum(1 for u in links if GPHOTOS.get(u, {}).get('i'))} trovate)")
        GPHOTOS_FILE.write_text(json.dumps(GPHOTOS, indent=0, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def broken_links(t):
    """[(chiave, valore, stato)] dei link sicuramente rotti dell'albero."""
    out = [(k, u, URL_STATUS[u]["s"]) for k, u in link_urls(t) if URL_STATUS.get(u, {}).get("s") in URL_BROKEN]
    out += [("wikimedia_commons", f, "missing") for f in commons_files(t)
            if URL_STATUS.get("commons:" + f, {}).get("s") == "missing"]
    return out


def wikidata_image(t, key="image"):
    """File di Commons dell'immagine (P18) dell'elemento di wikidata=*, se non è un taxon (sarebbe la foto della
    specie, non dell'albero); None se manca. Con key="winter" la veduta invernale (P5252)."""
    for q in qids(t, "wikidata"):
        if taxon_status(q) is not True and WD_ITEMS.get(q, {}).get(key):
            return WD_ITEMS[q][key]
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


def commons_far(t, pos):
    """[(file, distanza in m, tipo)] dei file di Commons collegati (commons_photo_files) le cui coordinate
    distano più di COMMONS_MAX_DIST dall'albero; tipo "object" o "camera" (vedi update_url_cache)."""
    out = []
    for f in commons_photo_files(t):
        c = URL_STATUS.get("geo:" + f, {}).get("c")
        if c and (d := distance_m(pos, [c[1], c[0]])) > COMMONS_MAX_DIST:
            out.append((f, round(d), c[2]))
    return out


def wikidata_photos(t):
    """[(QID, "File:…")] delle foto (P18) degli elementi di wikidata=* che non sono taxon e non hanno coordinate
    (P625): le coordinate della foto su Commons sostituiscono quelle dell'elemento."""
    return [(q, "File:" + WD_ITEMS[q]["image"].replace("_", " ")) for q in qids(t, "wikidata")
            if taxon_status(q) is not True and not WD_ITEMS.get(q, {}).get("coords") and WD_ITEMS.get(q, {}).get("image")]


def wikidata_photo_far(t, pos):
    """[(QID, file, distanza in m, tipo)] delle foto di wikidata_photos le cui coordinate su Commons distano più di
    COMMONS_MAX_DIST dall'albero. Le foto già collegate all'albero (commons_photo_files) le controlla commons_far."""
    out, own = [], set(commons_photo_files(t))
    for q, f in wikidata_photos(t):
        c = URL_STATUS.get("geo:" + f, {}).get("c")
        if f not in own and c and (d := distance_m(pos, [c[1], c[0]])) > COMMONS_MAX_DIST:
            out.append((q, f, round(d), c[2]))
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


def type_ok(tag, expected, palm_like=False):
    """leaf_type compatibile: leafless non si confronta con la lista. Per le palme e le piante dall'aspetto di palma
    (palm_like, vedi palm_like) palm e broadleaved sono equivalenti; per le altre, palm non vale come broadleaved."""
    return tag == expected or tag == "leafless" or (palm_like and {tag, expected} == {"palm", "broadleaved"})


# Palme (#55). Le liste della wiki OSM non hanno palm tra i leaf_type: i generi di Arecaceae sono elencati qui.
# Minuscolo, come li confronta tree_genus.
PALM_GENERA = frozenset("""phoenix washingtonia trachycarpus chamaerops butia syagrus roystonea cocos livistona sabal
brahea jubaea archontophoenix howea dypsis chrysalidocarpus arecastrum areca elaeis caryota bismarckia copernicia rhapis
rhopalostylis wodyetia ravenea euterpe acrocomia borassus corypha latania hyophorbe pritchardia trithrinax nannorrhops
serenoa adonidia veitchia ptychosperma raphia attalea bactris mauritia ceroxylon parajubaea chamaedorea hyphaene thrinax
coccothrinax arenga licuala rhapidophyllum dictyosperma cyrtostachys hedyscepe phytelephas oenocarpus astrocaryum
aiphanes calamus metroxylon nypa pseudophoenix carpentaria normanbya saribus kentia neodypsis chambeyronia geonoma
socratea iriartea lodoicea medemia acoelorrhaphe acoelorraphe allagoptera lytocaryum beccariophoenix pinanga wallichia
salacca erythea""".split())
# Piante che non sono palme ma ne hanno l'aspetto (cicadee, yucche, dracene, banani, felci arboree…): con
# leaf_type=palm non sono segnalate da leaf_type_mismatch, ma non sono disegnate come palme
PALM_LIKE_GENERA = frozenset("""yucca cordyline dracaena cycas zamia encephalartos dioon macrozamia musa ensete
strelitzia ravenala pandanus dicksonia cyathea sphaeropteris alsophila beaucarnea nolina dasylirion xanthorrhoea kingia
furcraea aloe aloidendron carica""".split())


def tree_genus(t):
    """Genere dell'albero, minuscolo: da genus se è di una sola parola, altrimenti dalla prima parola di species o,
    se manca, di taxon."""
    return _tree_genus(t.get("genus"), t.get("species") or t.get("taxon") or "")


@functools.lru_cache(maxsize=None)
def _tree_genus(g, sp):
    if g and len(genus_words(g)) == 1:
        g = genus_words(g)[0]
    else:
        g = genus_of_species(sp)
    return g.lstrip("× ").casefold()


def is_palm(t):
    """Palma: taxon:family, family, taxon o genus = Arecaceae, taxon:en=Palm, crown_type=I, oppure un genere di
    PALM_GENERA. Vale più di leaf_type (tranne leafless): una palma con leaf_type=broadleaved è disegnata come palma."""
    # i tag si guardano solo se ci sono: quasi nessun albero li ha, e la funzione è chiamata più volte per albero
    for k in ("taxon:family", "family", "taxon", "genus"):
        if (v := t.get(k)) and v.strip().casefold() == "arecaceae":
            return True
    if ((v := t.get("taxon:en")) and v.strip().casefold() == "palm") or ((v := t.get("crown_type")) and v.strip() == "I"):
        return True
    return tree_genus(t) in PALM_GENERA


def palm_like(t):
    """Palma o pianta dall'aspetto di palma: leaf_type=palm non è un errore."""
    return is_palm(t) or tree_genus(t) in PALM_LIKE_GENERA


def genus_ref(t):
    """(nome, voce) del genere nella lista: dal tag genus se è di una sola parola, altrimenti dalla species."""
    g = t.get("genus")
    g = genus_words(g)[0] if g and len(genus_words(g)) == 1 else (genus_of_species(t["species"]) if t.get("species") else "")
    return GENUS_REF.get(g.lstrip("× ").casefold()) if g else None


def leaf_species_ref(t, key):
    """Voce della lista delle specie con il valore di key (leaf_type o leaf_cycle), se species=* è una sola specie
    della lista; altrimenti None (la stima viene dal genere)."""
    sp = t.get("species")
    ref = SPECIES_REF.get(norm_species(sp)) if sp and ";" not in sp else None
    return ref if ref and ref.get(key) else None


def leaf_type_estimate(t):
    """leaf_type ricavato dalle liste della wiki: dalla specie; se non è nella lista, dal genere. Per le palme
    (is_palm) è sempre palm."""
    if is_palm(t):
        return "palm"
    if ref := leaf_species_ref(t, "leaf_type"):
        return ref["leaf_type"]
    g = genus_ref(t)
    return g[1].get("leaf_type") if g else None


def leaf_cycle_estimate(t):
    """leaf_cycle ricavato dalle liste della wiki, come leaf_type_estimate; None se il genere li ammette entrambi."""
    if ref := leaf_species_ref(t, "leaf_cycle"):
        return ref["leaf_cycle"]
    g = genus_ref(t)
    cycles = g[1].get("leaf_cycle") or [] if g else []
    return cycles[0] if len(cycles) == 1 else None


# --- species scritta male: confronto con la lista delle specie ---
# Il nome (genere + epiteto, senza il segno di ibrido ×) si confronta con quelli della lista contando le lettere
# da cambiare (distanza di Levenshtein). Tarato sui dati: con 1 lettera sono quasi sempre errori di battitura
# (Olea europeea, Phoenix dactilifera); con 2 anche specie vere assenti dalla lista (Myrtus communis -> Pyrus
# communis), quindi a 2 si segnala solo se il genere scritto non è un genere noto; da 3 in su troppi falsi positivi.
@functools.lru_cache(maxsize=None)
def binomial(sp):
    """Genere ed epiteto normalizzati, senza ×: 'Citrus × limon L.' -> 'citrus limon'."""
    words = [w for w in norm_species(sp).split() if w != "×"]
    return " ".join(words[:2])


SPECIES_BIN = {}
for _name in _LIST:
    SPECIES_BIN.setdefault(binomial(_name), _name)
for _alt in _WL.get("synonyms", {}):        # i sinonimi della whitelist valgono come nomi della lista
    SPECIES_BIN.setdefault(binomial(_alt), _alt)
KNOWN_GENERA = {b.split()[0] for b in SPECIES_BIN} | set(GENUS_REF)
# epiteti delle specie della lista, di qualunque genere (species_epithet_case); non le cultivar ("Ulmus 'New Horizon'")
LIST_EPITHETS = {b.split()[1] for b, _name in SPECIES_BIN.items()
                 if len(b.split()) > 1 and name_words(_name)[1][:1].islower()}


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
    if ";" in sp:
        return None
    # genere e cultivar senza epiteto (Magnolia 'Susan'): la cultivar non è un epiteto da confrontare
    # (anche dopo il segno di ibrido: Magnolia x 'Vulcan')
    words = [w for w in name_words(sp) if w not in HYBRID_SIGNS]
    if len(words) > 1 and words[1][:1] in "'‘’`´\"“”":
        return None
    b = binomial(sp)
    # in whitelist (genere ed epiteto, senza ×: vale anche con la cultivar, Prunus × incam 'Okame')
    if b in TYPO_WHITELIST:
        return None
    if len(b.split()) < 2 or b in SPECIES_BIN:   # una parola: c'è già il check; in lista (anche cultivar): ok
        return None
    best, dist = None, 3
    letters = sorted(b)
    for cand, name in SPECIES_BIN.items():
        # stesse lettere in un altro ordine, nello stesso genere ("Prunus serratula" -> "Prunus serrulata"): vale più
        # della specie più vicina per lettere cambiate (qui "Prunus serrula")
        if len(cand) == len(b) and cand.split()[0] == b.split()[0] and sorted(cand) == letters:
            return name
        d = levenshtein(b, cand, 2)
        if d < dist:
            best, dist = (cand, name), d
    if best is None:
        return None
    # una specie della whitelist più vicina di quella della lista ("Quercus myrsinfolia": a una lettera da "Quercus
    # myrsinifolia", a due da "Quercus myrtifolia"): la specie da proporre non è in lista, quindi nessuna proposta
    if any(levenshtein(b, w, dist - 1) < dist for w in TYPO_WHITELIST):
        return None
    if dist == 2 and b.split()[0] != best[0].split()[0] and b.split()[0] in KNOWN_GENERA:
        return None   # genere diverso ed esistente: probabilmente un'altra specie, non un errore
    # seconda parola maiuscola di cinque lettere o meno, con due lettere cambiate e il genere scritto giusto ("Magnolia
    # Susan" -> "Magnolia yulan"): una cultivar senza virgolette (species_common_name), non un errore di battitura
    if (dist == 2 and b.split()[0] == best[0].split()[0] and len(b.split()[1]) <= 5
            and len(words) > 1 and words[1][:1].isupper()):
        return None
    return best[1]


def group_name(sp):
    """Nome normalizzato della specie, uguale per un nome e i suoi sinonimi (whitelist.json)."""
    n = norm_species(sp)
    return norm_species(GROUP_NAME.get(n, n))


def same_species(a, b):
    """a e b sono la stessa specie della lista o della whitelist (a può essere None)."""
    return bool(a) and group_name(a) == group_name(b)


def species_list_mismatches(t):
    """Incoerenze con le liste della wiki OSM: (codici, proprietà per i messaggi). species:wikidata si
    confronta con la lista delle specie; leaf_type e leaf_cycle con la specie e, se non è in lista, con il genere."""
    issues, props = [], {}
    from_genus = False   # exp_for è un genere: exp_genus, scritto solo insieme a un errore
    sp = t.get("species")
    if sp and ";" in sp:
        return issues, props
    ref = SPECIES_REF.get(norm_species(sp)) if sp and SPECIES_REF else None
    wd = (t.get("species:wikidata") or "").strip()
    # il QID va bene anche se su Wikidata il suo nome scientifico o il suo basionimo è proprio species (la lista wiki
    # può usare l'altro nome: Mespilus germanica = Crataegus germanica)
    if (sp and SPECIES_REF and wd and ";" not in wd and wd not in WD_WHITELIST.get(norm_species(sp), ())
            and binomial(sp) not in wd_names(wd)):
        if ref and ref.get("wikidata") and wd != ref["wikidata"]:
            issues.append("wikidata_mismatch")
            props.update(sp_wd=wd, exp_wd=ref["wikidata"])
        # QID noto, ma assegnato a un'altra specie; non se è la specie che species voleva scrivere (errore di
        # battitura: lo segnala species_typo, e correggendo species il QID va già bene)
        elif not ref and wd in QID_TO_SPECIES and not same_species(species_typo(sp.strip()), QID_TO_SPECIES[wd]):
            issues.append("wikidata_mismatch")
            props.update(sp_wd=wd, exp_sp=QID_TO_SPECIES[wd])
    if ref:
        exp_for, exp_lt = sp.strip(), ref.get("leaf_type")
        exp_lc = [ref["leaf_cycle"]] if ref.get("leaf_cycle") else []
    else:
        g = genus_ref(t)
        exp_for, exp_lt, exp_lc = (g[0], g[1].get("leaf_type"), g[1].get("leaf_cycle") or []) if g else (None, None, [])
        from_genus = bool(g)            # exp_for è un genere: il sito scrive "il genere X" / "the genus X"
    lt, lc = t.get("leaf_type"), t.get("leaf_cycle")
    # leaf_type=palm senza species né genus confrontabili: il genere si prende anche dalla prima parola di taxon
    # (taxon=Ginkgo biloba), solo per questo confronto
    if lt == "palm" and not exp_for and not palm_like(t) and (g := GENUS_REF.get(tree_genus(t))):
        exp_for, exp_lt = g[0], g[1].get("leaf_type")
        from_genus = True
    # leaf_type=palm su un albero che per la lista è broadleaved si segnala, tranne che per le palme e le piante
    # dall'aspetto di palma (palm_like); una palma con leaf_type=broadleaved non si segnala. Per i generi di
    # LEAF_TYPE_GENERA (Tamarix, Cycas) vale ogni leaf_type elencato in whitelist.json, anche con la specie in lista
    if (exp_for and lt in LEAF_TYPE and exp_lt and not type_ok(lt, exp_lt, palm_like(t))
            and lt not in LEAF_TYPE_GENERA.get(tree_genus(t), ())):
        issues.append("leaf_type_mismatch")
        props.update(exp_lt=exp_lt, exp_for=exp_for)
    if exp_for and lc in LEAF_CYCLE and exp_lc and not cycle_ok(lc, exp_lc):
        issues.append("leaf_cycle_mismatch")
        props.update(exp_lc=";".join(exp_lc), exp_for=exp_for)   # più cicli separati da ";": li unisce il sito
    if issues and from_genus and "exp_for" in props:
        props["exp_genus"] = True
    return issues, props


def species_limit(t):
    """Limiti e rapporti di species_limits.json per la specie dell'albero. I sinonimi della whitelist e le scritture
    diverse del nome di un gruppo ("x" al posto di "×") usano la voce del nome della lista wiki (GROUP_NAME)."""
    sp = t.get("species")
    return _species_limit(sp) if sp else None


@functools.lru_cache(maxsize=None)
def _species_limit(sp):
    sp = GROUP_NAME.get(norm_species(sp), sp)
    return SPECIES_LIMITS.get(" ".join(sp.split()).lower())


@functools.lru_cache(maxsize=None)
def _limit_by_binomial(sp):
    b = binomial(sp)
    return next((v for k, v in SPECIES_LIMITS.items() if binomial(k) == b), None)


def species_max_limit(t):
    """Limiti massimi (altezza e circonferenza) per i check height_species e circumference_species: quelli di
    species_limit o, se il nome ha altro dopo genere ed epiteto (cultivar, autore, rango: Pyrus calleryana
    'Chanticleer'), quelli della specie con lo stesso genere ed epiteto. I rapporti (height_circ_ratio) restano solo
    del nome esatto: una cultivar colonnare o nana ha un'altra forma."""
    sp = t.get("species")
    return _species_max_limit(sp) if sp else None


@functools.lru_cache(maxsize=None)
def _species_max_limit(sp):
    if lim := _species_limit(sp):
        return lim
    # non per un ibrido scritto con i due genitori (Salix alba × fragilis): non è la prima specie
    if ";" in sp or any(w in HYBRID_SIGNS for w in sp.split()[2:]):
        return None
    return _limit_by_binomial(sp)


LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE ={"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}

# Il raggio di ripiego della chioma (quando manca diameter_crown) non è nei dati: lo ricava il sito da id, denotation
# e "mg" (fallbackCrown in index.html)
DEFAULT_STUMP_R = 0.25  # m, raggio della X di un ceppo senza circumference


# --- misure con unità (https://wiki.openstreetmap.org/wiki/Map_features/Units) ---
# Unità nella forma corretta di OSM (numero, spazio, unità minuscola: "150 cm") e fattore per i metri; ft e in si
# leggono ma, come in Osmose, si propone di scriverli con ' e " (30', 7'4")
LEN_UNITS = {"m": 1, "cm": 0.01, "mm": 0.001, "km": 1000, "ft": 0.3048, "in": 0.0254}
# Altre scritture comprensibili: il valore si usa, ma il check unit_format propone la forma corretta
LEN_ALIASES = {"metre": "m", "metres": "m", "meter": "m", "meters": "m", "mt": "m", "mtr": "m",
               "feet": "ft", "foot": "ft", "inch": "in", "inches": "in"}
# il numero può iniziare con il punto (.75): si legge 0.75; non è un errore, index.html dà un consiglio
LEN_RE = re.compile(r"^\s*([+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+))(\s*)([a-zA-Z]*)\s*$")
# piedi e pollici: 7'4" (forma della wiki); solo pollici: 5". Con le parole (7 ft 4 in, 7 feet 4 inches) o con apici
# diversi da ' e " (7′4″, 7’4”, 7'4'') il valore si usa ma è segnalato. Gli spazi (7' 4") non sono un errore (anche
# Osmose li accetta): index.html dà un consiglio
FEET_INCH = re.compile(r"^\s*(?:(\d+)\s*(?:'|’|′|ft|feet|foot)\s*)?(?:(\d+(?:[.,]\d+)?)\s*(?:\"|''|”|″|in|inch|inches)\s*)?$",
                       re.I)
WRONG_QUOTES = re.compile(r"’|′|”|″|''|[a-zA-Z]")


def parse_length(v, default=1.0):
    """Misura in metri da un valore OSM, e la forma corretta da proporre se è scritta in modo non standard.
    default: metri per unità quando l'unità manca (1 = metri; 0.001 per diameter, in millimetri).
    Restituisce (metri, correzioni): correzioni è [] se la forma è corretta, altrimenti le scritture da proporre
    (es. "8 metres" -> ["8", "8 m"]; "2 M" su diameter -> ["2000", "2 m"]; 7′4″ -> ['7'4"']).
    (None, []) se il valore non è leggibile."""
    return (None, []) if v is None else _parse_length(str(v), default)


@functools.lru_cache(maxsize=None)
def _parse_length(s, default):
    """parse_length per valore: le stesse misure si ripetono e ogni albero le legge più volte (check, disegno,
    primati). La lista delle correzioni è condivisa tra le chiamate: non va modificata."""
    if m := LEN_RE.match(s):
        n, space, unit = m.group(1).replace(",", "."), m.group(2), m.group(3)
        if n.lstrip("+-").startswith("."):         # numero senza lo zero davanti al punto (.75): nelle correzioni
            n = n.replace(".", "0.", 1)            # proposte si scrive con lo zero
        if not unit:
            return float(n) * default, []          # solo numero (l'eventuale virgola è il check comma)
        canon = unit.lower()
        canon = LEN_ALIASES.get(canon, canon)
        if canon not in LEN_UNITS:
            return None, []
        x = float(n) * LEN_UNITS[canon]
        # piedi e pollici con le lettere (30 ft, 5 in): come Osmose, si propone 30' e 5"
        if canon in ("ft", "in"):
            return x, [n + ("'" if canon == "ft" else '"')]
        if unit == canon and space == " ":
            return x, []
        # forma corretta con l'unità e, se c'è, quella senza unità (metri; millimetri per diameter):
        # "8 metres" -> 8 oppure 8 m; diameter "2 M" -> 2000 oppure 2 m
        fix = [f"{n} {canon}"]
        if canon == "m" or LEN_UNITS[canon] == default:
            fix.insert(0, f"{x / default:.10g}")
        return x, fix
    m = FEET_INCH.match(s)
    if m and (m.group(1) or m.group(2)):
        ft_, inch = int(m.group(1) or 0), float((m.group(2) or "0").replace(",", "."))
        if not WRONG_QUOTES.search(s):
            return ft_ * 0.3048 + inch * 0.0254, []
        i = (m.group(2) or "").replace(",", ".")
        fix = (f"{m.group(1)}'" if m.group(1) else "") + (f'{i}"' if i else "")
        return ft_ * 0.3048 + inch * 0.0254, [fix]
    return None, []


def num(v):
    """Misura in metri (vedi parse_length); None se manca o non è leggibile."""
    return parse_length(v)[0]


UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def panoramax_ids(t):
    """UUID da panoramax=* (anche separati da ';'; ignora parametri tipo &xyz=)."""
    if "panoramax" not in t:
        return []
    ids = [x.lower() for x in UUID.findall(t["panoramax"])]
    return list(dict.fromkeys(ids))


NUMERIC_TAGS = ("circumference", "height", "diameter_crown", "diameter")


# diameter: senza unità è in millimetri (https://wiki.openstreetmap.org/wiki/Key:diameter), a differenza di
# circumference, height e diameter_crown, che sono in metri
DIAM_DEFAULT = 0.001
DIAM_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?|[.,]\d+)\s*([a-zA-Z]*)\s*$")
DIAM_MM_MIN = 10   # sotto: diameter senza unità troppo piccolo per i millimetri (tronco < 1 cm), check diameter_unit
# circumference e diameter insieme: segnalati se la circonferenza si scosta più del 20% da π × diametro (check
# circ_diam_mismatch). Il margine lascia passare le imprecisioni di misura (tronchi non rotondi, quote diverse);
# nei dati gli scarti sono quasi tutti sotto il 10% o molto grandi
CIRC_DIAM_TOL = 0.2


def diameter_small(t):
    """Vero se diameter non ha unità e vale meno di DIAM_MM_MIN (es. diameter=0.5): in millimetri sarebbe un
    tronco di meno di 1 cm. Lo zero è già segnalato da number_invalid."""
    return "diameter" in t and _diameter_small(str(t["diameter"]))


@functools.lru_cache(maxsize=None)
def _diameter_small(v):
    m = DIAM_RE.match(v)
    return bool(m) and not m.group(2) and 0 < float(m.group(1).replace(",", ".")) < DIAM_MM_MIN


def diameter_m(t):
    """Diametro del tronco in metri da diameter=*: senza unità in mm, altrimenti con l'unità scritta
    (vedi parse_length). None se manca, non è leggibile o è troppo piccolo per i millimetri (diameter_small)."""
    if "diameter" not in t or diameter_small(t):
        return None
    return parse_length(t["diameter"], DIAM_DEFAULT)[0]


def unit_fixes(t):
    """Tag numerici con l'unità scritta in modo non standard ma comprensibile (8 metres, 2 M, 150cm, 12 mt, 7′4″):
    [(chiave, valore, [correzioni])]. Il valore è comunque usato per disegnare l'albero."""
    out = []
    for k in NUMERIC_TAGS:
        if k in t and not bad_number(t[k]):
            v, fix = parse_length(t[k], DIAM_DEFAULT if k == "diameter" else 1)
            if fix:
                out.append((k, t[k], fix))
    return out


def circ_value(t):
    """Circonferenza in m, e se deriva da diameter. Se manca circumference ma c'è diameter si usa
    π × diametro (vedi diameter_m); se ci sono entrambi i tag, diameter è ignorato."""
    if "circumference" in t:
        return num(t["circumference"]), False
    d = diameter_m(t)
    return (d * math.pi, True) if d is not None else (None, False)


def comma_tags(t):
    """Tag numerici scritti con la virgola decimale (2,9 invece di 2.9); diameter è controllato anche se ignorato.
    Solo se il valore è un numero: gli elenchi ("10, 12", "66,60,51", "14 x 16,30") li segnala number_invalid."""
    return [k for k in NUMERIC_TAGS if "," in str(t.get(k, "")) and not bad_number(t[k])]


# --- check su numeri, collegamenti e fonti monumentali ---
WIKIDATA = re.compile(r"^Q[1-9]\d*$")
UUID_START = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:&.*)?$")
URL = re.compile(r"^https?://\S+$", re.I)


def bad_number(v):
    """Vero se il valore non è una misura leggibile (numero con o senza unità, vedi parse_length) oppure è <= 0.
    Segnala quindi testo, intervalli, elenchi, unità sconosciute e valori nulli o negativi."""
    x = parse_length(v)[0]
    return x is None or x <= 0


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
    # image: un solo indirizzo o un solo file di Commons (File:…, che dà il consiglio di spostarlo in wikimedia_commons:
    # index.html); più indirizzi separati da ";" (con o senza spazi) sono segnalati
    if "image" in t and not COMMONS_IMAGE_NAME.match(t["image"]) and (
            not URL.match(t["image"].strip()) or sum(1 for x in t["image"].split(";") if URL.match(x.strip())) > 1):
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


# heritage:operator degli enti che tutelano gli alberi monumentali: ICNF (Portogallo, árvores de interesse público)
# e ANF (Lussemburgo, Administration de la nature et des forêts)
HERITAGE_OPERATORS = ("icnf", "anf")
# codici del CRFOP (Centralny Rejestr Form Ochrony Przyrody, registro polacco dei pomniki przyrody, issue #8):
# ref:CRFOP e ref:gid. Su un ceppo sono normali (il monumento naturale di un albero che non c'è più)
CRFOP_KEYS = ("ref:CRFOP", "ref:gid")

# tag dei registri nazionali degli alberi monumentali: (chiave, valore o None per qualsiasi valore, codice ISO della
# nazione). Check register_country: il tag su un albero di una regione di un'altra nazione
NATIONAL_TAGS = (("ref:masaf", None, "IT"), ("masaf:criteria", None, "IT"), ("ref:rami", None, "IT"),
                 ("ref:icnf", None, "PT"), ("heritage:operator", "icnf", "PT"),
                 ("heritage:operator", "anf", "LU"),
                 ("ref:CRFOP", None, "PL"), ("ref:gid", None, "PL"))
COUNTRY = ""   # codice ISO della nazione della regione che si sta aggiornando (update_region); "" = non noto
REGION_ISO = ""   # codice ISO completo della stessa regione (GB-SCT; per una regione-nazione come COUNTRY)


def register_out_of_country(t):
    """[(tag, nazione)] dei tag di NATIONAL_TAGS di un'altra nazione rispetto a COUNTRY: "ref:masaf" o
    "heritage:operator=anf" e il codice ISO della nazione del registro. Vuota se la nazione della regione non è nota."""
    if not COUNTRY:
        return []
    out = []
    for k, v, cc in NATIONAL_TAGS:
        if cc == COUNTRY or not t.get(k):
            continue
        if v is None:
            out.append((k, cc))
        elif v in (x.strip().lower() for x in str(t[k]).split(";")):
            out.append((f"{k}={v}", cc))
    return out


def monument_sources(t):
    """Fonti che indicano un albero monumentale: ref:masaf, ref:rami, ref:monumentaltrees, ref:CRFOP, ref:gid,
    heritage:operator di HERITAGE_OPERATORS, elemento di wikidata=* con P9704 o istanza di Q811534, link a
    monumentaltrees.com."""
    src = [k for k in ("ref:masaf", "ref:rami", "ref:monumentaltrees", *CRFOP_KEYS) if t.get(k)]
    if "heritage:operator" in t:
        src += [f"heritage:operator={v}" for v in (x.strip().lower() for x in str(t["heritage:operator"]).split(";"))
                if v in HERITAGE_OPERATORS]
    # elemento Wikidata dell'albero con l'id di monumentaltrees.com (P9704) o istanza di pianta monumentale (Q811534)
    for q in qids(t, "wikidata"):
        item = WD_ITEMS.get(q, {})
        src += [f"wikidata={q} ({p})" for p, ok in (("P9704", item.get("mt")), ("Q811534", item.get("mon"))) if ok]
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


# Elenco MASAF (scripts/masaf_list.json, da update_masaf.py): codice -> null se iscritto, [motivo, decreto] se rimosso
from update_masaf import masaf_code   # noqa: E402 (stessa normalizzazione dei codici della lista)
MASAF_LIST = load_json(HERE / "masaf_list.json", "codes")
# criteri degli alberi iscritti (scripts/masaf_trees.json, campo "cr"), per il consiglio hint_mc
MASAF_CRITERIA_OF = {x["c"]: x["cr"] for x in load_json(HERE / "masaf_trees.json", "trees") or [] if x.get("cr")}
MASAF_ORDER = ["a1", "a2.1", "a2.2", "a2.3", "b", "c", "d", "e", "f", "g"]
# circonferenze minime MASAF per specie (scripts/masaf_thresholds.json, in cm), per il consiglio hint_ms; solo in Italia
MASAF_MIN_CIRC = {binomial(k): v for k, v in load_json(HERE / "masaf_thresholds.json", "species").items()}
IN_ITALY = False   # lo imposta update_region per la regione che si sta aggiornando


# Import di alberi riconosciuti da source=* (scripts/imports.json, issue #2), per i consigli hint_imp_u e hint_imp_d
def source_key(v):
    """Valore di source normalizzato per il confronto: minuscolo, senza spazi ai lati e senza la barra finale."""
    return v.strip().casefold().rstrip("/")


_IMPORTS = load_json(HERE / "imports.json")
IMPORTS_UNDOC = {source_key(v): v for v in _IMPORTS.get("undocumented", [])}
IMPORTS_DOC = {source_key(v): (v, url) for v, url in _IMPORTS.get("documented", {}).items()}


def import_source(t):
    """("d", valore, pagina della wiki) per un import documentato, ("u", valore, None) per uno non documentato,
    None se source=* non indica un import noto."""
    if "source" not in t:
        return None
    for raw in str(t["source"]).split(";"):
        k = source_key(raw)
        if k in IMPORTS_DOC:
            return ("d", raw.strip(), IMPORTS_DOC[k][1])
        if k in IMPORTS_UNDOC:
            return ("u", raw.strip(), None)
    return None


def masaf_removed(t):
    """[(ref:masaf, motivo, decreto)] dei codici di un albero (non di un ceppo) che l'elenco MASAF dà come rimossi
    (morte naturale, abbattimento, perdita di requisiti, declassamento). Per un ceppo il codice di un albero rimosso
    è normale."""
    if "ref:masaf" not in t or t.get("natural") == "tree_stump":
        return []
    out = []
    for raw in (x.strip() for x in str(t["ref:masaf"]).split(";")):
        if raw and (rm := MASAF_LIST.get(masaf_code(raw))):
            out.append((raw, rm[0], rm[1]))
    return out


def masaf_unknown(t):
    """Codici di ref:masaf che non sono nell'elenco MASAF, né tra gli iscritti né tra i rimossi. Il primo numero può
    avere due o tre cifre (02/G114/CN/01 vale come 002/G114/CN/01, masaf_code), non una sola (2/G114/CN/01 si
    segnala). Niente se l'elenco non è caricato o se la regione è di un'altra nazione (lo segnala già register_country)."""
    if "ref:masaf" not in t or not MASAF_LIST or (COUNTRY and COUNTRY != "IT"):
        return []
    return [raw for raw in (x.strip() for x in str(t["ref:masaf"]).split(";"))
            if raw and (masaf_code(raw) not in MASAF_LIST or len(re.sub(r"\s+", "", raw).split("/")[0]) < 2)]


# --- Insiemi omogenei MASAF (issue #9) ---
# L'elenco MASAF a volte censisce un gruppo di alberi con un solo codice. La wiki (IT:Key:ref:masaf, «Gli "insiemi
# omogenei"») lo mappa con una relazione type=site + ref:masaf + natural=tree_group + denotation=natural_monument che ha
# per membri gli alberi; i singoli alberi hanno specie e misure e una denotation diversa (park, urban…). Nelle regioni
# italiane un nodo natural=tree membro di almeno una relazione con ref:masaf (basta quel tag sulla relazione) vale
# come un albero con denotation=natural_monument: disegno, chioma di ripiego, file ridotto, mappa dei monumentali
# (fetch_world.py) e check small_monument; niente consiglio hint_ms. Non è una fonte di monument_denotation (l'albero è
# mappato come dice la wiki) e sulla relazione non ci sono check. I ceppi e gli altri membri (vie, relazioni, nodi non
# natural=tree) non cambiano.
# Proprietà "mg" della feature: una riga per relazione, separate da "\n", "id|rimosso|ref:masaf|masaf:criteria|name"
# (rimosso = 1 se l'elenco MASAF dà come rimosso almeno un codice della relazione, come masaf_removed per gli alberi;
# name per ultimo, perché può contenere "|"). Il sito ne ricava la riga del gruppo nella scheda.
MASAF_GROUPS = {}   # id del nodo -> [righe di "mg"], per la regione che si sta aggiornando (update_region)


def masaf_groups_query(region):
    """Relazioni con ref:masaf che hanno almeno un membro nell'area della regione, con tag e membri."""
    return f"""[out:json][timeout:180];
{area_of(region)}
rel["ref:masaf"](area.a);
out body;"""


def masaf_groups(elements):
    """{id del nodo: [righe di "mg"]} dalle relazioni con ref:masaf di una risposta di Overpass. Solo i membri nodo:
    quali sono alberi lo decide to_feature (natural=tree). Una relazione senza nodi membri non dà niente."""
    out = collections.defaultdict(set)
    for el in elements:
        t = el.get("tags", {})
        ref = " ".join(str(t.get("ref:masaf", "")).split())
        if el.get("type") != "relation" or not ref:
            continue
        rm = any(MASAF_LIST.get(masaf_code(c)) for c in (x.strip() for x in ref.split(";")) if c)
        row = "|".join([str(el["id"]), "1" if rm else "0", ref, str(t.get("masaf:criteria", "")).strip(),
                        " ".join(str(t.get("name", "")).split())])
        for m in el.get("members", []):
            if m.get("type") == "node":
                out[m["ref"]].add(row)
    return {k: sorted(v, key=lambda r: int(r.split("|")[0])) for k, v in out.items()}


def previous_groups(path):
    """Righe "mg" dell'ultimo aggiornamento della regione (file path), per quando le relazioni non si leggono."""
    if not path.exists():
        return {}
    return {f["properties"]["id"]: f["properties"]["mg"].split("\n")
            for f in json.loads(gzip.decompress(path.read_bytes()))["features"] if f["properties"].get("mg")}


def load_masaf_groups(region, path):
    """MASAF_GROUPS della regione (solo in Italia). Se Overpass non risponde l'aggiornamento continua con un avviso e
    con i gruppi dell'ultimo aggiornamento (dal file della regione), per gli alberi che ci sono ancora."""
    data = run_overpass(masaf_groups_query(region), f"{region['slug']} relazioni con ref:masaf", False, http_timeout=240)
    if data is not None:
        groups = masaf_groups(data["elements"])
        rels = sum(1 for el in data["elements"] if el.get("type") == "relation")
        print(f"{region['slug']}: {rels} relazioni con ref:masaf, {len(groups)} nodi membri")
        return groups
    groups = previous_groups(path)
    warn(f"{region['slug']}: relazioni con ref:masaf non lette (Overpass non risponde), uso i gruppi dell'ultimo "
         f"aggiornamento ({len(groups)} alberi membri)")
    return groups


def masaf_member(osm_id):
    """Vero se il nodo è membro di una relazione con ref:masaf (MASAF_GROUPS)."""
    return osm_id is not None and bool(MASAF_GROUPS.get(osm_id))


# --- Forma di species: specie non identificata (sp., spp.) e autore del nome (L., (L.) Kuntze, Merr.…) ---
UNIDENTIFIED = re.compile(r"^\s*(×\s*)?(\S+)\s+(sp|spp)\.?\s*$", re.I)
INFRA = {"subsp.", "ssp.", "var.", "subvar.", "f.", "forma", "cv.", "nothosubsp.", "nothovar."}
# autore: iniziale maiuscola e punto (L., Mill., J.), oppure tra parentesi ((L.), (Lindl.))
AUTHOR = re.compile(r"^\(?[A-ZÀ-Ý][\w'’.-]*\.\)?$|^\([^)]*[A-ZÀ-Ý][^)]*\)$|^\([A-ZÀ-Ý]")
AUTHOR_LINK = {"ex", "&", "et", "in"}   # parole che uniscono due autori: "Kuntze ex Bureau"


@functools.lru_cache(maxsize=None)
def species_unidentified(sp):
    """Il genere, se species è "Genere sp." o "Genere spp." (specie non identificata); altrimenti None."""
    m = UNIDENTIFIED.match(sp or "")
    return m.group(2) if m else None


EQ_KEYS = ("species", "genus", "taxon", "name", "start_date", "ref:masaf", "ref:rami")   # check tag_in_value


def not_latin(v):
    """True se il valore contiene lettere di un alfabeto non latino (cirillico, greco…); le lettere accentate latine vanno bene."""
    s = str(v or "")
    if s.isascii():   # il caso normale: nessuna lettera da controllare una per una
        return False
    return any(c.isalpha() and not unicodedata.name(c, "").startswith("LATIN") for c in s)
SCI_NAME = re.compile(r"^(×\s*)?[A-Z][a-zë-]+\s+(×\s*)?[a-zë-]+$")   # forma di un nome scientifico: Genere epiteto


@functools.lru_cache(maxsize=None)
def species_common_name(sp):
    """Se species non ha la forma di un nome scientifico perché la seconda parola è maiuscola (nomi comuni come
    "Black Matipo", "Pino Marittimo", cultivar senza virgolette come "Malus Golden Hornet"):
    il nome scientifico se è scritto tra parentesi ("Black Matipo (Pittosporum tenuifolium)"), altrimenti "".
    None se species ha la forma di un nome scientifico. Le cultivar tra virgolette e gli autori non contano."""
    if not sp or ";" in sp:
        return None
    words = name_words(sp)                       # senza "x" iniziale: x Chitalpa tashkentensis
    if len(words) < 2:
        return None                              # una parola sola: c'è già il check apposito
    second = words[1]
    if not second[:1].isupper() or second[:1] in "'‘`\"“(" or AUTHOR.match(second):
        return None
    if second == "X" and len(words) > 2 and words[2][:1].islower():
        return None                              # segno di ibrido scritto maiuscolo ("Crataegus X prunifolia")
    m = re.search(r"\(([^)]*)\)", sp)
    return m.group(1).strip() if m and SCI_NAME.match(m.group(1).strip()) else ""


@functools.lru_cache(maxsize=None)
def species_epithet_case(sp):
    """Se species è un nome scientifico scritto con l'epiteto maiuscolo: il nome corretto. Vale per i nomi della
    lista ("Catalpa Bignonioides" -> "Catalpa bignonioides"; quello che segue l'epiteto resta com'è) e per i nomi di
    due parole con un genere noto e un epiteto della lista ("Prunus Chinensis" -> "Prunus chinensis"; non "Malus
    Zierform"); altrimenti None. Anche l'epiteto con l'iniziale minuscola ma una
    maiuscola dentro ("Ginkgo biloBa") e il genere con maiuscole dopo l'iniziale ("GINKGO biloba"), vedi
    species_inner_case."""
    if species_common_name(sp) is None:
        return species_inner_case(sp)
    if binomial(sp) in SPECIES_BIN:
        listed = SPECIES_BIN[binomial(sp)]
        if norm_species(sp) == norm_species(listed):
            return listed                        # lo stesso nome, a parte maiuscole e virgolette
        # quello che in species segue l'epiteto (cultivar, rango, altro) resta com'è: "Pyrus Communis 'Beech Hill'"
        # -> "Pyrus communis 'Beech Hill'". Della lista si prendono genere ed epiteto, con il segno di ibrido
        sw, lw = sp.split(), listed.split()
        sp_pos = [i for i, w in enumerate(sw) if w not in HYBRID_SIGNS]
        l_pos = [i for i, w in enumerate(lw) if w != "×"]
        if len(l_pos) < 2 or lw[l_pos[1]][:1] in "'‘’`´\"“”":
            return None                          # in lista c'è genere e cultivar: il resto non si può attaccare
        return " ".join(lw[:l_pos[1] + 1] + sw[sp_pos[1] + 1:])
    words = [w for w in sp.split() if w != "×"]
    # due parole con un genere noto: la proposta c'è solo se la seconda è un epiteto, cioè l'epiteto di una specie
    # della lista (di qualunque genere: "Prunus Chinensis") o a poche lettere da una specie della lista ("Quercus
    # Rubur"). "Malus Zierform", "Tilia Greenspire" restano a species_common_name, senza proposta. Con il nome tutto
    # maiuscolo ("PINUS JEFFREYI") o con "SP" ("Cedrus SPP") la maiuscola della seconda parola non distingue niente
    if (len(words) == 2 and words[0].casefold() in KNOWN_GENERA
            and (words[1].casefold() in LIST_EPITHETS or sp.isupper() or species_unidentified(sp) or species_typo(sp))):
        # il genere con la sola iniziale maiuscola ("PRUNUS Communis" -> "Prunus communis", non "PRUNUS communis")
        return f"{words[0][:1].upper()}{words[0][1:].lower()} {words[1].lower()}"
    return None


def species_inner_case(sp):
    """Se l'epiteto ha l'iniziale minuscola e c'è una maiuscola fuori posto, nell'epiteto ("Ginkgo biloBa",
    "Platanus ×hispaNica") o nel genere dopo l'iniziale ("GINKGO biloba", "GinKgo biloba"): species con genere ed
    epiteto scritti bene ("Ginkgo biloba"), il resto (autore, cultivar) com'è. Il genere con la sola iniziale
    minuscola ("ginkgo biloba") non conta: c'è species_lowercase. Vale anche il segno di ibrido scritto "X" tra
    genere ed epiteto ("Crataegus X prunifolia" -> "Crataegus × prunifolia"); "x" minuscolo e "X" all'inizio non
    contano. Solo per i nomi della lista e per i nomi di due parole con un genere noto, come species_epithet_case;
    altrimenti None."""
    if not sp or ";" in sp:
        return None
    words = sp.split()
    pos = [i for i, w in enumerate(words) if w not in ("×", "x", "X")]
    if len(pos) < 2:
        return None
    g, e = words[pos[0]], words[pos[1]]
    gp = g[:len(g) - len(g.lstrip("×"))]                    # "×" attaccato al genere (×Chitalpa)
    gn, ep = g[len(gp):], e.lstrip("×")
    if not ep[:1].islower() or any(q in g + e for q in "'‘’`´\"“”"):
        return None
    hx = [i for i in range(pos[0] + 1, pos[1]) if words[i] == "X"]   # segno di ibrido maiuscolo
    if ep == ep.lower() and gn[1:] == gn[1:].lower() and not hx:
        return None                                         # nessuna maiuscola fuori posto
    if binomial(sp) not in SPECIES_BIN and not (len(pos) == 2 and gn.casefold() in KNOWN_GENERA):
        return None
    words[pos[0]] = gp + gn[:1].upper() + gn[1:].lower()
    words[pos[1]] = e.lower()
    for i in hx:
        words[i] = "×"
    return " ".join(words)


AUTHOR_PARTICLE = {"de", "van", "von", "der", "den", "du", "da", "di", "d'", "le", "la", "ter"}   # "Siebold ex de Vriese"


def author_group(words):
    """Vero se le parole di un gruppo tra parentesi ("(Siebold", "&", "Zucc.)") sono un autore: tutte con la maiuscola,
    o parole che uniscono due autori, o particelle, o "f." (filius: "(Thunb. ex L. f.)"). "(Ulmus pedenculata, Ulmus
    effusa)" non lo è (epiteti minuscoli)."""
    parts = [w.strip("(),") for w in words]
    return all(not w or w[:1].isupper() or w in AUTHOR_LINK or w in AUTHOR_PARTICLE or w == "f." for w in parts)


def paren_author(words, i, j):
    """Vero se il gruppo tra parentesi words[i..j], fatto di parole maiuscole (author_group), è scritto come un
    autore: ha un'abbreviazione con il punto ("(L.)", "(Siebold & Zucc.)"), o una parola che unisce due autori, o è
    seguito dall'autore della combinazione ("(Siebold) Siebold ex de Vriese": dopo un autore tra parentesi c'è sempre).
    Parole maiuscole e basta in fondo al nome ("Quercus robur (Farnia)", "Hechtia sp. 10429 (Lunel)") non lo sono:
    restano a species_parentheses."""
    group = [w.strip("(),") for w in words[i:j + 1]]
    if any("." in w or w in AUTHOR_LINK for w in group):
        return True
    return j + 1 < len(words) and words[j + 1][:1].isupper()


@functools.lru_cache(maxsize=None)
def split_author(sp):
    """(species senza autore, autore trovato, resto incerto) per species_without_author e species_author_uncertain.
    Resto incerto: parole tenute che non sono genere, epiteto, ranghi infraspecifici né cultivar tra virgolette
    ("Insieme omogeneo di Fagus sylvatica L."): senza l'autore non resta un nome scientifico sicuro."""
    if not sp or ";" in sp or species_unidentified(sp):
        return None, False, False
    words = sp.split()
    base = 3 if {"×", "x", "X"} & set(words[:2]) else 2   # genere ed epiteto (con il segno di ibrido)
    if len(words) <= base or not words[base - 1][:1].islower():
        return None, False, False                # epiteto maiuscolo: non è un nome scientifico (es. "Black Matipo (…)")
    kept, found, in_author, quoted, extra, i = words[:base], False, False, False, False, base
    # pending: parole maiuscole senza punto dopo l'epiteto ("Siebold", "Sukaczev"): sono l'autore se dopo vengono un
    # autore, una parola che unisce due autori o un rango (una cultivar non sta prima di un rango); altrimenti restano
    pending = []
    while i < len(words):
        w = words[i]
        if quoted or w[:1] in "'‘`\"“":             # cultivar tra virgolette: si tiene tutta
            kept += pending + [w]
            pending, in_author = [], False
            quoted = not (w[-1:] in "'’`\"”" and (len(w) > 1 or quoted))
        elif w[:1] == "(":                           # parentesi: "(L.)", "(Siebold & Zucc.)", "(Ulmus …, …)"
            j = next((k for k in range(i, len(words)) if ")" in words[k]), len(words) - 1)
            if author_group(words[i:j + 1]) and paren_author(words, i, j):
                found, in_author, pending = True, True, []
            else:
                kept += pending + words[i:j + 1]
                pending, in_author, extra = [], False, True
            i = j
        elif w.lower() in INFRA and i + 1 < len(words):
            found = found or bool(pending)
            kept += [w, words[i + 1]]
            pending, in_author = [], False
            i += 1
        elif (AUTHOR.match(w) or (in_author and (w[:1].isupper() or w in AUTHOR_LINK))
              or (w in AUTHOR_LINK and pending)
              or (in_author and w in AUTHOR_PARTICLE and i + 1 < len(words) and words[i + 1][:1].isupper())):
            found, in_author, pending = True, True, []
        elif w[:1].isupper():
            pending.append(w)
        else:
            kept += pending + [w]
            pending, in_author, extra = [], False, True
        i += 1
    # dopo un rango con il suo epiteto, in un nome che ha già un autore: "… var. angustifolia Kimura"
    if not (pending and found and len(kept) > base and kept[-2].lower() in INFRA):
        kept += pending
    return (" ".join(kept) if found else None), found, extra


def species_without_author(sp):
    """species senza l'autore del nome, se c'è (es. "Camellia sinensis (L.) Kuntze" -> "Camellia sinensis",
    "Taxus cuspidata Siebold et Zucc." -> "Taxus cuspidata"); restano genere, epiteto, i ranghi infraspecifici
    (var., subsp. …) e le cultivar tra virgolette. None se non c'è autore."""
    return split_author(sp)[0]


def species_author_uncertain(sp):
    """Vero se species ha l'autore ma, tolto l'autore, resta altro oltre al nome scientifico (vedi split_author)."""
    return split_author(sp)[2]


# Varietà e cultivar scritte in species (issue #68): "var. epiteto" (epiteto minuscolo) e la cultivar tra virgolette,
# anche dopo "cv."; le virgolette sono quelle di QUOTES
VAR_EPITHET = re.compile(r"(?<!\S)var\.?\s+([a-zë-]+)(?!\S)")   # anche "var" senza punto
CV_QUOTED = re.compile(r"(?<!\S)(?:cv\.\s+)?['‘`´\"“](.+?)['’`´\"”](?!\S)")
ABBR_GENUS = re.compile(r"^[A-Z]\.$")                            # genere abbreviato: "T. baccata"
# Autori scritti senza punto dopo genere ed epiteto ("Betula pendula Roth", "Olea europaea L"): non sono cultivar
# senza virgolette. Lista scritta a mano, da quelli che si trovano nei dati e dai più comuni tra i nomi degli alberi
AUTHOR_NAMES = {"Linnaeus", "Linn", "Miller", "Mill", "Roth", "Vahl", "Marshall", "Marsh", "Carriere", "Carrière",
                "Labill", "Chabaud", "Zeyh", "Moench", "Moensch", "Borkh", "Donn", "Pall", "Lam", "Thunb", "Ehrh",
                "Willd", "Scop", "Münchh", "Desf", "Franco", "Mirb", "Karst", "Ten", "Sarg", "Koch", "Medik", "Aiton",
                "Jacq", "Michx", "Lindl", "Maxim", "Kuntze", "Bunge", "Batsch", "Gaertn", "Huds", "Poir", "Wangenh",
                "Siebold", "Zucc", "Hook", "Benth", "Roxb", "Blume", "Lour", "Burm", "Osbeck", "Swingle", "Cav",
                "Engelm", "Parl", "Rehder", "Schneid", "Hayata", "Mast", "Spach", "Gouan", "Planch", "Endl", "Manetti"}
ABBR_INDEX = {}   # (iniziale, epiteto) -> generi delle specie della lista; riempito alla prima chiamata


def expand_genus(initial, epithet, genus):
    """Genere per un genere abbreviato in species ("T. baccata"): quello di genus=*, se comincia con quella lettera
    ed è un genere noto (o con l'epiteto dà una specie della lista); senza genus, l'unico genere della lista wiki con
    quell'iniziale e una specie con quell'epiteto ("Z. serrata" -> Zelkova; "C. japonica" ne ha più d'uno). None se
    non si può dire."""
    if not ABBR_INDEX:
        for b, name in SPECIES_BIN.items():
            w = name_words(name)
            if len(b.split()) == 2 and w[1][:1].islower():
                ABBR_INDEX.setdefault((b[0], b.split()[1]), set()).add(w[0].lstrip("×"))
    g = genus.strip()
    if g:
        ok = (len(g.split()) == 1 and g[0] == initial and g[1:].isalpha() and g[1:].islower()
              and (g.casefold() in KNOWN_GENERA or binomial(f"{g} {epithet}") in SPECIES_BIN))
        return g if ok else None
    found = ABBR_INDEX.get((initial.lower(), epithet.casefold()), set())
    return next(iter(found)) if len(found) == 1 else None


_VARIETY_NAMES = {}   # WD_TAXA (l'oggetto) -> nomi normalizzati dei taxon di rango varietà; riempito alla prima chiamata


def known_variety(base, variety):
    """Vero se "base var. variety" è il nome di un taxon di rango varietà (Q767728) tra quelli di
    data/wikidata_taxa.json ("Populus alba var. pyramidalis"): solo per questi la correzione scrive taxon:variety,
    perché con "var." si trovano scritte anche le cultivar ("Pyrus calleryana var. chanticleer")."""
    if id(WD_TAXA) not in _VARIETY_NAMES:
        _VARIETY_NAMES.clear()
        _VARIETY_NAMES[id(WD_TAXA)] = {norm_species(n) for e in WD_TAXA.values() if "Q767728" in e.get("ranks", [])
                                       for n in e.get("names", [])}
    return norm_species(f"{base} var. {variety}") in _VARIETY_NAMES[id(WD_TAXA)]


def unquoted_cultivar(base):
    """(genere ed epiteto, cultivar) se base è una specie della lista wiki seguita da una cultivar senza virgolette
    ("Pyrus calleryana Chanticleer", "Tilia cordata Green Spire", punto 3.3 del report): dopo l'epiteto solo parole,
    la prima con la maiuscola e almeno tre lettere, nessuna un autore (con il punto, AUTHOR; senza, AUTHOR_NAMES), un
    rango o un segno di ibrido. Con l'epiteto seguito da una parola minuscola ("Quercus robur fastigiata") non si può
    dire se è una cultivar o una varietà: None."""
    w = base.split()
    n = 3 if len(w) > 2 and w[1] in HYBRID_SIGNS else 2
    rest = w[n:]
    if (not rest or binomial(base) not in SPECIES_BIN or not w[n - 1].lstrip("×")[:1].islower()
            or not rest[0][:1].isupper() or len(rest[0]) < 3 or split_author(base)[1]):
        return None
    if any(x in AUTHOR_NAMES or x in HYBRID_SIGNS or x.lower() in INFRA or not x[:1].isalpha()
           or any(c in x for c in ".()=+,/") for x in rest):
        return None
    return " ".join(w[:n]), " ".join(rest)


@functools.lru_cache(maxsize=None)
def species_infra(sp, genus=""):
    """(species senza varietà e cultivar, varietà, cultivar, genere abbreviato: bool) se species ha una varietà
    ("Quercus robur var. fastigiata" -> ("Quercus robur", "fastigiata", "", False); anche "var" senza punto), una
    cultivar tra virgolette ("Acer palmatum 'Atropurpureum'" -> ("Acer palmatum", "", "Atropurpureum", False)) o
    senza, dopo una specie della lista (unquoted_cultivar), o il genere abbreviato ("T. baccata 'Fastigiata'" con
    genus=Taxus -> ("Taxus baccata", "", "Fastigiata", True): expand_genus), anche insieme; la cultivar è senza
    virgolette, come in taxon:cultivar. Il resto del nome (subsp.) resta com'è; con l'autore None. Con un genere della lista e la
    sola cultivar ("Magnolia 'Susan'") il primo elemento è il genere ("Magnolia"). None se non c'è niente di questo,
    con più di una varietà o cultivar, con più valori (";"), se il valore intero è un nome della lista ("Ulmus 'New
    Horizon'"), se il genere abbreviato non si può sciogliere, o se quello che resta ha ancora virgolette, finisce con
    un rango ("Quercus robur f. 'Fastigiata'") o con il segno di ibrido."""
    # con l'autore c'è già species_author, che propone la sua species: prima quella correzione
    if not sp or ";" in sp or norm_species(sp) in SPECIES_REF or split_author(sp)[1]:
        return None
    words, abbr = sp.split(), False
    if ABBR_GENUS.match(words[0]):
        if (len(words) < 2 or not re.fullmatch(r"[a-zë-]+", words[1])
                or not (g := expand_genus(words[0][0], words[1], genus))):
            return None
        sp, abbr = " ".join([g] + words[1:]), True
    vr, cv = VAR_EPITHET.findall(sp), CV_QUOTED.findall(sp)
    if len(vr) > 1 or len(cv) > 1:
        return None
    cultivar = cv[0].strip() if cv else ""
    base = " ".join(CV_QUOTED.sub(" ", VAR_EPITHET.sub(" ", sp)).split())
    if not cv and (uq := unquoted_cultivar(base)):
        base, cultivar = uq
    if not (vr or cultivar or abbr):
        return None
    words = name_words(base)
    if (not words or base != base.translate(QUOTES) or words[-1].lower() in INFRA or words[-1] in HYBRID_SIGNS
            or (cv and (not cultivar or cultivar[0] in "'‘’`´\"“”" or cultivar[-1] in "'‘’`´\"“”"))):
        return None
    if len(words) == 1:   # genere e cultivar: solo con un genere della lista, senza varietà
        ref = GENUS_REF.get(words[0].lstrip("×").casefold())
        return (ref[0], "", cultivar, False) if ref and cultivar and not vr and not abbr and base == words[0] else None
    if not words[1].lstrip("×")[:1].islower():
        return None
    return base, (vr[0] if vr else ""), cultivar, abbr


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
SPECIES_LANG = re.compile(r"^species:([a-z]{2,3})(?:[-_][A-Za-z]{2,4})?$")
# I codici di due lettere devono essere lingue di ISO 639-1: species:mp e species:at non lo sono. Quelli di tre
# lettere (ISO 639-2 e 639-3, troppi da elencare) valgono tutti, tranne le sottochiavi che non indicano una lingua
LANG_2 = set("""aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz
ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja
jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng
nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw
ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu""".split())
NOT_LANG_3 = {"ref", "lat", "src", "alt", "old"}


def lang_key(pattern, k):
    """Vero se la chiave k (species:xx, taxon:xx) finisce con il codice di una lingua (vedi LANG_2, NOT_LANG_3)."""
    m = pattern.match(k)
    return bool(m) and (m.group(1) in LANG_2 if len(m.group(1)) == 2 else m.group(1) not in NOT_LANG_3)


def species_lang_only(t):
    """Nomi della specie in qualche lingua (species:xx) su un albero senza species=*: [(chiave, valore)]."""
    if t.get("species"):
        return []
    return sorted((k, v) for k, v in t.items() if k.startswith("species:") and lang_key(SPECIES_LANG, k))


# Nome di taxon=* in una lingua: taxon:<codice lingua> (taxon:en, taxon:de…); taxon:cultivar e simili non sono lingue
TAXON_LANG = re.compile(r"^taxon:([a-z]{2,3})(?:[-_][A-Za-z]{2,4})?$")


def taxon_lang_only(t):
    """Nomi del taxon in qualche lingua (taxon:xx) su un albero senza taxon=*: [(chiave, valore)]."""
    if t.get("taxon"):
        return []
    return sorted((k, v) for k, v in t.items() if k.startswith("taxon:") and lang_key(TAXON_LANG, k))


def taxon_lang_moves(t, tl):
    """Nomi di taxon_lang_only (tl) che si possono scrivere in genus:xx o species:xx: (righe, tag).
    righe = [(chiave, valore, tipo, nome, "chiave nuova=valore nuovo")], tipo "g" se il valore è il nome comune di un
    genere (GENUS_NAMES, nome = il genere; va in genus:xx con l'iniziale maiuscola e senza «sp.»), "s" se è il nome di
    una specie (SPECIES_NAMES, nome = la specie; va in species:xx, scritto come nella lista), "p" se non è un nome noto
    ma species=* c'è già (va in species:xx com'è). tag = "species=…" o "genus=…" da aggiungere perché manca, o "".
    Una riga non c'è se il tag nuovo ha già un altro valore; nessuna riga se i nomi indicano generi o specie diversi
    tra loro o da genus e species."""
    sp, g = str(t.get("species") or "").strip(), str(t.get("genus") or "").strip()
    if ";" in sp or ";" in g:
        return [], ""
    rows, genera, specs = [], {genus_key(x) for x in (g, sp) if x}, {binomial(sp)} if sp else set()
    for k, v in tl:
        lang, v = k[6:], v.strip()
        code, name = re.split(r"[-_]", lang)[0], SP_SUFFIX.sub("", v)
        key = (code, name.casefold())
        if (gen := GENUS_NAMES.get(key)) and genus_name_here(key):
            row = (k, v, "g", gen, f"genus:{lang}", name[:1].upper() + name[1:])
            genera.add(genus_key(gen))
        elif s := SPECIES_NAMES.get((code, v.casefold())):
            row = (k, v, "s", s, f"species:{lang}", SPECIES_NAME_FORM.get((code, v.casefold())) or v)
            genera.add(genus_key(s))
            specs.add(binomial(s))
        elif sp:
            row = (k, v, "p", "", f"species:{lang}", v)
        else:
            continue
        if (cur := str(t.get(row[4]) or "").strip()) and cur.casefold() != row[5].casefold():
            continue
        rows.append((*row[:4], f"{row[4]}={row[5]}"))
    if len(genera) > 1 or len(specs) > 1:
        return [], ""
    tag = ""
    if not sp and (s := next((r[3] for r in rows if r[2] == "s"), None)):
        tag = f"species={s}"
    elif not sp and not g and (gen := next((r[3] for r in rows if r[2] == "g"), None)):
        tag = f"genus={gen}"
    return rows, tag


# seconda parola di un nome di specie: l'epiteto, in minuscolo (con un trattino: "Prunus x-yedoensis" no, "Picea
# abies" sì). Per taxon_precise
EPITHET = re.compile(r"[a-z]+(?:-[a-z]+)?")
# parole che dopo il genere non sono un epiteto (Quercus sp, Pinus spp, Tilia x)
NOT_EPITHETS = {"sp", "spp", "ssp", "subsp", "var", "cv", "f", "x", "hybrid", "species"}


def taxon_precise(t):
    """Tag più preciso per il valore di taxon=*, dalle liste wiki: ("species", nome della lista) se è genere ed
    epiteto di una specie della lista (senza cultivar o altro), ("genus", genere) se è un genere della lista;
    ("species", valore, "b") se non è nella lista ma è un genere noto con l'iniziale maiuscola seguito da un epiteto
    (taxon=Citrus sinensis): genere ed epiteto sono la forma del nome di una specie. None altrimenti, o se species o
    genus (o il genere di species) dicono già un'altra cosa: la proposta non sarebbe certa."""
    tx = str(t.get("taxon") or "").strip()
    if not tx or ";" in tx or "=" in tx:
        return None
    sp, g = str(t.get("species") or "").strip(), str(t.get("genus") or "").strip()
    words = name_words(tx)
    if len(words) == 2 and (name := SPECIES_BIN.get(binomial(tx))):
        same_g = not g or genus_key(g) == genus_key(genus_of_species(name))
        if same_g and (not sp or binomial(sp) == binomial(name)):
            return "species", name
        return None
    if (len(words) == 2 and words[0][:1].isupper() and words[0][1:].islower() and words[0].casefold() in KNOWN_GENERA
            and EPITHET.fullmatch(words[1]) and words[1] not in NOT_EPITHETS):
        if (not g or genus_key(g) == words[0].casefold()) and (not sp or binomial(sp) == binomial(tx)):
            return "species", " ".join(tx.split()), "b"
        return None
    if len(words) == 1 and (gr := GENUS_REF.get(words[0].lstrip("×").casefold())):
        if (not g or genus_key(g) == genus_key(gr[0])) and (not sp or genus_key(genus_of_species(sp)) == genus_key(gr[0])):
            return "genus", gr[0]
    return None


# "sp." o "spp." dopo il nome comune: species:pl=Klon sp. vale come Klon (sp_name in index.html)
SP_SUFFIX = re.compile(r"\s+spp?\.?$", re.I)


def species_lang_genus(t, lang):
    """Nomi di species_lang_only che nella lista dei generi sono il nome comune di un genere nella stessa lingua
    (species:fr=Tilleul -> Tilia; species:en-GB vale come en; species:pl=Klon sp. come Klon): (ok, altro).
    ok = [(chiave, valore, genere, specie)] se genus manca o è già quel genere; specie è l'unica specie del genere
    (SINGLE_SPECIES, es. Cognassier -> Cydonia oblonga), altrimenti "". altro = [(chiave, valore, genere)] se genus è
    un genere diverso (species:pl=Dąb con genus=Acer). Con la stessa lingua in genus:xx (genus:pl per species:pl):
    gk = chiavi species:xx il cui nome è già in genus:xx (senza «sp.» e maiuscole); gkx = [(chiave, valore, genere,
    "genus:xx=valore")] se genus:xx ha un altro nome (species:pl=Dąb con genus:pl=Buk): due generi, nessun consiglio."""
    g = (t.get("genus") or "").strip()
    ok, other, gk, gkx = [], [], [], []
    for k, v in lang:
        name = SP_SUFFIX.sub("", v.strip())
        key = (re.split(r"[-_]", k[8:])[0], name.casefold())
        gen = GENUS_NAMES.get(key) if genus_name_here(key) else None
        if not gen:
            continue
        gkey = "genus:" + k[8:]
        gv = (t.get(gkey) or "").strip()
        if g and g.casefold() != gen.casefold():
            other.append((k, v, gen))
        elif gv and SP_SUFFIX.sub("", gv).casefold() != name.casefold():
            gkx.append((k, v, gen, f"{gkey}={t[gkey]}"))
        else:
            ok.append((k, v, gen, SINGLE_SPECIES.get(gen, "")))
            if gv:
                gk.append(k)
    return ok, other, gk, gkx


def species_lang_species(t, lang):
    """Nomi di species_lang_only che sono il nome comune di una specie in SPECIES_NAMES (species:pl=Klon zwyczajny
    -> Acer platanoides): [(chiave, valore, specie, nome)], con nome = il nome scritto come in whitelist.json se le
    maiuscole sono diverse (species:en=northern red oak -> Northern Red Oak), altrimenti "". Il confronto con genus lo
    fa index.html."""
    out = []
    for k, v in lang:
        key = (re.split(r"[-_]", k[8:])[0], v.strip().casefold())
        if s := SPECIES_NAMES.get(key):
            form = SPECIES_NAME_FORM.get(key, "")
            out.append((k, v, s, form if form != v.strip() else ""))
    return out


def cached_species_qid(species):
    """QID di una specie che non è nella lista wiki (species:it=Pino d'Aleppo -> Pinus halepensis), dalla cache di
    Wikidata: l'unico elemento taxon di rango specie con quel nome scientifico (P225), altrimenti None."""
    return _cached_species_qid(species, len(WD_TAXA))   # la cache cresce da una regione all'altra


@functools.lru_cache(maxsize=None)
def _cached_species_qid(species, _n):
    b = binomial(species)
    found = {q for q, e in WD_TAXA.items() if e.get("taxon") and "Q7432" in e.get("ranks", [])
             and any(binomial(n) == b for n in e.get("names", []))}
    return next(iter(found)) if len(found) == 1 else None


def species_lang_extras(t, species, prefix="sp_lang_"):
    """Tag che mancano e che la specie proposta per un species:xx (species, un solo nome) permette di aggiungere
    insieme a species=*: {"sp_lang_wd": species:wikidata della lista wiki, "sp_lang_lt": leaf_type, "sp_lang_lc":
    leaf_cycle}. Servono alla correzione con JOSM, che aggiunge species. Con prefix "sp_fix_" gli stessi tag per la
    species corretta da species_author o species_typo."""
    t2 = {**t, "species": species}
    out = {}
    if "species:wikidata" not in t and (q := list_qid(species) or cached_species_qid(species)):
        out[prefix + "wd"] = q
    if "leaf_type" not in t and (lt := leaf_type_estimate(t2)):
        out[prefix + "lt"] = lt
    if "leaf_cycle" not in t and (lc := leaf_cycle_estimate(t2)):
        out[prefix + "lc"] = lc
    return out


def not_a_tree(t):
    """Pianta di NOT_TREES (non un albero, es. Agave americana) indicata da species=* (genere ed epiteto, anche con
    autore o cultivar) o da species:wikidata=*, su un natural=tree: il nome della specie, altrimenti None."""
    if t.get("natural") != "tree":
        return None
    b, qs = binomial(t.get("species") or ""), qids(t, "species:wikidata")
    return next((s for s, q in NOT_TREES.items() if (b and binomial(s) == b) or q in qs), None)


def common_name_genus(word):
    """Genere di cui word è il nome comune in una lingua di GENUS_NAMES (species=oak -> Quercus), se è uno solo."""
    return common_name_genus_lang(word)[0]


def common_name_genus_lang(word):
    """(genere, lingua) se word è il nome comune di un solo genere in GENUS_NAMES (genus=wierzba -> ("Salix", "pl"));
    lingua "" se il nome è lo stesso in più lingue; (None, "") se non è il nome di un genere o è di più generi."""
    w = word.strip().casefold()
    found = {(lang, g) for (lang, n), g in GENUS_NAMES.items() if n == w and genus_name_here((lang, n))}
    genera = {g for _, g in found}
    if len(genera) != 1:
        return None, ""
    langs = {lang for lang, _ in found}
    return next(iter(genera)), next(iter(langs)) if len(langs) == 1 else ""


def undocumented_tags(t):
    """Tag non documentati presenti: [(chiave, valore)], esclusi quelli in UNDOC_IGNORE."""
    return [(k, t[k]) for k in UNDOC_TAGS if k in t and (k, t[k].strip()) not in UNDOC_IGNORE]


# --- chiavi scritte male (issue #32): Species, heigth, leaftype, circumfence ---
# Chiavi note degli alberi: una chiave che non è tra queste ma ne differisce di poco è segnalata (check key_typo)
KNOWN_KEYS = ("natural", "species", "genus", "taxon", "leaf_type", "leaf_cycle", "denotation", "circumference",
              "height", "diameter_crown", "diameter", "start_date", "name", "wikidata", "wikipedia",
              "wikimedia_commons", "image", "species:wikidata", "species:wikipedia", "genus:wikidata",
              "genus:wikipedia", "taxon:wikidata", "taxon:wikipedia", "ref:masaf", "masaf:criteria", "ref:rami",
              "ref:monumentaltrees", "heritage:operator", "panoramax", "website", "multi_stemmed", "sex",
              "sacred_tree", "religion", "operator", "source", "description", "check_date", "survey:date",
              "protected", "image:fotoshi", "ref:zapis", "ref:icnf", "ref:CRFOP", "ref:gid")
KNOWN_KEY_SET = set(KNOWN_KEYS)
NUMBERED_KEY = re.compile(r"[:_ ]?\d+$")   # image2, panoramax:1: varianti numerate, non errori


def key_tolerance(k):
    """Differenze ammesse rispetto a una chiave nota (distanza di Damerau-Levenshtein, oltre al maiuscolo): nessuna
    per le chiavi corte (name, sex), dove una lettera fa un'altra parola; 1 da 5 lettere; 2 da 12 lettere
    (circumfence, diameter_crwn), dove due lettere non bastano a fare un'altra chiave. Per le chiavi ref:… conta la
    parte dopo "ref:": ref:gid, ref:rami e ref:icnf valgono come chiavi corte (ref:uid e ref:fid sono altre chiavi)."""
    if k.startswith("ref:"):
        k = k[4:]
    return 0 if len(k) < 5 else 1 if len(k) < 12 else 2


def osa_distance(a, b, limit):
    """Distanza tra a e b contando come un solo errore lo scambio di due lettere vicine (heigth, Damerau-Levenshtein
    ristretta), o limit + 1 se è maggiore di limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    d = [list(range(len(b) + 1))] + [[i] + [0] * len(b) for i in range(1, len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
        if min(d[i]) > limit:
            return limit + 1
    return d[-1][-1]


@functools.lru_cache(maxsize=None)
def key_typo(k):
    """Chiave nota che k probabilmente voleva scrivere, o None. Non sono errori le chiavi note, le loro sottochiavi
    (species:wikipedia:it, circumference:note) e le varianti numerate (image2, panoramax:1)."""
    if k in KNOWN_KEY_SET or NUMBERED_KEY.sub("", k) in KNOWN_KEY_SET:
        return None
    if any(k.startswith(known + ":") for known in KNOWN_KEYS):
        return None
    low = k.casefold()
    best, dist = None, 99
    for known in KNOWN_KEYS:
        d = osa_distance(low, known, key_tolerance(known))
        if d <= key_tolerance(known) and d < dist:
            best, dist = known, d
    return best


def key_typos(t):
    """Chiavi scritte male: [(chiave, chiave nota, valore)]."""
    return [(k, kn, v) for k, v in t.items() if (kn := key_typo(k))]


def slenderness(t):
    """height / circumference (anche da diameter); None per i ceppi o se manca una delle due misure."""
    if t.get("natural") == "tree_stump":
        return None
    c, h = circ_value(t)[0], num(t.get("height"))
    return h / c if c and h and c > 0 and h > 0 else None


def checks(t, pos, osm_id=None):
    """Check QA di un albero con tag t in posizione pos ([lon, lat]) e id osm_id (per le eccezioni della
    whitelist): (codici degli errori, proprietà per i messaggi della scheda), in un solo passaggio.
    Per aggiungere un check: una riga qui con flag(codice, proprietà), e il messaggio in index.html."""
    issues, props = [], {}

    def flag(code, **p):
        issues.append(code)
        props.update(p)

    c, h, cd = circ_value(t)[0], num(t.get("height")), num(t.get("diameter_crown"))
    # circumference non leggibile (circumference=big, già number_invalid) con diameter: il limite si controlla su
    # diameter, che altrimenti non sarebbe guardato da nessun check. Solo per il limite: il disegno non lo usa
    c_max = d * math.pi if c is None and (d := diameter_m(t)) else c
    if c_max is not None and c_max > MAX_CIRCUMFERENCE:   # diam_used: il messaggio dice «(da diameter)»
        flag("circumference", **({"diam_used": True} if c is None and "circumference" in t else {}))
    if h is not None and h > MAX_HEIGHT:
        flag("height")
    if cd is not None and cd > MAX_CROWN:
        flag("crown")
    if comma := comma_tags(t):
        flag("comma", comma=";".join(comma))
    if diameter_small(t):   # come comma, vale anche se c'è circumference e diameter non si usa
        flag("diameter_unit")
    if uf := unit_fixes(t):  # righe "chiave|valore|correzione|…" separate da "\n" (i valori possono contenere ";")
        flag("unit_format", unit_fix="\n".join("|".join([k, v, *fix]) for k, v, fix in uf))
    if "circumference" in t and c and c > 0 and (d := diameter_m(t)) and abs(c / (math.pi * d) - 1) > CIRC_DIAM_TOL:
        flag("circ_diam_mismatch", cd_circ=round(math.pi * d, 2))   # circonferenza che corrisponde a diameter
    lim = species_limit(t) or {}  # limite per specie: solo se non è già scattato quello generico
    mx = species_max_limit(t) or {}   # limiti massimi: anche per cultivar, autore e rango della stessa specie
    if "circumference" in mx and c is not None and "circumference" not in issues and c > mx["circumference"]:
        flag("circumference_species", lim_c=mx["circumference"],
             **({"lim_c_src": mx["circumference_source"]} if "circumference_source" in mx else {}))
    if "height" in mx and h is not None and "height" not in issues and h > mx["height"]:
        flag("height_species", lim_h=mx["height"], **({"lim_h_src": mx["height_source"]} if "height_source" in mx else {}))
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
    # parole di species senza il segno di ibrido scritto a sé (× o x): "x carnea" è una parola sola
    spw = name_words(spf) if spf is not None else []
    # genere seguito dal solo autore ("Tilia L.", "Malus Hybr."): vale come una parola sola
    genus_author = len(spw) == 2 and bool(AUTHOR.match(spw[1])) and not species_unidentified(spf)
    one_word = len(spw) == 1 or genus_author
    if one_word:
        # se la parola è un genere della lista wiki (es. species=magnolia), la scheda propone genus=Magnolia
        # se è il nome comune di un genere (species=oak), propone genus=Quercus (sp_common_genus)
        gr = GENUS_REF.get(spw[0].lstrip("×").casefold())
        cg = None if gr else common_name_genus(spw[0])
        flag("species", **({"sp_as_genus": gr[0]} if gr else {"sp_common_genus": cg} if cg else {}))
    # Una sola correzione per le maiuscole, perché le proposte non si contraddicano: con l'autore la propone
    # species_author (sp_clean già con le maiuscole giuste), altrimenti species_epithet_case, che sistema anche
    # l'iniziale del genere; species_lowercase resta per i nomi che hanno solo l'iniziale minuscola
    no_auth = species_without_author(spf) if spf else None
    ec = species_epithet_case(spf) if spf and not no_auth else None
    # con una sola parola basta l'errore precedente: la correzione è la stessa, minuscola o no
    # il segno di ibrido iniziale scritto "x" non è un'iniziale minuscola
    if len(spw) > 1 and not one_word and spw[0].lstrip("×")[:1].islower() and not no_auth and not ec:
        flag("species_lowercase")
    if spf and (g_un := species_unidentified(spf)):
        flag("species_unidentified", sp_genus=g_un)
    if ec:                                                        # maiuscole sbagliate nel nome scientifico
        flag("species_epithet_case", sp_sci=ec)
    elif spf and (common := species_common_name(spf)) is not None:  # "" se non c'è un nome scientifico da proporre
        flag("species_common_name", **({"sp_sci": common} if common else {}))
    if no_auth:   # senza proposta se, tolto l'autore, non resta solo il nome ("Insieme omogeneo di Fagus sylvatica L.")
        clean = species_inner_case(no_auth) or no_auth
        flag("species_author", **({} if species_author_uncertain(spf) else {"sp_clean": clean[:1].upper() + clean[1:]}))
    # nessun nome scientifico contiene parentesi; non si ripete se le spiegano già l'autore o il nome comune
    if spf and ("(" in spf or ")" in spf) and not {"species_author", "species_common_name"} & set(issues):
        flag("species_parentheses")
    if und := [k for k in ("species", "genus") if "_" in str(t.get(k, "")) and k not in eq]:
        flag("underscore", und_keys=";".join(und))
    # più valori separati da ";" in species o genus: un nodo è un solo albero, con un solo genere e una sola specie
    if mv := [k for k in ("species", "genus") if ";" in str(t.get(k, "")) and k not in eq]:
        flag("multiple_values", mv_keys=";".join(mv))
    typo = species_typo(spf) if spf and SPECIES_BIN else None
    if typo:
        flag("species_typo", sp_sugg=typo)
    # Correzione con JOSM della species: sp_fix = la specie della lista proposta da species_typo, solo se species (tolto
    # l'autore) è genere ed epiteto e basta, così la correzione non perde una cultivar o altro; con l'autore vale anche
    # per species_author, perché le due proposte non si contraddicano (Cupréssus sempervìrens L. -> Cupressus
    # sempervirens). sp_fix_wd, sp_fix_lt, sp_fix_lc: species:wikidata, leaf_type e leaf_cycle che mancano, per la
    # specie corretta (sp_fix o sp_clean): la correzione li aggiunge insieme a species
    if typo and not (no_auth and "sp_clean" not in props) and len(name_words(no_auth or spf)) == 2:
        props["sp_fix"] = typo
    if fixed := props.get("sp_fix") or props.get("sp_clean"):
        props.update(species_lang_extras(t, fixed, "sp_fix_"))
    if lang := species_lang_only(t):    # "species:it=Leccio" separati da "\n" (i valori possono contenere ";")
        # sp_lang_genus: righe "species:fr=Tilleul|Tilia" per i nomi di un genere della lista (issue #35), con l'unica
        # specie del genere se c'è: "species:fr=Cognassier|Cydonia|Cydonia oblonga". sp_lang_gx: le stesse righe
        # "species:pl=Dąb|Quercus" quando genus è un altro genere (l'incongruenza si segnala)
        # sp_lang_sp: righe "species:pl=Klon zwyczajny|Acer platanoides" per i nomi di una specie (species_names), con il
        # nome scritto come nella lista se le maiuscole sono diverse: "species:en=bur oak|Quercus macrocarpa|Bur Oak"
        # sp_lang_gk: chiavi species:xx (righe) il cui nome è già in genus:xx; c'è sempre insieme a sp_lang_genus, anche
        # vuoto, così index.html riconosce i dati che hanno il confronto con genus:xx (la correzione con JOSM lo richiede).
        # sp_lang_gkx: righe "species:pl=Dąb|Quercus|genus:pl=Buk" quando genus:xx ha un altro nome
        gl, gx, gk, gkx = species_lang_genus(t, lang)
        sl = species_lang_species(t, lang)
        # sp_lang_wd, sp_lang_lt, sp_lang_lc: species:wikidata, leaf_type e leaf_cycle che mancano, per la specie
        # proposta (se è una sola): la correzione con JOSM li aggiunge insieme a species
        cand = {s for _, _, s, _ in sl} | {s for _, _, _, s in gl if s}
        if len(cand) == 1:
            props.update(species_lang_extras(t, next(iter(cand))))
        flag("species_lang_only", sp_lang="\n".join(f"{k}={v}" for k, v in lang),
             **({"sp_lang_sp": "\n".join(f"{k}={v}|{s}" + (f"|{n}" if n else "") for k, v, s, n in sl)} if sl else {}),
             **({"sp_lang_genus": "\n".join(f"{k}={v}|{g}" + (f"|{s}" if s else "") for k, v, g, s in gl),
                 "sp_lang_gk": "\n".join(gk)} if gl else {}),
             **({"sp_lang_gx": "\n".join(f"{k}={v}|{g}" for k, v, g in gx)} if gx else {}),
             **({"sp_lang_gkx": "\n".join(f"{k}={v}|{g}|{x}" for k, v, g, x in gkx)} if gkx else {}))
    if tl := taxon_lang_only(t):        # "taxon:en=Oak" separati da "\n" (i valori possono contenere ";")
        # tx_lang_mv: righe "taxon:it=pino|g|Pinus|genus:it=Pino" per i nomi che si possono scrivere in genus:xx o
        # species:xx (taxon_lang_moves; tipo g, s o p); tx_lang_add: "species=…" o "genus=…" da aggiungere con loro.
        # tx_lang_wd, tx_lang_lt, tx_lang_lc: species:wikidata, leaf_type e leaf_cycle che mancano per quella species
        mv, add = taxon_lang_moves(t, tl)
        if add.startswith("species="):
            props.update(species_lang_extras(t, add[8:], "tx_lang_"))
        flag("taxon_lang_only", tx_lang="\n".join(f"{k}={v}" for k, v in tl),
             **({"tx_lang_mv": "\n".join(f"{k}={v}|{kind}|{n}|{nkv}" for k, v, kind, n, nkv in mv)} if mv else {}),
             **({"tx_lang_add": add} if add else {}))
    if nt_sp := not_a_tree(t):          # una pianta che non è un albero: natural=plant (nt_sp: il nome della specie)
        flag("not_a_tree", nt_sp=nt_sp)
    g = t.get("genus")
    gf = None if "genus" in eq or "genus" in nl else g   # genus per i check sulla forma del nome
    gw = genus_words(gf) if gf is not None else None    # senza il segno di ibrido (× Sycoparrotia)
    if gw is not None and len(gw) > 1:
        flag("genus")
    # genus di una parola che non è un genere noto ma il nome comune di un genere (genus=wierzba -> Salix, in polacco):
    # al posto di genus_lowercase, che proporrebbe la maiuscola di un nome che non va bene comunque
    gcn, gcn_lang = (common_name_genus_lang(gw[0]) if gw and len(gw) == 1 and gw[0].casefold() not in KNOWN_GENERA
                     else (None, ""))
    if gcn and gcn.casefold() == gw[0].casefold():
        gcn = None
    if gcn:
        flag("genus_common_name", g_common=gcn, **({"g_common_lang": gcn_lang} if gcn_lang else {}))
    elif gw and gw[0][:1].islower():
        flag("genus_lowercase")
    if "leaf_type" in t and t["leaf_type"] not in LEAF_TYPE:
        flag("leaf_type")
    if "leaf_cycle" in t and t["leaf_cycle"] not in LEAF_CYCLE:
        flag("leaf_cycle")
    # species di una sola parola minuscola (es. species=alba): è un epiteto, non un genere da confrontare
    # (anche dopo il segno di ibrido: "x carnea"). Con più valori in genus o in species (Crataegus;Quercus) non si
    # confronta: il genere di una specie non è quello delle altre. Dopo il segno di ibrido vale anche la parola
    # maiuscola che non è un genere noto ("x Sepulcralis" con genus=Salix): è l'epiteto scritto maiuscolo
    sp_words = name_words(sp) if sp is not None else []
    sp_epithet = len(sp_words) == 1 and (sp_words[0][:1].islower() or (
        sp.split()[0] in HYBRID_SIGNS and sp_words[0].lstrip("×").casefold() not in KNOWN_GENERA))
    if (g is not None and sp is not None and ";" not in g and ";" not in sp and len(gw := genus_words(g)) == 1
            and not sp_epithet and not nl):
        sg = genus_of_species(sp).lstrip("×")   # il segno di ibrido non conta, né nel genus né nella species
        # con genus nome comune dello stesso genere di species basta genus_common_name
        if sg and gw[0].casefold() != sg.casefold() and not (gcn and gcn.casefold() == sg.casefold()):
            # correzione (gm_fix): genus non è un genere noto, quello di species sì, e i due nomi differiscono di poco
            # (genus=Styphlonobium con species=Styphnolobium japonicum): un errore di battitura in genus
            gm = (sg[:1].upper() + sg[1:] if sg.casefold() in KNOWN_GENERA and gw[0].casefold() not in KNOWN_GENERA
                  and not gcn and levenshtein(gw[0].casefold(), sg.casefold(), 2) <= 2 else None)
            flag("genus_mismatch", **({"gm_fix": gm} if gm else {}))
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
    if far := wikidata_far(t, pos):     # "QID:km;…"
        flag("wikidata_far", wd_far=";".join(f"{q}:{km}" for q, km in far))
    if wpf := wikidata_photo_far(t, pos):   # righe "QID|metri|tipo|File:…" separate da "\n"
        flag("wikidata_photo_far", wd_ph_far="\n".join(f"{q}|{m}|{k}|{f}" for q, f, m, k in wpf))
    if bad := bad_numeric_tags(t):
        flag("number_invalid", bad_num=";".join(bad))
    if bad := bad_link_tags(t):
        flag("link_format", bad_links=";".join(bad))
    if bl := broken_links(t):           # righe "chiave=valore (stato)" separate da "\n" (i valori possono contenere ";")
        flag("broken_link", bad_url="\n".join(f"{k}={v} ({s})" for k, v, s in bl))
    # website=* o url=* con una voce di Wikipedia: wpw "chiave|lingua:Titolo"; wpw_wp il valore di wikipedia=*, se
    # c'è (non è tra le proprietà della feature); wpw_same se è la stessa voce (la correzione con JOSM toglie la chiave)
    if wpw := website_wikipedia(t):
        wp = str(t.get("wikipedia") or "").strip()
        flag("website_wikipedia", wpw="|".join(wpw), **({"wpw_wp": wp} if wp else {}),
             **({"wpw_same": 1} if wp and wp_key(wp) == wp_key(wpw[1]) else {}))
    if cf := commons_far(t, pos):       # righe "metri|tipo|File:…" separate da "\n" (i nomi possono contenere ";")
        flag("commons_far", cm_far="\n".join(f"{m}|{k}|{f}" for f, m, k in cf))
    # un solo check per tutte le fonti monumentali, anche la tutela su Wikidata (P1435, wd_her_*): chiedono tutte
    # la stessa correzione
    # Su un ceppo i codici CRFOP non chiedono natural_monument (CRFOP_KEYS)
    hr = wikidata_heritage(t)
    src = monument_sources(t)
    den_src = [s for s in src if s not in CRFOP_KEYS] if t.get("natural") == "tree_stump" else src
    if (den_src or hr) and "natural_monument" not in [x.strip() for x in t.get("denotation", "").split(";")]:
        flag("monument_denotation", mon_src=";".join(den_src))
        if hr:
            q, (label_it, label_en) = hr[0]
            props.update(wd_her_q=q, wd_her_it=label_it, wd_her_en=label_en)
    # albero monumentale (natural_monument, una fonte monumentale o membro di un insieme omogeneo MASAF) molto piccolo,
    # non in whitelist (issue #34). Non si ripete la misura che un check sul rapporto altezza/circonferenza dà già per
    # sbagliata: la circonferenza se l'albero è troppo slanciato, l'altezza se è troppo tozzo. sm_keys: misure sotto la
    # soglia, separate da ";". Essere membro di un insieme omogeneo non è una fonte di monument_denotation (MASAF_GROUPS)
    if ((src or masaf_member(osm_id) or "natural_monument" in [x.strip() for x in t.get("denotation", "").split(";")])
            and t.get("natural") != "tree_stump" and osm_id not in SMALL_MON_WHITELIST):
        sm = []
        if h is not None and 0 < h < SMALL_MON_HEIGHT and "too_stocky" not in issues:
            sm.append("height")
        if c is not None and 0 < c < SMALL_MON_CIRC and not {"too_slender", "too_slender_species"} & set(issues):
            sm.append("circumference")
        if sm:
            flag("small_monument", sm_keys=";".join(sm), **({"sm_circ": round(c, 3)} if "circumference" in sm else {}))
    if den := denotation_problems(t):   # valori, motivi e usi, nello stesso ordine
        flag("denotation_value", den_bad=";".join(v for v, _, _ in den), den_why=";".join(w for _, w, _ in den),
             den_n=";".join(str(n) for _, _, n in den))
    if bad := masaf_criteria_bad(t):    # parti separate da "," come nel tag (non possono contenerne)
        flag("masaf_criteria", masaf_bad=",".join(bad))
    if rm := masaf_removed(t):          # righe "codice|motivo|decreto" separate da "\n"
        flag("masaf_removed", masaf_rm="\n".join(f"{c}|{r}|{d}" for c, r, d in rm))
    if unk := masaf_unknown(t):         # codici separati da ";"
        flag("masaf_unknown", masaf_unk=";".join(unk))
    if "start_date" in t and "start_date" not in eq and (why := start_date_problem(t["start_date"])):
        flag("start_date_invalid", **({"sd_future": 1} if why == "future" else {}))
    # voce di wikipedia=* (wp_v) il cui elemento Wikidata (wp_tx) è un taxon; wp_genus: è di rango genere (Q34740), la
    # voce va in genus:wikipedia e non in species:wikipedia
    if wpt := wikipedia_taxon(t):
        flag("wikipedia_is_taxon", wp_tx=wpt, wp_v=t["wikipedia"],
             **({"wp_genus": 1} if "Q34740" in WD_TAXA.get(wpt, {}).get("ranks", []) else {}))
    if ro := register_out_of_country(t):   # "tag|nazione;…" (tag: "ref:masaf" o "heritage:operator=anf")
        flag("register_country", reg_out=";".join(f"{k}|{cc}" for k, cc in ro))
    if undoc := undocumented_tags(t):  # i tag non sono in KEEP: il valore va nella proprietà tag_<chiave>
        flag("undocumented_tag", **{"tag_" + k: v for k, v in undoc})
    if kt := key_typos(t):              # righe "chiave|chiave nota|valore" separate da "\n" (il valore può contenere "|")
        flag("key_typo", kt_keys="\n".join(f"{k}|{kn}|{v}" for k, kn, v in kt))
    return issues, props


KEEP = ["name", "species", "genus", "taxon", "taxon:variety", "variety", "taxon:cultivar", "cultivar",
        "denotation", "leaf_type", "leaf_cycle", "circumference", "height",
        "diameter_crown", "image", "wikimedia_commons", "wikidata", "start_date",
        "ref:masaf", "masaf:criteria", "ref:rami", "website", "url", "diameter", "image:fotoshi", "ref:zapis", "sacred_tree", "religion", "multi_stemmed",
        "heritage:operator", "ref:icnf", "ref:CRFOP", "ref:gid", "sex"]

# Alberi da frutto (rendering: puntini colorati sulla chioma, issue #3): mele, noci, limoni e arance. Solo specie
# coltivate per i frutti, non i generi interi (molti Malus e Prunus sono ornamentali). Colore del frutto per specie.
# Il confronto è su genere ed epiteto (binomial): valgono anche autore, cultivar e la forma con × (Citrus × limon).
# Per ogni specie: colore e mesi in cui l'albero è carico di frutti nell'emisfero nord (issue #4; nell'emisfero sud
# index.html li sposta di 6 mesi); None = tutto l'anno. Valori indicativi per clima temperato o mediterraneo.
FRUIT_SPECIES = {"Malus domestica": ("#d63a2f", [8, 9, 10]),             # mela, rossa
                 "Juglans regia": ("#b8d63a", [7, 8, 9, 10]),            # noce, verde come il mallo (bordo in index.html)
                 "Citrus limon": ("#f5d90a", None),                      # limone, giallo: frutti quasi tutto l'anno
                 "Citrus sinensis": ("#f28a00", [11, 12, 1, 2, 3, 4]),   # arancia, arancione: matura in inverno
                 "Citrus × aurantium": ("#f28a00", [11, 12, 1, 2, 3]),   # arancia amara (alberi dei viali), arancione
                 "Olea europaea": ("#4a2850", [10, 11, 12]),             # oliva, viola scuro
                 "Diospyros kaki": ("#e8601c", [10, 11, 12])}            # cachi, arancio rossastro (anche senza foglie)
FRUIT_BY_NAME = {}   # riempito alla prima chiamata, con genere ed epiteto normalizzati (binomial)


def fruit_info(t):
    """(colore, mesi) dei frutti da disegnare, dalla specie (FRUIT_SPECIES); None se l'albero non è da frutto."""
    if not FRUIT_BY_NAME:
        FRUIT_BY_NAME.update({binomial(k): v for k, v in FRUIT_SPECIES.items()})
    sp = t.get("species") or ""
    return FRUIT_BY_NAME.get(binomial(sp)) if ";" not in sp else None


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
    return ("ref:zapis" in t or t.get("place_of_worship") == "sacred_tree" or t.get("sacred_tree", "no") != "no"
            or t.get("religion") == "shinto")


def sizes(t):
    """Diametro chioma (solo se il tag c'è) e circonferenza per il rendering. Se manca la circonferenza
    (anche da diameter), la stima da height o da diameter_crown con i rapporti della specie
    (height_circ_ratio = altezza/circonferenza, crown_circ_ratio = diametro chioma/circonferenza) o,
    se la specie non ne ha, con quelli di "_default" in species_limits.json: per l'altezza prima il rapporto
    della fascia d'altezza (band_ratio, issue #39), poi quello generale. Alcune specie hanno fasce d'altezza
    proprie, o le fasce generali prima del rapporto unico (height_ratios, issue #42). Se un rapporto dà una
    circonferenza impossibile si passa al successivo. Le stime servono solo al rendering.
    """
    lim = species_limit(t) or {}
    h, c, cd = num(t.get("height")), circ_value(t)[0], num(t.get("diameter_crown"))
    max_c, max_h = lim.get("circumference", MAX_CIRCUMFERENCE), lim.get("height", MAX_HEIGHT)
    rh = height_ratios(lim, h)
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


def bare_feature(el, natural):
    """Feature di un albero (o ceppo) che ha il solo tag natural: nessun errore, nessun consiglio, chioma di ripiego
    (ricavata dal sito, quindi senza "cr"; per il ceppo la X di default). Le stesse proprietà, nello stesso ordine, che darebbe il percorso completo di
    to_feature: lo verifica test_checks.py (feature_failures). Se un nuovo check o consiglio vale anche per un albero
    senza altri tag, quel confronto fallisce e questa funzione va aggiornata."""
    p = {"id": el["id"]}
    if ts := el.get("timestamp"):
        p["ts"] = ts[:10]
    if natural == "tree_stump":
        p["stump"], p["sr"], p["sr_est"] = 1, DEFAULT_STUMP_R, True
    p["issues"] = "|"
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


def to_feature(el, fast=True):
    """Feature GeoJSON di un albero. fast=False salta l'uscita rapida degli alberi con il solo tag natural (serve a
    test_checks.py per confrontare i due percorsi)."""
    t = el.get("tags", {})
    # uscita rapida: più di metà degli alberi ha il solo tag natural, e il risultato non dipende da altro che da id,
    # data e posizione. Non per i membri di un insieme omogeneo MASAF (mg, chioma dei monumentali); i ceppi membri
    # invece non cambiano
    if (fast and len(t) == 1 and (nat := t.get("natural")) in ("tree", "tree_stump")
            and (nat == "tree_stump" or not MASAF_GROUPS.get(el["id"]))):
        return bare_feature(el, nat)
    p = {k: t[k] for k in KEEP if k in t}
    p["id"] = el["id"]
    if ts := el.get("timestamp"):   # giorno dell'ultima modifica del nodo (AAAA-MM-GG): voce «modificati di recente» del menu errori
        p["ts"] = ts[:10]
    if (sp := t.get("species")) and (gn := GROUP_NAME.get(norm_species(sp))) and sp != gn:
        p["sp_key"] = gn   # la stessa specie con un altro nome: il sito la conta e la filtra con gli altri
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
        # membro di un insieme omogeneo MASAF (MASAF_GROUPS): le relazioni, per la scheda; vale come monumentale
        if grp := MASAF_GROUPS.get(el["id"]):
            p["mg"] = "\n".join(grp)
        crown, circ, circ_est = sizes(t)
        # chioma: "cr" solo con diameter_crown; senza, il raggio di ripiego lo ricava il sito (fallbackCrown in
        # index.html: dipende da id e denotation; i membri di un insieme omogeneo hanno quello dei monumentali)
        if crown:
            p["cr"] = round(crown / 2, 2)
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
        if t.get("xmas:feature") == "tree":   # albero di Natale: lucine e stella dall'8 dicembre al 6 gennaio (#7)
            p["xmas"] = 1
        if fi := fruit_info(t):   # fruit: colore; fm: mesi della stagione (emisfero nord), assente = tutto l'anno
            p["fruit"] = fi[0]
            if fi[1]:
                p["fm"] = ",".join(map(str, fi[1]))
        # leaf_type e leaf_cycle mancanti o non validi: stimati dalle liste (forma e colore della chioma)
        # Le palme riconosciute da is_palm hanno lt=palm anche con un leaf_type valido diverso (broadleaved,
        # needleleaved): il sito disegna lt prima di leaf_type (#55). leafless resta com'è
        if t.get("leaf_type") not in LEAF_TYPE and (est := leaf_type_estimate(t)):
            p["lt"] = est
        elif t.get("leaf_type") in ("broadleaved", "needleleaved") and is_palm(t):
            p["lt"] = "palm"
        if t.get("leaf_cycle") not in LEAF_CYCLE and (est := leaf_cycle_estimate(t)):
            p["lc"] = est
        # stime prese dal genere (specie assente dalla lista): lg = nome del genere, lgk = quali ("t" leaf_type,
        # "c" leaf_cycle), perché il consiglio della scheda nomini il genere e non la specie
        lgk = "".join(k for k, key, prop in (("t", "leaf_type", "lt"), ("c", "leaf_cycle", "lc"))
                      if prop in p and not leaf_species_ref(t, key))
        if lgk and (g := genus_ref(t)):
            p["lg"], p["lgk"] = g[0], lgk
    if wd_img := wikidata_image(t):   # per la scheda, se l'albero non ha altre immagini
        p["wd_img"] = wd_img
        if wd_img_w := wikidata_image(t, "winter"):   # veduta invernale: la scheda la mostra d'inverno al posto di wd_img
            p["wd_img_w"] = wd_img_w
    if (gl := gphotos_link(t)) and (gi := GPHOTOS.get(gl, {}).get("i")):   # foto di Google Foto (gphotos.json)
        p["gp_img"] = gi
    if (fs := FOTOSHI.get(t.get("image:fotoshi", "").strip())) and fs.get("author"):   # crediti della foto
        p["fs_author"], p["fs_license"] = fs["author"], fs.get("license", "")
    iss, props = checks(t, [el["lon"], el["lat"]], el["id"])
    # nome comune della specie e voci di Wikipedia, per la scheda; se species:wikidata è sbagliato il nome comune
    # viene dalla lista wiki
    wd_bad = {"wikidata_mismatch", "wikidata_not_taxon", "wikidata_rank", "wikidata_name_mismatch"} & set(iss)
    p.update(wikidata_extras(t, not wd_bad))
    p["issues"] = "|" + "|".join(iss) + "|" if iss else "|"
    # confronto con il tronco tipico della specie (index.html, finestra «Tronco a confronto» e tasto C): solo con
    # altezza e circonferenza mappate (anche dal diametro; per i ceppi slenderness è None) e una specie con
    # height_circ_ratio, senza ripiegare sul rapporto di _default. Gli stessi campi dei check too_slender*
    r, r0 = slenderness(t), (species_limit(t) or {}).get("height_circ_ratio")
    if r is not None and r0:
        p.update(hc_ratio=round(r, 3), hc_circ=round(circ_value(t)[0], 2), sp_ratio=r0)
    p.update(props)
    p.update(hint_props(t, el["id"]))     # consigli della scheda (non errori)
    if SLENDER_CHECKS & set(iss):          # rapporto altezza/circonferenza segnalato: niente consiglio sulla soglia MASAF
        p.pop("hint_ms", None)
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
    # chi non ha un tronco misurato (stimato, mancante o già troppo grande): conta come un punto.
    # Le celle usano lo stesso coseno per tutta la regione (quello della latitudine più lontana dall'equatore: le celle
    # non sono mai più strette di 15 m), la distanza invece il coseno della latitudine media dei due alberi. Un coseno
    # diverso per ogni albero sbaglierebbe la distanza: lon × 111320 vale milioni di metri e anche una differenza minima
    # di coseno tra due alberi vicini sposta x di metri.
    CELL = 15.0
    if not trees:
        return
    kx = math.cos(math.radians(max(abs(f["geometry"]["coordinates"][1]) for f in trees)))
    grid = collections.defaultdict(list)
    for f in trees:
        p = f["properties"]
        measured = p.get("tr") and not p.get("tr_est") and not BIG_TRUNK & set(p["issues"].strip("|").split("|"))
        lon, lat = f["geometry"]["coordinates"]
        x, y = lon * 111320 * kx, lat * 111320
        grid[(int(x // CELL), int(y // CELL))].append((p["id"], lon, lat, p["tr"] if measured else 0))
    # una coppia conta solo se almeno uno dei due ha un tronco misurato: si guardano solo le celle con un tronco
    # misurato e le otto intorno (quasi tutte le altre si saltano). L'ordine delle celle resta quello di grid
    active = {(cx + dx, cy + dy) for (cx, cy), cell in grid.items() if any(t[3] for t in cell)
              for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    for (cx, cy), cell in grid.items():
        if (cx, cy) not in active:
            continue
        near = [t for dx in (-1, 0, 1) for dy in (-1, 0, 1) for t in grid.get((cx + dx, cy + dy), [])]
        for a in cell:
            for b in near:
                if b[0] <= a[0] or not (a[3] or b[3]) or b[0] in same.get(a[0], ()):
                    continue
                d = math.hypot((a[1] - b[1]) * 111320 * math.cos(math.radians((a[2] + b[2]) / 2)), (a[2] - b[2]) * 111320)
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


def xmas_feature(el):
    """Albero di Natale (xmas:feature=tree) che non è natural=tree né tree_stump, spesso un albero tagliato o finto in
    una piazza: solo per il disegno dall'8 dicembre al 6 gennaio (#7), senza check. Chioma da diameter_crown o raggio
    di ripiego; leaf_type solo se documentato (senza, il sito lo disegna come un'aghifoglia)."""
    t = el.get("tags", {})
    p = {k: t[k] for k in KEEP if k in t}
    if p.get("leaf_type") not in ("broadleaved", "needleleaved", "palm"):
        p.pop("leaf_type", None)
    p.update({"id": el["id"], "xmas": 1, "issues": "|"})
    crown, _, _ = sizes(t)
    if crown:
        p["cr"] = round(crown / 2, 2)
    return {"type": "Feature", "properties": p,
            "geometry": {"type": "Point", "coordinates": [round(el["lon"], 7), round(el["lat"], 7)]}}


def tree_rows(elements):
    """Filari (way natural=tree_row) da disegnare con alberi finti (issue #56): {"id": id della way, "c": [[lon, lat],
    …], "lt": leaf_type, "lc": leaf_cycle}. lt e lc sono il tag se è valido, altrimenti la stima dalle liste (come per
    gli alberi; palm per le palme); mancano se non si conoscono. Un filare che ha tra i suoi nodi almeno un albero o
    un ceppo della regione (natural=tree o tree_stump) non c'è: i suoi alberi sono già mappati uno per uno. Solo per
    il disegno: niente check, conteggi o file ridotto."""
    trees = {e["id"] for e in elements if e.get("type") == "node"
             and e.get("tags", {}).get("natural") in ("tree", "tree_stump")}
    rows = []
    for el in elements:
        t = el.get("tags", {})
        if el.get("type") != "way" or t.get("natural") != "tree_row" or trees & set(el.get("nodes", [])):
            continue
        pts = [[round(g["lon"], 6), round(g["lat"], 6)] for g in el.get("geometry") or [] if g]
        pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(pts) < 2:
            continue
        row = {"id": el["id"], "c": pts}
        lt = "palm" if is_palm(t) and t.get("leaf_type") != "leafless" else t.get("leaf_type")
        if lt not in LEAF_TYPE:
            lt = leaf_type_estimate(t)
        if lt in ("broadleaved", "needleleaved", "palm"):
            row["lt"] = lt
        lc = t.get("leaf_cycle") if t.get("leaf_cycle") in LEAF_CYCLE else leaf_cycle_estimate(t)
        if lc:
            row["lc"] = lc
        rows.append(row)
    return sorted(rows, key=lambda r: r["id"])


def build(data):
    nodes = [e for e in data["elements"] if e.get("type") == "node"]
    is_tree = lambda e: e.get("tags", {}).get("natural") in ("tree", "tree_stump")
    feats = [to_feature(e) for e in nodes if is_tree(e)]
    feats.sort(key=lambda f: f["properties"]["id"])  # ordine stabile: se i dati non cambiano, il file resta identico
    neighbour_checks(feats)
    fc = {"type": "FeatureCollection", "features": feats}
    # filari senza alberi mappati tra i nodi: membro aggiuntivo "rows", come "xmas" fuori da conteggi, errori e storico
    if rows := tree_rows(data["elements"]):
        fc["rows"] = rows
    # alberi di Natale che non sono alberi OSM: in un elenco a parte ("xmas", membro aggiuntivo del GeoJSON), fuori
    # da conteggi, errori, storico e file ridotto
    if xmas := sorted((xmas_feature(e) for e in nodes if not is_tree(e)), key=lambda f: f["properties"]["id"]):
        fc["xmas"] = xmas
    return fc


def write_gz(path, raw):
    """Scrive un file .gz identico se i dati sono identici (mtime=0, nessun nome di file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        with gzip.GzipFile(filename="", mode="wb", fileobj=fh, compresslevel=9, mtime=0) as gz:
            gz.write(raw)


def minimal(fc):
    """File ridotto per il caricamento in due fasi: solo gli alberi visibili a zoom basso (con errori o
    monumentali, anche i membri di un insieme omogeneo MASAF). Il sito lo carica e lo mostra subito, mentre il file
    completo si carica in secondo piano."""
    feats = [f for f in fc["features"] if f["properties"]["issues"] != "|" or f["properties"].get("mg")
             or "natural_monument" in [x.strip() for x in str(f["properties"].get("denotation", "")).split(";")]]
    return json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# --- Primati (scheda «Alberi da record» di index.html, issue #15) ---
# data/records.json = {"<regione>": {"<primato>": {"t": [alberi], "m": altri a pari merito}}}: per ogni regione e per
# ogni primato i primi RECORD_TOP alberi (issue #48), dal valore migliore, nell'ordine di record_order, ognuno con i
# tag che servono alla riga del sito (RECORD_KEEP) e la posizione ("c"); "m" = alberi a pari merito con l'ultimo della
# lista che non ci sono entrati. Il sito li usa quando nessuna regione è caricata (tutte le regioni, o quelle di una
# nazione): unisce le liste delle regioni, le riordina e ricalcola da sé il valore da mostrare. Con una regione
# caricata il sito calcola tutto dal file della regione, con le stesse regole (recValues e recTop in index.html: le
# due versioni devono restare uguali). Le regioni non ancora aggiornate hanno la forma precedente ({"n": alberi a
# pari merito al primo posto, "t": [i primi dieci di quelli]}, senza "m"): il sito le riconosce e avvisa.
# Primati: height = più alto; trunk = tronco più spesso (circumference, altrimenti π × diameter; diameter senza unità
# è sempre in millimetri, anche sotto DIAM_MM_MIN; i valori di multi_stemmed non si usano); slender e stocky = rapporto
# altezza / diametro del tronco più alto e più basso, solo con entrambe le misure e altezza di almeno RECORD_MIN_H;
# crown = chioma più larga (diameter_crown); oldest = start_date più vecchia. Ceppi esclusi. I valori segnalati dai
# check contano lo stesso (il sito li marca con ⚠); quelli non leggibili, nulli o negativi no.
RECORDS_FILE = DATA / "records.json"
RECORD_MIN_H = 2.0     # m: sotto, niente rapporto altezza/diametro (alberelli appena piantati)
RECORD_TOP = 10        # alberi scritti nel file per ogni primato di una regione (la lista dei primi dieci)
RECORD_LOW = {"stocky", "oldest"}   # primati in cui vince il valore più basso
# tr e tr_est: il tronco disegnato sulla mappa, per il confronto dei primi tre più alti quando manca la circonferenza
# leaf_type e lt (stimato) servono alla sagoma delle chiome nel confronto dall'alto di «Chioma più larga» (issue #60)
RECORD_KEEP = ("id", "name", "species", "sp_it", "sp_en", "genus", "height", "circumference", "diameter",
               "diameter_crown", "start_date", "issues", "comma", "unit_fix", "eq_keys", "tr", "tr_est", "leaf_type", "lt")
# Frasi sull'anno per «Più vecchio» (pulsante nella riga del sito): una voce per anno, decennio o secolo, scritta a
# mano. Le chiavi sono quelle di start_date_event_key
YEAR_EVENTS = HERE / "year_events.json"

# start_date (https://wiki.openstreetmap.org/wiki/Key:start_date): AAAA, AAAA-MM, AAAA-MM-GG, decennio (1850s),
# secolo (C16, anche early/mid/late C16), anni avanti Cristo (500 BC), con davanti ~, before o after, e intervalli
# (1500..1600). Le date approssimative contano con l'anno più vecchio che indicano: ~1500 e before 1500 = 1500,
# 1850s = 1850, C16 = 1500 (early 1500, mid 1533, late 1566), 1500..1600 = 1500.
SD_PREFIX = re.compile(r"^(?:~|(?:before|after)\s+)\s*", re.I)
SD_DATE = re.compile(r"^(\d{3,4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$")
SD_DECADE = re.compile(r"^(\d{2,3}0)s$")
SD_CENTURY = re.compile(r"^(?:(early|mid|late)\s+)?C(\d{1,2})$", re.I)
SD_BC = re.compile(r"^(\d{1,5})\s*(?:BCE?|B\.C\.(?:E\.)?)$", re.I)
SD_THIRD = {"early": 0, "mid": 33, "late": 66}


def start_date_key(v):
    """Numero per ordinare start_date (anno × 10000 + mese × 100 + giorno; più basso = più vecchio); None se manca o
    non è in una delle forme lette (vedi sopra). Gli anni di una o due cifre non si leggono (start_date=50)."""
    if v is None:
        return None
    s = " ".join(str(v).split()).split("..")[0].strip()
    while m := SD_PREFIX.match(s):
        s = s[m.end():]
    if m := SD_BC.match(s):
        return -int(m.group(1)) * 10000
    if m := SD_DATE.match(s):
        mo, d = int(m.group(2) or 0), int(m.group(3) or 0)
        if mo > 12 or d > 31 or (m.group(2) and not mo) or (m.group(3) and not d):
            return None
        return int(m.group(1)) * 10000 + mo * 100 + d
    if m := SD_DECADE.match(s):
        return int(m.group(1)) * 10000
    if m := SD_CENTURY.match(s):
        if not int(m.group(2)):
            return None
        return ((int(m.group(2)) - 1) * 100 + SD_THIRD[(m.group(1) or "early").lower()]) * 10000
    return None


# early/mid/late davanti a un decennio (early 1850s): la wiki lo ammette; start_date_key non lo legge
SD_DECADE_PART = re.compile(r"^(?:early|mid|late)\s+(\d{2,3}0s)$", re.I)


def start_date_problem(v, today=None):
    """Perché start_date va segnalato: "invalid" se una parte (prima e dopo ".." in un intervallo) non è in una delle
    forme della wiki (vedi sopra; anche early/mid/late 1850s) o è una data che non esiste (2020-02-30), "future" se
    la data più vecchia che indica è dopo oggi (anche con before: un albero mappato esiste già oggi); None se va bene.
    Più severa di start_date_key, che per i primati legge anche queste forme: mese o giorno di una cifra (1990-5-3),
    più di un prefisso (~~1850, before after 1850), il secolo con la c minuscola (c16) e un intervallo che finisce
    prima di cominciare (1900..1800) sono "invalid"."""
    s = " ".join(str(v).split())
    parts = s.split("..")
    if len(parts) > 2 or not all(p.strip() for p in parts):   # anche il valore vuoto
        return "invalid"
    keys, ends = [], []
    for part in (p.strip() for p in parts):
        plain = SD_DECADE_PART.sub(r"\1", part)
        k = start_date_key(plain)
        if k is None:
            return "invalid"
        pre = SD_PREFIX.match(part)
        core = part[pre.end():] if pre else part
        if SD_PREFIX.match(core) or re.search(r"\bc\d", core):
            return "invalid"
        m = SD_DATE.match(core)
        if m and any(x is not None and len(x) != 2 for x in m.group(2, 3)):
            return "invalid"
        if m and m.group(3):
            try:
                datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return "invalid"
        keys.append(k)
        # ultimo anno che la parte indica: un decennio arriva a 9 anni dopo, un secolo a 99 (un suo terzo a 33)
        c = SD_CENTURY.match(_sd_parts(plain)[0])
        span = 9 if SD_DECADE.match(_sd_parts(plain)[0]) else (33 if c.group(1) else 99) if c else 0
        ends.append(k + span * 10000 + (1231 if span or k % 10000 == 0 else 0))
    if len(keys) == 2 and ends[1] < keys[0]:
        return "invalid"
    today = today or datetime.date.today()
    if keys[0] > today.year * 10000 + today.month * 100 + today.day:
        return "future"
    return None


def _sd_parts(v):
    """start_date senza ~, before, after e senza il secondo estremo di un intervallo, e se era una di queste forme."""
    raw = " ".join(str(v if v is not None else "").split())
    s, approx = raw.split("..")[0].strip(), ".." in raw
    while m := SD_PREFIX.match(s):
        s, approx = s[m.end():], True
    return s, approx


def start_date_precision(v):
    """Precisione di start_date, per ordinare le date dello stesso anno (sdPrec in index.html): 0 giorno, 1 mese,
    2 anno, 3 decennio, 4 secolo, 5 anni avanti Cristo; mezzo punto in più con ~, before, after o un intervallo."""
    s, approx = _sd_parts(v)
    if SD_BC.match(s):
        p = 5
    elif m := SD_DATE.match(s):
        p = 0 if m.group(3) else 1 if m.group(2) else 2
    else:
        p = 3 if SD_DECADE.match(s) else 4
    return p + (0.5 if approx else 0)


def start_date_event_key(v):
    """Chiave di year_events.json per una data (sdEvent in index.html): l'anno scritto, senza zeri davanti ("1850",
    anche per ~1850, before 1850 e 1850..1900), il decennio ("1850s"), il secolo ("C16", anche early/mid/late) o
    l'anno avanti Cristo ("500 BC"); None se la data non si legge."""
    if start_date_key(v) is None:
        return None
    s, _ = _sd_parts(v)
    if m := SD_BC.match(s):
        return f"{int(m.group(1))} BC"
    if m := SD_DATE.match(s):
        return str(int(m.group(1)))
    if m := SD_DECADE.match(s):
        return f"{int(m.group(1))}s"
    if m := SD_CENTURY.match(s):
        return f"C{int(m.group(2))}"
    return None


def year_event_decade(key):
    """Voce di ripiego per un anno senza voce in YEAR_EVENTS (sdEventDecade in index.html): il suo decennio, "1853"
    -> "1850s", "975 BC" -> "970s BC"; None per decenni e secoli."""
    if m := re.fullmatch(r"(\d+)( BC)?", key or ""):
        return f"{int(m.group(1)) // 10 * 10}s{m.group(2) or ''}"
    return None


def year_event(key, events, iso=""):
    """La voce di YEAR_EVENTS per la chiave, o in mancanza quella del suo decennio; None se mancano entrambe. Una
    voce può valere per una sola nazione o regione: la chiave ha davanti il codice ISO del paese ("JP:1159", anche
    "JP:1150s") o della regione ("IT-21:C16", il Piemonte), come in scripts/regions.json. Per un albero di quella
    regione (iso, il suo codice) vale prima la voce della regione, poi quella del paese, poi quella per tutti; prima
    per l'anno, poi per il decennio (yearEvent in index.html)."""
    iso = (iso or "").upper()
    pre = list(dict.fromkeys(p for p in (iso, iso[:2]) if p))   # regione e paese ("JP" una volta sola)
    keys = [x for k in (key, year_event_decade(key)) if k for x in [f"{p}:{k}" for p in pre] + [k]]
    return next((events[k] for k in keys if events.get(k)), None)


def record_values(p):
    """Valori di un albero per i primati, dalle proprietà della feature: {primato: valore}. Vuoto per i ceppi."""
    if p.get("stump"):
        return {}
    out = {}
    h, cd = num(p.get("height")), num(p.get("diameter_crown"))
    if "circumference" in p:           # come circ_value: con circumference, diameter non si usa
        c = num(p["circumference"])
        d = c / math.pi if c else None
    else:
        d = parse_length(p.get("diameter"), DIAM_DEFAULT)[0]
        c = d * math.pi if d else None
    if h and h > 0:
        out["height"] = h
    if c and c > 0:
        out["trunk"] = c
        if h and h >= RECORD_MIN_H:
            out["slender"] = out["stocky"] = h / d
    if cd and cd > 0:
        out["crown"] = cd
    if (y := start_date_key(p.get("start_date"))) is not None:
        out["oldest"] = y // 10000         # l'anno: le date dello stesso anno sono a pari merito
    return out


RATIO_CHECKS = ("too_slender", "too_slender_species", "too_stocky")


def record_warned(k, p):
    """Vero se il valore usato dal primato k è segnalato da un check (il ⚠ del sito: recWarn in index.html): i check
    sulla misura e sul rapporto altezza/circonferenza, e virgola, unità o «tag nel valore» nei tag usati."""
    from_d = "circumference" not in p
    trunk = "diameter" if from_d else "circumference"
    h_c = ("height", "height_species")
    t_c = ("circumference", "circumference_species", "diameter_unit" if from_d else "circ_diam_mismatch")
    tags = {"height": ("height",), "trunk": (trunk,), "slender": ("height", trunk), "stocky": ("height", trunk),
            "crown": ("diameter_crown",), "oldest": ("start_date",)}[k]
    codes = {"height": h_c + RATIO_CHECKS, "trunk": t_c + RATIO_CHECKS, "slender": h_c + t_c + RATIO_CHECKS,
             "stocky": h_c + t_c + RATIO_CHECKS, "crown": ("crown",), "oldest": ()}[k]
    iss = p.get("issues") or "|"
    if any(f"|{c}|" in iss for c in codes):
        return True

    def listed(prop, sep):
        return any(x.split("|")[0] in tags for x in str(p.get(prop) or "").split(sep))
    return (("|comma|" in iss and listed("comma", ";")) or ("|unit_format|" in iss and listed("unit_fix", "\n"))
            or ("|tag_in_value|" in iss and listed("eq_keys", ";")))


def record_round(v):
    """Valore per il confronto: uguali nelle prime 9 cifre significative = pari merito (recRound in index.html); i
    rapporti calcolati differiscono nelle ultime cifre."""
    return float(f"{v:.9g}")


def record_order(k, v, p):
    """Chiave d'ordine di un albero nella lista del primato k (recTop in index.html): il valore, dal migliore; a
    parità prima i valori segnalati da un check, per «Più vecchio» poi la data più precisa e, a parità, la più
    vecchia; infine l'id del nodo, che dà un ordine fisso."""
    old, v = k == "oldest", record_round(v)
    return (v if k in RECORD_LOW else -v, 0 if record_warned(k, p) else 1,
            start_date_precision(p.get("start_date")) if old else 0, start_date_key(p.get("start_date")) if old else 0, p["id"])


def region_records(feats):
    """Primati di una regione (vedi RECORDS_FILE): {primato: {"t": [i primi RECORD_TOP alberi], "m": altri alberi a
    pari merito con l'ultimo}}; un primato senza alberi con quella misura manca."""
    cands = collections.defaultdict(list)
    for f in feats:
        for k, v in record_values(f["properties"]).items():
            cands[k].append((record_order(k, v, f["properties"]), v, f))
    out = {}
    for k, rows in cands.items():
        rows.sort(key=lambda r: r[0])
        top, last = rows[:RECORD_TOP], record_round(rows[min(RECORD_TOP, len(rows)) - 1][1])
        out[k] = {"t": [{**{x: f["properties"][x] for x in RECORD_KEEP if x in f["properties"]},
                         "c": f["geometry"]["coordinates"]} for _, _, f in top],
                  "m": sum(1 for _, v, _ in rows[RECORD_TOP:] if record_round(v) == last)}
    return out


# «Più isolato» (isolated): gli alberi più lontani dal più vicino tra gli altri alberi (natural=tree), i boschi
# (natural=wood, landuse=forest) e i filari (natural=tree_row) di tutto il mondo, non solo della regione. Non si
# calcola dal file della regione ma con QLever (OSM di tutto il pianeta, aggiornato con i suoi tempi), in locale con
# «--isolated» (vedi QLEVER_OFF); il sito non lo ricalcola: lo prende sempre da data/records.json. Due passaggi:
# 1) candidati: i 500 alberi della regione (la sua relazione OSM) più lontani dall'albero più vicino;
# 2) a gruppi di ISO_BATCH candidati, i boschi e i filari entro la distanza dall'albero più vicino del primo del
#    gruppo (un bosco più lontano non cambia il risultato), al massimo ISO_CAP_KM.
# L'isolamento è la distanza minore tra le due; ci si ferma quando il RECORD_TOP-esimo è già più isolato del prossimo
# candidato, quindi la lista è esatta. Se i candidati finiscono prima (regioni con molti boschi: in Bassa Austria ne
# servono circa 700), se ne chiedono 2000, poi 8000 (ISO_CANDS). Gli alberi della lista senza nulla entro ISO_CAP_KM
# (isole remote) hanno una query in più con il raggio fino all'albero più vicino; se non risponde valgono ISO_CAP_KM
# ("cap": il sito scrive «oltre»). Ceppi e alberi che non sono nel file della regione (QLever può essere indietro)
# non entrano.
# Ogni albero della lista ha, oltre a RECORD_KEEP e "c": "iso" (km), "nk" (cosa c'è di più vicino: tree, wood,
# forest, tree_row), "ni" (il suo elemento OSM, "node/123", "way/…", "relation/…"), "cap" se vale ISO_CAP_KM.
# Se QLever non risponde restano i valori del calcolo precedente; l'aggiornamento della regione li lascia com'erano.
QLEVER_URL = "https://qlever.dev/api/osm-planet"
ISO_CANDS = (500, 2000, 8000)   # candidati chiesti: se non bastano, il passo dopo
ISO_BATCH = 50
ISO_CAP_KM = 50
QL_PREFIX = """PREFIX geo: <http://www.opengis.net/ont/geosparql#>
PREFIX ogc: <http://www.opengis.net/rdf#>
PREFIX osmrel: <https://www.openstreetmap.org/relation/>
PREFIX osmnode: <https://www.openstreetmap.org/node/>
PREFIX osmkey: <https://www.openstreetmap.org/wiki/Key:>
PREFIX qlss: <https://qlever.cs.uni-freiburg.de/spatialSearch/>
"""


# QLever rifiuta le richieste delle Action di GitHub (HTTP 403 dal suo nginx, dal 16/11/2026), non quelle da un PC:
# per questo «Più isolato» non si calcola nell'aggiornamento delle regioni ma in locale, con
# «python scripts/fetch_trees.py --isolated …» (isolated_main). Se arriva comunque un 403, non si interroga più
# QLever fino alla fine della corsa (QLEVER_OFF = il motivo).
QLEVER_OFF = None


def qlever(q):
    """Righe di una query a QLever ({variabile: valore}); un secondo tentativo dopo 30 s, poi solleva l'eccezione."""
    global QLEVER_OFF
    if QLEVER_OFF:
        raise RuntimeError(QLEVER_OFF)
    for attempt in range(2):
        try:
            r = requests.post(QLEVER_URL, data={"query": QL_PREFIX + q}, timeout=300,
                              headers={**HEADERS, "Accept": "application/sparql-results+json"})
            if r.status_code == 403:
                QLEVER_OFF = "HTTP 403: QLever rifiuta le richieste da qui"
                raise RuntimeError(QLEVER_OFF)
            if not r.ok:    # nel messaggio anche l'inizio della risposta, che spiega il rifiuto
                raise RuntimeError(f"HTTP {r.status_code}: {' '.join(r.text.split())[:300]}")
            return [{k: v["value"] for k, v in b.items()} for b in r.json()["results"]["bindings"]]
        except Exception:
            if attempt or QLEVER_OFF:
                raise
            time.sleep(30)


def osm_ref(iri):
    """'https://www.openstreetmap.org/node/123' -> 'node/123'."""
    return "/".join(iri.rstrip("/").split("/")[-2:])


def iso_candidates(left, n):
    """[(id del nodo, km dall'albero più vicino, suo elemento)] degli n alberi più lontani tra quelli scelti da left
    (SPARQL che lega ?tree: per una regione «osmrel:<rel> ogc:sfContains ?tree .»)."""
    rows = qlever(f"""SELECT DISTINCT ?tree ?near ?dt WHERE {{
  {left}
  ?tree osmkey:natural "tree" ; geo:hasGeometry/geo:asWKT ?g1 .
  SERVICE qlss: {{
    _:c qlss:algorithm qlss:s2 ; qlss:left ?g1 ; qlss:right ?g2 ; qlss:numNearestNeighbors 2 ;
        qlss:bindDistance ?dt ; qlss:distanceUnit <http://qudt.org/vocab/unit/KiloM> ; qlss:payload ?near .
    {{ ?near osmkey:natural "tree" ; geo:hasGeometry/geo:asWKT ?g2 . }}
  }}
  FILTER (?tree != ?near)
}} ORDER BY DESC(?dt) LIMIT {n}""")
    return [(int(osm_ref(r["tree"]).split("/")[1]), float(r["dt"]), osm_ref(r["near"]))
            for r in rows if "/node/" in r["tree"]]


def iso_woods(ids, km):
    """{id del nodo: (km, elemento, tipo)}: il bosco, la foresta o il filare più vicino entro km, per i nodi dati."""
    rows = qlever(f"""SELECT ?tree ?wood ?kind ?dw WHERE {{
  VALUES ?tree {{ {" ".join(f"osmnode:{i}" for i in ids)} }}
  ?tree geo:hasGeometry/geo:asWKT ?g1 .
  SERVICE qlss: {{
    _:w qlss:algorithm qlss:libspatialjoin ; qlss:left ?g1 ; qlss:right ?gw ; qlss:joinType qlss:within-dist ;
        qlss:maxDistance {max(1, math.ceil(km * 1000))} ; qlss:bindDistance ?dw ;
        qlss:distanceUnit <http://qudt.org/vocab/unit/M> ; qlss:payload ?wood, ?kind .
    {{ {{ ?wood osmkey:natural "wood" BIND("wood" AS ?kind) }} UNION {{ ?wood osmkey:landuse "forest" BIND("forest" AS ?kind) }}
      UNION {{ ?wood osmkey:natural "tree_row" BIND("tree_row" AS ?kind) }}
      ?wood geo:hasGeometry/geo:asWKT ?gw . }}
  }}
}}""")
    out = {}
    for r in rows:
        i, d = int(osm_ref(r["tree"]).split("/")[1]), float(r["dw"]) / 1000
        if i not in out or d < out[i][0]:
            out[i] = (d, osm_ref(r["wood"]), r["kind"])
    return out


def isolated_top(left, ids, label, top=RECORD_TOP):
    """I top alberi più isolati tra quelli scelti da left (SPARQL che lega ?tree) e presenti in ids, dal più isolato:
    [(id, km, elemento più vicino, tipo, cap)]; None se QLever non risponde. Vedi «Più isolato» sopra."""
    done, far = {}, {}      # id -> (km, elemento, tipo, cap); far: id -> (km dall'albero, albero) se oltre il tetto
    if QLEVER_OFF:          # già rifiutato in questa corsa: l'avviso c'è già stato
        print(f"{label}: «Più isolato» non calcolato ({QLEVER_OFF}), resta quello precedente")
        return None
    try:
        for n in ISO_CANDS:  # se i candidati finiscono prima che la lista sia sicura, se ne chiedono di più
            raw = iso_candidates(left, n)
            cands = [c for c in raw if c[0] in ids and c[0] not in done]
            exact = False
            for i in range(0, len(cands), ISO_BATCH):
                batch = cands[i:i + ISO_BATCH]
                best = sorted((v[0] for v in done.values()), reverse=True)
                if len(best) >= top and best[top - 1] >= batch[0][1]:
                    exact = True
                    break
                lim = min(batch[0][1], ISO_CAP_KM)
                woods = iso_woods([c[0] for c in batch], lim)
                for tid, dt, near in batch:
                    w = woods.get(tid)
                    if w and w[0] < dt:
                        done[tid] = (w[0], w[1], w[2], False)
                    elif dt <= lim:
                        done[tid] = (dt, near, "tree", False)
                    else:   # niente entro ISO_CAP_KM e albero più lontano: per ora «oltre ISO_CAP_KM»
                        done[tid] = (ISO_CAP_KM, None, None, True)
                        far[tid] = (dt, near)
            if exact or len(raw) < n:   # lista sicura, o esaminati tutti gli alberi
                break
        else:
            warn(f"{label}: «Più isolato» calcolato solo sui primi {ISO_CANDS[-1]} candidati")
    except Exception as e:
        warn(f"{label}: QLever non risponde ({str(e)[:350]}), «Più isolato» resta quello precedente"
             + (". Va calcolato in locale: python scripts/fetch_trees.py --isolated <regioni>" if QLEVER_OFF else ""))
        return None
    rows = sorted(done.items(), key=lambda x: (-record_round(x[1][0]), x[0]))[:top]
    # alberi della lista oltre il tetto (isole remote): una query sola con il raggio fino al più lontano, per la
    # distanza vera; lenta (2–3 minuti), quindi solo per questi. Se non risponde restano «oltre ISO_CAP_KM»
    capped = [tid for tid, v in rows if v[3]]
    if capped:
        try:
            woods = iso_woods(capped, max(far[t][0] for t in capped))
            for tid in capped:
                dt, near = far[tid]
                w = woods.get(tid)
                done[tid] = (w[0], w[1], w[2], False) if w and w[0] < dt else (dt, near, "tree", False)
            rows = sorted(done.items(), key=lambda x: (-record_round(x[1][0]), x[0]))[:top]
        except Exception as e:
            warn(f"{label}: QLever non risponde per gli alberi oltre {ISO_CAP_KM} km ({str(e)[:150]})")
    return [(tid, *v) for tid, v in rows]


def region_isolated(region, feats):
    """Primato «Più isolato» della regione, nella forma di region_records ({"t": [...], "m": 0}); None se QLever non
    risponde o la regione non ha una relazione."""
    if not region.get("rel"):
        return None
    by_id = {f["properties"]["id"]: f for f in feats
             if not f["properties"].get("stump") and not f["properties"].get("xmas")}
    rows = isolated_top(f"osmrel:{region['rel']} ogc:sfContains ?tree .", by_id, region["slug"])
    if rows is None:
        return None
    out = []
    for tid, km, ref, kind, cap in rows:
        f = by_id[tid]
        t = {**{x: f["properties"][x] for x in RECORD_KEEP if x in f["properties"]},
             "c": f["geometry"]["coordinates"], "iso": round(km, 3)}
        if cap:
            t["cap"] = 1
        else:
            t["nk"], t["ni"] = kind, ref
        out.append(t)
    # "d" = giorno del calcolo, per «--isolated oldest»
    return {"t": out, "m": 0, "d": time.strftime("%Y-%m-%d", time.gmtime())} if out else None


def save_records(slug, feats, isolated=None):
    """Scrive i primati della regione in RECORDS_FILE; isolated = «Più isolato» (region_isolated): se è None resta
    quello già nel file."""
    rec = load_json(RECORDS_FILE)
    prev = (rec.get(slug) or {}).get("isolated")
    rec[slug] = region_records(feats)
    if isolated or prev:
        rec[slug]["isolated"] = isolated or prev
    write_records(rec)


def isolated_main(names):
    """«python scripts/fetch_trees.py --isolated <regioni>»: solo «Più isolato», dai file delle regioni già scaricati
    (data/regions), scritto in RECORDS_FILE senza toccare gli altri primati. Serve perché QLever rifiuta le richieste
    delle Action (vedi QLEVER_OFF): si lancia dal proprio PC e si pubblica il file. Regioni come per main: nomi,
    gruppi, "all"; "oldest" = la regione con «Più isolato» calcolato meno di recente (prima quelle che non l'hanno)."""
    have = [r for r in REGIONS if (OUT_DIR / f"{r['slug']}.geojson.gz").exists()]
    rec = load_json(RECORDS_FILE)
    when = lambda r: ((rec.get(r["slug"]) or {}).get("isolated") or {}).get("d", "")
    groups = collections.defaultdict(list)
    for r in have:
        if r.get("group"):
            groups[r["group"].lower().replace(" ", "-")].append(r)
    by_slug = {r["slug"]: r for r in have}
    todo = []
    for x in names or ["oldest"]:
        if x == "all":
            todo += have
        elif x == "oldest":
            r = min(have, key=when)
            print(f"oldest: {r['slug']} («Più isolato» calcolato: {when(r) or 'mai'})")
            todo.append(r)
        elif x in groups or x in by_slug:
            todo += groups.get(x) or [by_slug[x]]
        else:
            sys.exit(f"Regione sconosciuta o non ancora scaricata: {x}")
    todo = list({r["slug"]: r for r in todo}.values())
    for i, region in enumerate(todo, 1):
        feats = json.loads(gzip.decompress((OUT_DIR / f"{region['slug']}.geojson.gz").read_bytes()))["features"]
        t = time.time()
        iso = region_isolated(region, feats)
        if QLEVER_OFF:
            sys.exit(f"QLever rifiuta le richieste: {QLEVER_OFF}")
        if iso:
            rec = load_json(RECORDS_FILE)
            rec.setdefault(region["slug"], {})["isolated"] = iso
            write_records(rec)
        print(f"{region['slug']} ({i}/{len(todo)}): {'fatto' if iso else 'non calcolato'} in {time.time() - t:.0f} s"
              + (f", il primo a {iso['t'][0]['iso']} km" if iso else ""), flush=True)


def write_records(rec):
    RECORDS_FILE.write_text("{\n" + ",\n".join(
        f"{json.dumps(k)}: {json.dumps(v, separators=(',', ':'), ensure_ascii=False, sort_keys=True)}"
        for k, v in sorted(rec.items())) + "\n}\n", encoding="utf-8")


def missing_year_events():
    """Chiavi (anni, decenni, secoli) delle date nelle liste di «Più vecchio» di tutte le regioni di RECORDS_FILE che
    non hanno una voce in YEAR_EVENTS, né per l'anno né per il suo decennio, dalla più vecchia. I primi dieci di una nazione o di tutte le regioni sono
    sempre tra i primi dieci delle regioni, quindi l'elenco copre tutti i livelli del sito. Valore: [(chiave, regioni)],
    le regioni (slug) in cui la data è tra i primi dieci, perché le regioni aggiornate nell'Action non sono le sole."""
    events = load_json(YEAR_EVENTS)
    iso = {r["slug"]: r.get("iso", "") for r in REGIONS}   # voci di una sola nazione o regione (year_event)
    found, where = {}, collections.defaultdict(set)
    for slug, rec in load_json(RECORDS_FILE).items():
        for t in (rec.get("oldest") or {}).get("t", []):
            key = start_date_event_key(t.get("start_date"))
            if key and not year_event(key, events, iso.get(slug)):   # un anno con la voce del decennio non manca
                found[key] = min(found.get(key, math.inf), start_date_key(t["start_date"]))
                where[key].add(slug)
    return [(k, sorted(where[k])) for k in sorted(found, key=lambda x: (found[x], x))]


def report_year_events():
    """Riepilogo dell'Action (e log): le voci che mancano in year_events.json, da aggiungere a mano, ognuna con le
    regioni da cui viene."""
    missing = missing_year_events()
    if not missing:
        return
    text = ", ".join(f"{k} ({', '.join(r)})" for k, r in missing)
    print(f"Anni senza una voce in scripts/year_events.json ({len(missing)}): {text}")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"### Years without an entry in `scripts/year_events.json` ({len(missing)})\n\n"
                    "They appear in the top ten of \"Oldest\" (Record trees) of at least one region, of any region "
                    "and not only the ones updated by this run, and have no \"what happened that year\" sentence yet. "
                    "The regions are in brackets.\n\n```\n" + text + "\n```\n")


def update_region(region, regions_index):
    global IN_ITALY, COUNTRY, REGION_ISO, MASAF_GROUPS
    slug = region["slug"]
    IN_ITALY = region.get("iso", "").startswith("IT")   # consiglio hint_ms (soglie MASAF)
    COUNTRY = region.get("iso", "")[:2]                 # check register_country
    REGION_ISO = region.get("iso", "")                  # nomi dei generi esclusi in una regione (genus_name_here)
    MASAF_GROUPS = {}                                   # i membri della regione precedente non valgono qui
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
    nodes = sum(1 for el in data["elements"] if el.get("type") == "node"     # solo gli alberi, non gli alberi di
                and el.get("tags", {}).get("natural") in ("tree", "tree_stump"))   # Natale che non sono natural=tree
    prev = regions_index.get(slug, {}).get("count")
    if path.exists() and prev and nodes < prev * MIN_KEEP_RATIO:
        warn(f"{slug}: solo {nodes} alberi contro {prev} dell'ultimo aggiornamento: risposta di Overpass probabilmente "
             f"incompleta, mantengo i dati precedenti")
        return True
    # Un server può anche rispondere con dati OSM più vecchi di quelli dell'ultimo aggiornamento (vedi OSM_BASE): i
    # dati precedenti non si toccano
    base, prev_base = osm_base(data), regions_index.get(slug, {}).get("osm_base", "")
    if path.exists() and base and prev_base and base < prev_base:
        warn(f"{slug}: i dati di Overpass sono del {base}, quelli dell'ultimo aggiornamento del {prev_base}: "
             f"mantengo i dati precedenti")
        return True
    newest = data["osm3s"].get("newest") or base if base else ""
    if base and base_seconds(newest) - base_seconds(base) > BASE_SPREAD_MAX:
        warn(f"{slug}: i tile della regione hanno dati OSM di date diverse, dal {base} al {newest}")
    els = [el for el in data["elements"] if el.get("type") == "node"]   # senza i filari: le cache servono agli alberi
    update_wikidata_cache(els)   # prima dei check: servono ai check su Wikidata (taxon e distanza)
    update_url_cache(els)        # e al check sui link rotti
    update_fotoshi_cache(els)    # autore e licenza delle foto di Fotoshi, per la scheda
    update_gphotos_cache(els)    # indirizzo delle foto di Google Foto, per la scheda
    MASAF_GROUPS = load_masaf_groups(region, path) if IN_ITALY else {}   # insiemi omogenei MASAF, solo in Italia
    fc = build(data)
    raw = json.dumps(fc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()
    errors = sum(1 for f in fc["features"] if f["properties"]["issues"] != "|")  # alberi con almeno un errore
    min_path = OUT_DIR / f"{slug}.min.geojson.gz"
    # primati della regione, anche quando i dati non cambiano; «Più isolato» resta quello già nel file (si calcola in
    # locale con --isolated, vedi QLEVER_OFF)
    save_records(slug, fc["features"])
    if regions_index.get(slug, {}).get("sha1") == digest and path.exists():
        regions_index[slug]["errors"] = errors  # per gli indici scritti prima che esistesse il conteggio
        regions_index[slug].pop("diam", None)   # campo non più usato (era per fixDiameter, tolto da index.html)
        if base:
            regions_index[slug]["osm_base"] = base
        if not min_path.exists():               # regioni scaricate prima che esistesse il file ridotto
            write_gz(min_path, minimal(fc))
        record_history(slug, len(fc["features"]), errors)
        print(f"{slug}: nessuna modifica ({len(fc['features'])} alberi)")
        return True
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_gz(path, raw)
    write_gz(min_path, minimal(fc))
    regions_index[slug] = {"name": region["name"], "count": len(fc["features"]), "errors": errors,
                           "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "sha1": digest,
                           **({"osm_base": base} if base else {})}
    record_history(slug, len(fc["features"]), errors)
    print(f"{slug}: {len(fc['features'])} alberi -> {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return True


# Storico degli errori per il grafico del sito: data/history.json = {"<regione>": [["AAAA-MM-GG", alberi, alberi
# con errori], …]}, in ordine di data. Una riga al giorno: se la regione si aggiorna più volte nello stesso giorno
# resta l'ultimo valore, così l'ultima riga coincide sempre con data/index.json (anche quando cambiano solo i check).
HISTORY = DATA / "history.json"


def record_history(slug, count, errors):
    hist = load_json(HISTORY)
    rows = hist.setdefault(slug, [])
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if rows and rows[-1][0] == today:
        rows[-1] = [today, count, errors]
    else:
        rows.append([today, count, errors])
    HISTORY.write_text("{\n" + ",\n".join(f"{json.dumps(k)}: {json.dumps(v, separators=(',', ':'))}"
                                          for k, v in sorted(hist.items())) + "\n}\n", encoding="utf-8")


def save_index(regions_index):
    DATA.mkdir(parents=True, exist_ok=True)
    INDEX.write_text(json.dumps({"regions": regions_index}, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                     encoding="utf-8")


def last_run(slug, regions_index, hist):
    """Data (AAAA-MM-GG) dell'ultimo scaricamento della regione, riuscito o no: l'ultima riga di data/history.json
    (scritta anche quando i dati non cambiano) o il campo "tried" di data/index.json (scritto anche quando lo
    scaricamento non va a buon fine). "" se la regione non è mai stata scaricata."""
    rows = hist.get(slug) or []
    return max(rows[-1][0] if rows else "", regions_index.get(slug, {}).get("tried", ""))


def oldest(groups, regions_index):
    """Per "oldest": il paese (tutte le sue regioni) o la regione senza gruppo scaricati meno di recente. La data di
    un paese è quella della sua regione scaricata meno di recente; a parità di data vale l'ordine di regions.json."""
    hist = load_json(HISTORY)
    units = list(groups.values()) + [[r] for r in REGIONS if not r.get("group")]
    dates = [min(last_run(r["slug"], regions_index, hist) for r in u) for u in units]
    i = min(range(len(units)), key=lambda k: dates[k])
    print(f"oldest: {', '.join(r['slug'] for r in units[i])} (ultimo scaricamento: {dates[i] or 'mai'})", flush=True)
    return units[i]


def main(argv):
    # regioni separate da spazi o virgole; "all" = tutte; il nome di un gruppo (es. "italia") = tutte le sue regioni;
    # "oldest" = il paese o la regione senza gruppo scaricati meno di recente
    names = [x.lower() for a in argv for x in re.split(r"[\s,]+", a) if x] or ["all"]
    by_slug = {r["slug"]: r for r in REGIONS}
    groups = {}
    for r in REGIONS:
        if r.get("group"):
            groups.setdefault(r["group"].lower().replace(" ", "-"), []).append(r)   # "Regno Unito" -> regno-unito
    regions_index = json.loads(INDEX.read_text(encoding="utf-8")).get("regions", {}) if INDEX.exists() else {}
    if "all" in names:
        todo = REGIONS
    else:
        unknown = [x for x in names if x not in by_slug and x not in groups and x != "oldest"]
        if unknown:
            sys.exit(f"Regione sconosciuta: {', '.join(unknown)}. "
                     f"Disponibili: all, oldest, {', '.join([*groups, *by_slug])}")
        todo = list({r["slug"]: r for x in names                                         # senza doppioni
                     for r in (oldest(groups, regions_index) if x == "oldest" else groups.get(x) or [by_slug[x]])}.values())
    ok = True
    for i, region in enumerate(todo, 1):
        print(f"== {region['name']} ({i}/{len(todo)})", flush=True)
        ok &= update_region(region, regions_index)
        if region["slug"] in regions_index:   # data del tentativo, anche se non è andato a buon fine (per "oldest")
            regions_index[region["slug"]]["tried"] = time.strftime("%Y-%m-%d", time.gmtime())
        save_index(regions_index)
        time.sleep(5)
    report_year_events()   # anni di «Più vecchio» senza una frase: nel riepilogo dell'Action
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--isolated"]:
        isolated_main([x.lower() for a in sys.argv[2:] for x in re.split(r"[\s,]+", a) if x])
    else:
        main(sys.argv[1:])
