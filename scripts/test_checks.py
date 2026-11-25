#!/usr/bin/env python3
"""Test dei check QA: una tabella di alberi finti (tag -> codici attesi) passata a checks() di fetch_trees.py.

Uso: python scripts/test_checks.py        (esce con 1 se un caso non torna; nessuna rete)

Serve a scoprire subito se una modifica cambia per sbaglio il risultato di un check. Le Action lo eseguono a ogni
push che tocca scripts/ («Test checks») e prima di scaricare i dati («Update tree data»).

I casi non dipendono dai file di riferimento (whitelist.json, species_limits.json, species_list.json, genus_list.json,
denotation_values.json, masaf_list.json) né dalle cache in data/: prima di eseguirli, le tabelle che fetch_trees.py
ricava da quei file sono sostituite dalle piccole tabelle finte scritte qui sotto (FIXTURES). I QID Q91… e Q92… sono
inventati. Le soglie fisse dei check (MAX_HEIGHT, SLENDER_MAX, …) sono invece quelle vere: i casi usano valori lontani
dalle soglie, ma se una soglia cambia molto vanno riguardati.

HINT_CASES prova i consigli di hint_props (hint_ms, hint_mf, hint_wg, hint_hx, hint_nl…); GROUP_ELEMENTS e feature_failures la lettura delle relazioni con ref:masaf
(insiemi omogenei, issue #9) e i loro membri nelle feature.

Quando si aggiunge o si cambia un check: aggiornare CASES. Un codice che checks() può segnalare e che nessun caso
si aspetta fa fallire il test.
"""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_trees as ft   # noqa: E402

POS = [12.0, 42.0]          # posizione degli alberi finti ([lon, lat])
FAR = [13.0, 43.0]          # un punto a più di 100 km
COUNTRY = "IT"              # nazione della regione degli alberi finti, se il caso non ne indica un'altra
REMOVED = "001/A001/RM/01"  # codice MASAF finto, rimosso dall'elenco
LISTED = "002/A002/RM/02"   # codice MASAF finto, iscritto (albero singolo)
GROUP_CODE = "004/A004/RM/04"   # codice MASAF finto, iscritto come filare singolo
MEMBER = 9001               # id di un nodo membro di un insieme omogeneo MASAF (FIXTURES["MASAF_GROUPS"])

_SPECIES = {
    "Quercus robur": {"wikidata": "Q920001", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
    "Pinus pinea": {"wikidata": "Q920002", "leaf_type": "needleleaved", "leaf_cycle": "evergreen"},
    # due specie vicine: "Prunus serratula" è a due lettere dalla prima e ha le stesse lettere della seconda
    "Prunus serrula": {"wikidata": "Q920008", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
    "Prunus serrulata": {"wikidata": "Q920009", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
    # "Quercus ruber": a una lettera da Quercus suber, a due da Quercus robur (stessa iniziale dell'epiteto)
    "Quercus suber": {"wikidata": "Q920011", "leaf_type": "broadleaved", "leaf_cycle": "evergreen"},
    # "Pirus piraster": a due lettere da Pinus pinaster e da Pyrus pyraster
    "Pinus pinaster": {"wikidata": "Q920012", "leaf_type": "needleleaved", "leaf_cycle": "evergreen"},
    "Pyrus pyraster": {"wikidata": "Q920013", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
    # un ibrido, con il segno tra genere ed epiteto (species_hybrid_end)
    "Platanus × acerifolia": {"wikidata": "Q920014", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
}
_GENERA = {
    "Quercus": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous", "evergreen"]},
    "Pinus": {"leaf_type": "needleleaved", "leaf_cycle": ["evergreen"]},
    "Tilia": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous"]},
    "Magnolia": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous", "evergreen"]},
    "Yucca": {"leaf_type": "broadleaved", "leaf_cycle": ["evergreen"]},
    "Cycas": {"leaf_type": "needleleaved", "leaf_cycle": ["evergreen"]},
    "Trachycarpus": {"leaf_type": "broadleaved", "leaf_cycle": ["evergreen"]},   # palma: nella lista è broadleaved
}
_DEN = {"natural_monument": 50000, "urban": 100000, "avenue": 50000, "park": 20000, "rarevalue": 3}

# nome della variabile di fetch_trees.py -> valore finto
FIXTURES = {
    "SPECIES_LIMITS": {"quercus robur": {"circumference": 15.0, "height": 45.0, "height_circ_ratio": 10.0}},
    # con l'ibrido della lista scritto senza × (HYBRID_ALIAS), come fa fetch_trees.py
    "SPECIES_REF": {**{ft.norm_species(k): v for k, v in _SPECIES.items()},
                    "platanus acerifolia": _SPECIES["Platanus × acerifolia"]},
    "HYBRID_ALIAS": {"platanus acerifolia": "Platanus × acerifolia"},
    "QID_TO_SPECIES": {v["wikidata"]: k for k, v in _SPECIES.items()},
    "SPECIES_BIN": {ft.binomial(k): k for k in _SPECIES},
    "SPECIES_REF_BIN": {ft.binomial(k): (k, v) for k, v in _SPECIES.items()},
    "GENUS_REF": {k.casefold(): (k, v) for k, v in _GENERA.items()},
    "KNOWN_GENERA": {k.casefold() for k in _GENERA},
    "LIST_EPITHETS": {ft.binomial(k).split()[1] for k in _SPECIES},   # senza il segno di ibrido
    # eccezioni (come in whitelist.json)
    "WD_WHITELIST": {"quercus robur": {"Q920009"}},
    "TYPO_WHITELIST": {"quercus robar"},
    "HERITAGE_NOT_NATURAL": {"Q43874039"},       # monumento archeologico
    "SYNONYMS": {"quercus pedunculata": "Quercus robur"},
    "GROUP_NAME": {"quercus pedunculata": "Quercus robur", "quercus robur": "Quercus robur",
                   "platanus × acerifolia": "Platanus × acerifolia", "platanus acerifolia": "Platanus × acerifolia"},
    "SMALL_MON_WHITELIST": {4242},
    "GENUS_NAMES": {("fr", "tilleul"): "Tilia", ("it", "quercia"): "Quercus", ("en", "plane"): "Platanus",
                    ("pl", "świerk"): "Picea"},
    "GENUS_OF_NAMES": {"horse chestnut": {"Aesculus"}},
    "GENUS_NAMES_EXCEPT": {("en", "plane"): {"GB-SCT"}},
    "SPECIES_NAMES": {("pl", "klon zwyczajny"): "Acer platanoides", ("it", "bagolaro"): "Celtis australis",
                      ("en", "english oak"): "Quercus robur", ("fr", "mirabellier"): "Prunus domestica"},
    "SPECIES_NAME_FORM": {("pl", "klon zwyczajny"): "Klon zwyczajny", ("it", "bagolaro"): "Bagolaro",
                          ("en", "english oak"): "English Oak", ("fr", "mirabellier"): "Mirabellier"},
    "SPECIES_NAME_SSP": {("fr", "mirabellier"): "Prunus domestica subsp. syriaca"},
    "SINGLE_SPECIES": {},
    "NOT_TREES": {"Agave americana": "Q920007"},
    "LEAF_TYPE_GENERA": {"cycas": {"needleleaved", "broadleaved", "palm"}},
    "DEN_COUNTS": _DEN,
    "DEN_COMMON": sorted((v for v, n in _DEN.items() if n >= ft.DEN_COMMON_MIN), key=len, reverse=True),
    "MASAF_LIST": {ft.masaf_code(REMOVED): ["morte naturale", "D.M. 1/2020"], ft.masaf_code(LISTED): None,
                   ft.masaf_code(GROUP_CODE): None},
    # tipo degli alberi iscritti (masaf_shared, masaf_group_single: masaf_code_failures)
    "MASAF_TYPE_OF": {ft.masaf_code(LISTED): "albero singolo", ft.masaf_code(GROUP_CODE): "filare singolo"},
    # posizione e comune degli alberi iscritti (hint_mf, HINT_CASES): LISTED dove sono gli alberi finti, GROUP_CODE lontano
    "MASAF_POS_OF": {ft.masaf_code(LISTED): (*POS, "Roma"), ft.masaf_code(GROUP_CODE): (*FAR, "Rieti")},
    # membri di insiemi omogenei MASAF (relazioni con ref:masaf): id del nodo -> righe "mg" (come masaf_groups)
    "MASAF_GROUPS": {MEMBER: [f"17460265|0|{LISTED}|a1|Glicini"]},
    # soglie MASAF di circonferenza (cm) per il consiglio hint_ms (HINT_CASES)
    "MASAF_MIN_CIRC": {ft.binomial("Quercus robur"): 400},
    # elementi di wikidata=* (i singoli alberi)
    "WD_ITEMS": {
        "Q910001": {"taxon": False, "coords": [POS]},                 # vicino
        "Q910002": {"taxon": False, "coords": [FAR]},                 # lontano
        "Q910003": {"taxon": False, "taxa": ["Q920002"]},             # individuo di Pinus pinea (P10241)
        "Q910004": {"taxon": False, "heritage": [["monumento naturale", "natural monument"]]},   # tutela (P1435)
        "Q910005": {"taxon": False, "mt": "12345"},                   # id di monumentaltrees.com (P9704)
        "Q910006": {"taxon": False, "image": "Far_tree.jpg"},         # foto (P18), senza coordinate
        "Q910007": {"taxon": False, "taxa": ["Q920001"]},             # individuo di Quercus robur (P10241)
        "Q910008": {"taxon": False, "taxa": ["Q920003"]},             # individuo del genere Quercus
        # solo una tutela culturale (HERITAGE_NOT_NATURAL); una culturale e una naturale
        "Q910009": {"taxon": False, "heritage": [["monumento archeologico", "archaeological monument", "Q43874039"]]},
        "Q910010": {"taxon": False, "heritage": [["monumento archeologico", "archaeological monument", "Q43874039"],
                                                 ["monumento naturale", "natural monument", "Q23790"]]},
    },
    # elementi di species:wikidata e genus:wikidata
    "WD_TAXA": {
        "Q920001": {"taxon": True, "names": ["Quercus robur"], "ranks": ["Q7432"]},
        "Q920002": {"taxon": True, "names": ["Pinus pinea"], "ranks": ["Q7432"]},
        "Q920003": {"taxon": True, "names": ["Quercus"], "ranks": ["Q34740"]},
        "Q920004": {"taxon": False},                                  # non è un taxon
        "Q920005": {"taxon": True, "names": ["Pinus"], "ranks": ["Q34740"]},
        "Q920006": {"taxon": True, "names": ["Acer campestre"], "ranks": ["Q7432"]},   # specie non in lista
        "Q920007": {"taxon": True, "names": ["Agave americana"], "ranks": ["Q7432"]},  # non è un albero (NOT_TREES)
        "Q920010": {"taxon": True, "names": ["Pinus pinea var. fragilis"], "ranks": ["Q767728"]},   # varietà (sp_var_ok)
    },
    # voci di Wikipedia -> QID (wikipedia=* e species:wikipedia)
    "WP_QIDS": {"it:Quercus robur": "Q920001", "it:Quercia delle streghe": "Q910001", "it:Quercus": "Q920003"},
    "URL_STATUS": {
        "https://example.org/gone.jpg": {"s": "404", "d": "2026-01-01"},
        "https://example.org/ok.jpg": {"s": "200", "d": "2026-01-01"},
        "commons:File:Missing tree.jpg": {"s": "missing", "d": "2026-01-01"},
        "geo:File:Far tree.jpg": {"c": [FAR[1], FAR[0], "camera"]},   # [lat, lon, tipo]
        "geo:File:Near tree.jpg": {"c": [POS[1], POS[0], "object"]},
    },
}

OAK = {"natural": "tree", "species": "Quercus robur"}
MON = {"natural": "tree", "denotation": "natural_monument"}

# (nome del caso, tag, codici attesi[, id del nodo[, nazione della regione]]). L'ordine dei codici non conta; la
# nazione (codice ISO, "" = non nota) è COUNTRY se il caso non la indica.
CASES = [
    # --- alberi corretti ---
    ("albero corretto, con tutti i tag", {**OAK, "genus": "Quercus", "leaf_type": "broadleaved",
                                          "leaf_cycle": "deciduous", "height": "20", "circumference": "2",
                                          "diameter_crown": "10", "species:wikidata": "Q920001",
                                          "denotation": "urban", "wikidata": "Q910001",
                                          "image": "https://example.org/ok.jpg"}, []),
    ("albero corretto, solo natural=tree", {"natural": "tree"}, []),
    ("misure con unità scritte bene", {"natural": "tree", "height": "20 m", "circumference": "200 cm"}, []),
    ("piedi e pollici", {"natural": "tree", "height": "65'7\""}, []),
    ("genere ibrido", {"natural": "tree", "genus": "× Sycoparrotia", "species": "× Sycoparrotia semidecidua"}, []),

    # --- misure oltre i limiti ---
    ("circumference", {"natural": "tree", "circumference": "50"}, ["circumference"]),
    ("circumference da diameter", {"natural": "tree", "diameter": "20000"}, ["circumference"]),
    ("circumference da diameter, se circumference non è leggibile",
     {"natural": "tree", "circumference": "big", "diameter": "20000"}, ["circumference", "number_invalid"]),
    ("height", {"natural": "tree", "height": "150"}, ["height"]),
    ("crown", {"natural": "tree", "diameter_crown": "80"}, ["crown"]),
    ("circumference_species", {**OAK, "circumference": "20"}, ["circumference_species"]),
    ("circumference_species per un sinonimo", {"natural": "tree", "species": "Quercus pedunculata",
                                                "circumference": "20"}, ["circumference_species"]),
    ("height_species", {**OAK, "height": "60"}, ["height_species"]),
    ("height_species con cultivar", {"natural": "tree", "species": "Quercus robur 'Fastigiata'", "height": "60"},
     ["height_species"]),
    ("circumference_species con cultivar senza virgolette",
     {"natural": "tree", "species": "Quercus robur Fastigiata", "circumference": "20"}, ["circumference_species"]),
    ("too_slender_species: non per una cultivar", {"natural": "tree", "species": "Quercus robur 'Fastigiata'",
                                                   "height": "20", "circumference": "0.4"}, []),
    ("height_species: non per un ibrido con due genitori",
     {"natural": "tree", "species": "Quercus robur × petraea", "height": "60"}, []),
    ("dipendenza: con circumference niente circumference_species", {**OAK, "circumference": "50"}, ["circumference"]),
    ("dipendenza: con height niente height_species", {**OAK, "height": "150"}, ["height"]),

    # --- forma dei numeri ---
    ("comma", {"natural": "tree", "height": "12,5"}, ["comma"]),
    ("comma: con l'unità", {"natural": "tree", "circumference": "2,9 m"}, ["comma"]),
    ("comma: non per un elenco", {"natural": "tree", "height": "10, 12"}, ["number_invalid"]),
    ("comma: non per più virgole", {"natural": "tree", "diameter": "66,60,51"}, ["number_invalid"]),
    ("comma: tre cifre dopo la virgola (150 o 150000), il valore non si usa", {"natural": "tree", "height": "150,000"},
     ["comma"]),                # non height: 150 m non si legge
    ("comma: tre cifre dopo la virgola, con l'unità", {"natural": "tree", "height": "150,000 m"}, ["comma"]),
    ("comma: 0,500 non è ambiguo", {"natural": "tree", "circumference": "50,0", "height": "0,500"},
     ["circumference", "comma"]),
    ("number_invalid: tre cifre dopo la virgola, negativo", {"natural": "tree", "height": "-1,200"}, ["number_invalid"]),
    ("diameter_unit", {"natural": "tree", "diameter": "0.5"}, ["diameter_unit"]),
    ("unit_format: parola intera", {"natural": "tree", "height": "8 metres"}, ["unit_format"]),
    ("unit_format: senza spazio", {"natural": "tree", "circumference": "150cm"}, ["unit_format"]),
    ("numero senza lo zero davanti al punto: non è un errore", {"natural": "tree", "circumference": ".75"}, []),
    ("numero senza lo zero, con l'unità: non è un errore", {"natural": "tree", "height": ".5 m"}, []),
    ("unit_format: senza lo zero e senza spazio", {"natural": "tree", "height": ".5m"}, ["unit_format"]),
    ("diameter_unit: senza lo zero", {"natural": "tree", "diameter": ".75"}, ["diameter_unit"]),
    ("number_invalid: testo", {"natural": "tree", "height": "tall"}, ["number_invalid"]),
    ("number_invalid: solo il punto", {"natural": "tree", "height": "."}, ["number_invalid"]),
    ("number_invalid: zero", {"natural": "tree", "circumference": "0"}, ["number_invalid"]),
    ("number_invalid: intervallo", {"natural": "tree", "height": "10-12"}, ["number_invalid"]),
    ("circ_diam_mismatch", {"natural": "tree", "circumference": "2", "diameter": "300"}, ["circ_diam_mismatch"]),
    ("circumference e diameter coerenti", {"natural": "tree", "circumference": "0.95", "diameter": "300"}, []),

    # --- rapporto altezza / circonferenza ---
    ("too_slender", {"natural": "tree", "height": "24", "circumference": "0.01"}, ["too_slender"]),
    ("too_stocky", {"natural": "tree", "height": "2", "circumference": "10"}, ["too_stocky"]),
    ("too_slender_species", {**OAK, "height": "20", "circumference": "0.4"}, ["too_slender_species"]),
    ("too_slender_species: non sotto i 10 m", {**OAK, "height": "8", "circumference": "0.16"}, []),
    ("ceppo: niente rapporto", {"natural": "tree_stump", "height": "0.5", "circumference": "3"}, []),
    ("dipendenza: con height niente too_slender", {"natural": "tree", "height": "150", "circumference": "0.5"},
     ["height"]),
    ("dipendenza: con height_species niente too_slender", {**OAK, "height": "60", "circumference": "0.5"},
     ["height_species"]),
    ("dipendenza: con height_species niente too_slender_species", {**OAK, "height": "60", "circumference": "1"},
     ["height_species"]),
    ("dipendenza: con circumference niente too_stocky", {"natural": "tree", "height": "5", "circumference": "50"},
     ["circumference"]),
    ("dipendenza: con circumference_species niente too_stocky", {**OAK, "height": "5", "circumference": "20"},
     ["circumference_species"]),

    # --- species e genus ---
    ("tag_in_value", {"natural": "tree", "genus": "leaf_type=broadleaved"}, ["tag_in_value"]),
    ("dipendenza: con tag_in_value niente check sulla forma di species",
     {"natural": "tree", "species": "genus=quercus"}, ["tag_in_value"]),
    ("not_latin", {"natural": "tree", "genus": "череша"}, ["not_latin"]),
    ("species: una sola parola", {"natural": "tree", "species": "Magnolia"}, ["species"]),
    ("dipendenza: con species (una parola) niente species_lowercase", {"natural": "tree", "species": "quercus"},
     ["species"]),
    ("species_lowercase", {"natural": "tree", "species": "quercus robur"}, ["species_lowercase"]),
    ("species_unidentified", {"natural": "tree", "species": "Quercus sp."}, ["species_unidentified"]),
    ("species_epithet_case", {"natural": "tree", "species": "Quercus Robur"}, ["species_epithet_case"]),
    ("species_epithet_case: nome della lista con la cultivar", {"natural": "tree", "species": "Quercus Robur 'Fastigiata'"},
     ["species_epithet_case"]),
    ("species_epithet_case: genere noto, epiteto di un'altra specie della lista", {"natural": "tree", "species": "Tilia Pinea"},
     ["species_epithet_case"]),
    ("species_common_name: genere noto, la seconda parola non è un epiteto della lista",
     {"natural": "tree", "species": "Tilia Greenspire"}, ["species_common_name"]),
    ("species_epithet_case: genere noto, nome tutto maiuscolo", {"natural": "tree", "species": "TILIA GREENSPIRE"},
     ["species_epithet_case"]),
    ("species_epithet_case: maiuscola dentro l'epiteto", {"natural": "tree", "species": "Quercus roBur"},
     ["species_epithet_case"]),
    ("species_epithet_case: maiuscola dentro, genere noto", {"natural": "tree", "species": "Tilia cordAta"},
     ["species_epithet_case"]),
    ("species_epithet_case: maiuscola dentro, genere sconosciuto", {"natural": "tree", "species": "Abies albA"}, []),
    ("species_epithet_case: maiuscole solo nella cultivar", {"natural": "tree", "species": "Quercus robur 'FastiGiata'"},
     []),
    ("species_epithet_case: genere maiuscolo", {"natural": "tree", "species": "QUERCUS robur"}, ["species_epithet_case"]),
    ("species_epithet_case: maiuscola dentro il genere, genere noto", {"natural": "tree", "species": "TiLia cordata"},
     ["species_epithet_case"]),
    ("species_epithet_case: segno di ibrido X, genere noto", {"natural": "tree", "species": "Tilia X europaea"},
     ["species_epithet_case"]),
    ("species_epithet_case: segno di ibrido X, genere sconosciuto", {"natural": "tree", "species": "Abies X alba"}, []),
    ("species_epithet_case: segno di ibrido x minuscolo", {"natural": "tree", "species": "Tilia x europaea"}, []),
    ("species_common_name: X seguita da una maiuscola", {"natural": "tree", "species": "Tilia X Europaea"},
     ["species_common_name"]),
    ("species_hybrid_end", {"natural": "tree", "species": "Platanus acerifolia x"}, ["species_hybrid_end"]),
    ("species_hybrid_end: segno ×", {"natural": "tree", "species": "Platanus acerifolia ×"}, ["species_hybrid_end"]),
    ("species_hybrid_end: segno anche in mezzo", {"natural": "tree", "species": "Platanus x acerifolia X"},
     ["species_hybrid_end"]),
    ("species_hybrid_end: specie che nella lista non è un ibrido", {"natural": "tree", "species": "Quercus robur x"},
     ["species_hybrid_end"]),
    ("species_hybrid_end: segno in mezzo", {"natural": "tree", "species": "Platanus x acerifolia"}, []),
    ("species_hybrid_end: ibrido scritto con i due genitori", {"natural": "tree", "species": "Quercus robur x pinea"}, []),
    ("species_hybrid_end: segno dopo il solo genere", {"natural": "tree", "species": "Platanus x"}, ["species"]),
    ("species_hybrid_end: segno maiuscolo dopo il solo genere", {"natural": "tree", "species": "Tilia X"}, ["species"]),
    ("species_hybrid_end: cultivar dopo il segno", {"natural": "tree", "species": "Platanus acerifolia x 'Tremonia'"}, []),
    ("dipendenza: con species_hybrid_end e la proposta niente species_lowercase",
     {"natural": "tree", "species": "platanus acerifolia x"}, ["species_hybrid_end"]),
    ("dipendenza: con species_hybrid_end senza proposta resta species_lowercase",
     {"natural": "tree", "species": "quercus robur x"}, ["species_hybrid_end", "species_lowercase"]),
    ("dipendenza: species_epithet_case scrive già il segno al suo posto", {"natural": "tree", "species": "Platanus Acerifolia x"},
     ["species_epithet_case"]),
    ("dipendenza: species_epithet_case, specie che nella lista non è un ibrido", {"natural": "tree", "species": "Quercus Robur x"},
     ["species_epithet_case", "species_hybrid_end"]),
    ("dipendenza: con species_author niente species_epithet_case per il segno X",
     {"natural": "tree", "species": "Tilia X europaea L."}, ["species_author"]),
    ("dipendenza: con species_epithet_case niente species_lowercase", {"natural": "tree", "species": "quercus Robur"},
     ["species_epithet_case"]),
    ("dipendenza: con species_author niente species_epithet_case", {"natural": "tree", "species": "Quercus roBur L."},
     ["species_author"]),
    ("dipendenza: con species_author niente species_lowercase", {"natural": "tree", "species": "quercus robur L."},
     ["species_author"]),
    ("species_common_name", {"natural": "tree", "species": "Black Matipo"}, ["species_common_name"]),
    ("species_common_name: nome comune con un genere certo", {"natural": "tree", "species": "Horse Chestnut"},
     ["species_common_name"]),
    ("species_common_name: nome comune di una specie della lista", {"natural": "tree", "species": "English Oak"},
     ["species_common_name"]),
    ("dipendenza: con species_common_name niente species_parentheses",
     {"natural": "tree", "species": "Black Matipo (Pittosporum tenuifolium)"}, ["species_common_name"]),
    ("species_author", {"natural": "tree", "species": "Quercus robur L."}, ["species_author"]),
    ("dipendenza: con species_author niente species_parentheses",
     {"natural": "tree", "species": "Camellia sinensis (L.) Kuntze"}, ["species_author"]),
    ("species_parentheses", {"natural": "tree", "species": "Quercus robur (red)"}, ["species_parentheses"]),
    ("species_genus_abbr", {"natural": "tree", "species": "Q. robur"}, ["species_genus_abbr"]),
    ("species_genus_abbr: con genus, niente genus_mismatch", {"natural": "tree", "species": "Q. robur", "genus": "Quercus"},
     ["species_genus_abbr"]),
    ("species_genus_abbr: genus con un'altra iniziale", {"natural": "tree", "species": "Q. robur", "genus": "Pinus"},
     ["species_genus_abbr", "genus_mismatch"]),
    ("species_genus_abbr: genere che non si scioglie", {"natural": "tree", "species": "A. campestre"}, ["species_genus_abbr"]),
    ("species_parentheses: parola maiuscola tra parentesi in fondo, non un autore",
     {"natural": "tree", "species": "Quercus robur (Farnia)"}, ["species_parentheses"]),
    ("species_parentheses: parole maiuscole tra parentesi in fondo, non un autore",
     {"natural": "tree", "species": "Quercus robur (English Oak)"}, ["species_parentheses"]),
    ("species_author: autore tra parentesi, con il punto", {"natural": "tree", "species": "Quercus robur (L.)"},
     ["species_author"]),
    ("species_author: autore tra parentesi senza punto, seguito da un altro autore",
     {"natural": "tree", "species": "Malus toringo (Siebold) Siebold ex de Vriese"}, ["species_author"]),
    ("species_parentheses: sinonimi tra parentesi, non un autore",
     {"natural": "tree", "species": "Ulmus laevis (Ulmus pedenculata, Ulmus effusa)"}, ["species_parentheses"]),
    ("species_author: segno di ibrido attaccato all'epiteto",
     {"natural": "tree", "species": "Malus ×purpurea (E.Barbier) Rehder"}, ["species_author"]),
    ("species_author: con altro prima del nome", {"natural": "tree", "species": "Insieme omogeneo di Fagus sylvatica L."},
     ["species_author"]),
    ("underscore", {"natural": "tree", "genus": "Tilia_cordata"}, ["underscore"]),
    ("species_typo", {"natural": "tree", "species": "Quercus rubur"}, ["species_typo"]),
    ("species_typo: eccezione in whitelist", {"natural": "tree", "species": "Quercus robar"}, []),
    ("species_typo: eccezione in whitelist, con × e cultivar", {"natural": "tree", "species": "Quercus × robar 'Alba'"}, []),
    ("species_typo: non per una cultivar senza epiteto", {"natural": "tree", "species": "Quercus 'Rubur'"}, []),
    ("species_typo: non per una cultivar dopo il segno di ibrido", {"natural": "tree", "species": "Quercus x 'Rubur'"}, []),
    ("species_typo: stesse lettere in un altro ordine", {"natural": "tree", "species": "Prunus serratula"},
     ["species_typo"]),
    ("species_typo: non se una specie della whitelist è più vicina", {"natural": "tree", "species": "Quercus robarr"}, []),
    ("species_typo: non se una specie della whitelist è vicina quanto quella della lista",
     {"natural": "tree", "species": "Quercus robor"}, []),
    ("species_typo: non se due specie della lista sono vicine uguali", {"natural": "tree", "species": "Pirus piraster"}, []),
    ("species_typo: specie della lista vicina, nessun pareggio", {"natural": "tree", "species": "Pyrus piraster"},
     ["species_typo"]),
    ("species_typo: non se la proposta cambia l'iniziale dell'epiteto e un'altra specie con quell'iniziale è vicina",
     {"natural": "tree", "species": "Quercus ruber"}, []),
    ("species_typo: iniziale dell'epiteto diversa, nessun'altra specie vicina", {"natural": "tree", "species": "Quercus tuber"},
     ["species_typo"]),
    ("species_typo: non per una parola maiuscola corta con due lettere diverse",
     {"natural": "tree", "species": "Quercus Rabus"}, ["species_common_name"]),
    ("species_typo: parola maiuscola con una lettera diversa", {"natural": "tree", "species": "Quercus Rubur"},
     ["species_epithet_case", "species_typo"]),
    ("species: nome comune di un genere", {"natural": "tree", "species": "tilleul"}, ["species"]),
    ("species: genere con autore", {"natural": "tree", "species": "Tilia L."}, ["species"]),
    ("ibrido con x iniziale", {"natural": "tree", "species": "x Cupressocyparis leylandii",
                               "genus": "Cupressocyparis"}, []),
    ("ibrido con x iniziale, genere minuscolo", {"natural": "tree", "species": "x cupressocyparis leylandii"},
     ["species_lowercase"]),
    ("x ed epiteto senza genere", {"natural": "tree", "genus": "Quercus", "species": "x robur"}, ["species"]),
    ("x ed epiteto maiuscolo senza genere: niente genus_mismatch",
     {"natural": "tree", "genus": "Quercus", "species": "x Sepulcralis"}, ["species"]),
    ("x e un altro genere noto: genus_mismatch", {"natural": "tree", "genus": "Quercus", "species": "x Pinus"},
     ["species", "genus_mismatch"]),
    ("species_lang_only: nome di una specie", {"natural": "tree", "species:pl": "Klon zwyczajny"}, ["species_lang_only"]),
    ("species_lang_only: nome di una sottospecie", {"natural": "tree", "species:fr": "Mirabellier"}, ["species_lang_only"]),
    ("species_lang_only: nome di una specie, maiuscole diverse", {"natural": "tree", "species:pl": "klon ZWYCZAJNY"},
     ["species_lang_only"]),
    ("species_lang_only", {"natural": "tree", "species:it": "Leccio"}, ["species_lang_only"]),
    ("species_lang_only: nome italiano di una specie", {"natural": "tree", "species:it": "Bagolaro"},
     ["species_lang_only"]),
    ("species_lang_only: nome italiano di un genere", {"natural": "tree", "species:it": "Quercia"},
     ["species_lang_only"]),
    ("species:xx insieme a species", {**OAK, "species:it": "Farnia"}, []),
    ("species_lang_only: non per due lettere che non sono una lingua", {"natural": "tree", "species:mp": "Pilón"}, []),
    ("species_lang_only: non per species:ref", {"natural": "tree", "species:ref": "123"}, []),
    ("species_lang_only: lingua di tre lettere", {"natural": "tree", "species:ast": "Carbayu"}, ["species_lang_only"]),
    ("taxon_lang_only: non per taxon:ref", {"natural": "tree", "taxon:ref": "123"}, []),
    ("not_a_tree: species", {"natural": "tree", "species": "Agave americana"}, ["not_a_tree"]),
    ("not_a_tree: species con autore", {"natural": "tree", "species": "Agave americana L."},
     ["not_a_tree", "species_author"]),
    ("not_a_tree: solo species:wikidata", {"natural": "tree", "species:wikidata": "Q920007"}, ["not_a_tree"]),
    ("not_a_tree: species e species:wikidata", {"natural": "tree", "species": "Agave americana",
                                                "species:wikidata": "Q920007"}, ["not_a_tree"]),
    ("not_a_tree: non per i ceppi", {"natural": "tree_stump", "species": "Agave americana"}, []),
    ("not_a_tree: altra specie dello stesso genere", {"natural": "tree", "species": "Agave sisalana"}, []),
    ("genus: più parole", {"natural": "tree", "genus": "Quercus robur"}, ["genus"]),
    ("genus_lowercase", {"natural": "tree", "genus": "quercus"}, ["genus_lowercase"]),
    ("genus_common_name: al posto di genus_lowercase", {"natural": "tree", "genus": "quercia"}, ["genus_common_name"]),
    ("genus_common_name: con la maiuscola", {"natural": "tree", "genus": "Tilleul"}, ["genus_common_name"]),
    ("genus_common_name: con un carattere non ASCII", {"natural": "tree", "genus": "świerk"}, ["genus_common_name"]),
    ("dipendenza: con genus_common_name dello stesso genere niente genus_mismatch", {**OAK, "genus": "Quercia"},
     ["genus_common_name"]),
    ("genus_common_name e genus_mismatch: un altro genere", {**OAK, "genus": "Tilleul"},
     ["genus_common_name", "genus_mismatch"]),
    ("genus_mismatch", {**OAK, "genus": "Pinus"}, ["genus_mismatch"]),
    ("genus_mismatch: genus scritto male", {**OAK, "genus": "Quercos"}, ["genus_mismatch"]),
    ("name_taxon_missing: nome di una specie", {"natural": "tree", "name": "English oak"}, ["name_taxon_missing"]),
    ("name_taxon_missing: nome di un genere", {"natural": "tree", "name": "Quercia"}, ["name_taxon_missing"]),
    ("name_taxon_missing: nome scientifico di una specie", {"natural": "tree", "name": "quercus  Robur"},
     ["name_taxon_missing"]),
    ("name_taxon_missing: non per un nome scientifico fuori lista", {"natural": "tree", "name": "Citrus sinensis"}, []),
    ("name_taxon_missing: nome scientifico di un genere", {"natural": "tree", "name": "Quercus"}, ["name_taxon_missing"]),
    ("name_taxon: nome scientifico di un genere, con genus uguale", {"natural": "tree", "name": "Quercus",
     "genus": "Quercus"}, []),
    ("name_taxon: nome scientifico di un genere, con species dello stesso genere", {**OAK, "name": "Quercus"}, []),
    ("name_taxon_missing: non per un genere fuori lista", {"natural": "tree", "name": "Camphora"}, []),
    ("name_taxon: nome scientifico uguale a species", {**OAK, "name": "Quercus robur"}, []),
    ("name_taxon_mismatch: nome scientifico, species di un'altra specie", {"natural": "tree", "species": "Pinus pinea",
     "name": "Quercus robur"}, ["name_taxon_mismatch"]),
    ("name_taxon_missing: non con taxon", {"natural": "tree", "name": "Quercia", "taxon": "Quercus"}, []),
    ("taxon_mismatch: specie diversa dello stesso genere", {**OAK, "taxon": "Quercus suber"}, ["taxon_mismatch"]),
    ("taxon_mismatch: specie di un altro genere", {**OAK, "taxon": "Pinus pinea"}, ["taxon_mismatch"]),
    ("taxon_mismatch: genere diverso da quello di species", {**OAK, "taxon": "Pinus"}, ["taxon_mismatch"]),
    ("taxon_mismatch: genere diverso da genus", {"natural": "tree", "genus": "Quercus", "taxon": "Pinus"},
     ["taxon_mismatch"]),
    ("taxon_mismatch: specie di un genere diverso da genus", {"natural": "tree", "genus": "Pinus", "taxon": "Quercus robur"},
     ["taxon_mismatch"]),
    ("taxon_mismatch: non con la stessa specie", {**OAK, "taxon": "Quercus robur"}, []),
    ("taxon_mismatch: non con il genere della specie", {**OAK, "taxon": "Quercus"}, []),
    ("taxon_mismatch: non con un sinonimo", {**OAK, "taxon": "Quercus pedunculata"}, []),
    ("taxon_mismatch: non con una cultivar della stessa specie", {**OAK, "taxon": "Quercus robur 'Fastigiata'"}, []),
    ("taxon_mismatch: non con un genere sconosciuto", {**OAK, "taxon": "Fagaceae"}, []),
    ("taxon_mismatch: non con più valori", {**OAK, "taxon": "Quercus suber;Quercus robur"}, []),
    ("name_taxon_missing: non per un nome proprio", {"natural": "tree", "name": "Quercia delle streghe"}, []),
    ("name_taxon_missing: non con più valori", {"natural": "tree", "name": "Quercia;Bagolaro"}, []),
    ("name_taxon: species uguale", {**OAK, "name": "English oak"}, []),
    ("name_taxon: species sinonimo", {"natural": "tree", "species": "Quercus pedunculata", "name": "English oak"}, []),
    ("name_taxon: species dello stesso genere del nome di un genere", {**OAK, "name": "Quercia"}, []),
    ("name_taxon: genus uguale al nome di un genere", {"natural": "tree", "genus": "Quercus", "name": "Quercia"}, []),
    ("name_taxon_mismatch: species di un'altra specie", {"natural": "tree", "species": "Quercus petraea",
     "name": "English oak"}, ["name_taxon_mismatch"]),
    ("name_taxon_mismatch: species di un altro genere", {"natural": "tree", "species": "Pinus pinea", "name": "Quercia"},
     ["name_taxon_mismatch"]),
    ("name_taxon_mismatch: genus di un altro genere", {"natural": "tree", "genus": "Pinus", "name": "English oak"},
     ["name_taxon_mismatch"]),
    ("name_taxon_mismatch: non con species che non è un nome scientifico", {"natural": "tree", "species": "Roverella",
     "name": "English oak"}, ["species"]),
    ("name_taxon_mismatch: non con genus nome comune", {"natural": "tree", "genus": "Tilleul", "name": "Quercia"},
     ["genus_common_name"]),
    ("name_botanical: genere", {"natural": "tree", "name:botanical": "Tilia"}, ["name_botanical"]),
    ("name_botanical: specie", {"natural": "tree", "name:botanical": "Quercus robur"}, ["name_botanical"]),
    ("name_botanical: con species uguale", {**OAK, "name:botanical": "Quercus robur"}, ["name_botanical"]),
    ("name_botanical: nome sconosciuto", {"natural": "tree", "name:botanical": "Fagaceae"}, ["name_botanical"]),
    ("name_botanical: vuoto", {"natural": "tree", "name:botanical": " "}, []),
    ("taxon_lang_only", {"natural": "tree", "taxon:en": "Oak"}, ["taxon_lang_only"]),
    ("taxon:xx insieme a taxon", {"natural": "tree", "taxon": "Quercus", "taxon:en": "Oak"}, []),
    ("taxon:cultivar non è una lingua", {"natural": "tree", "taxon:cultivar": "Fastigiata"}, []),
    ("multiple_values: genus e species, senza genus_mismatch",
     {"natural": "tree", "genus": "Quercus;Pinus", "species": "Quercus robur;Pinus pinea"}, ["multiple_values"]),
    ("multiple_values: solo species", {"natural": "tree", "species": "Quercus robur; Pinus pinea"}, ["multiple_values"]),
    ("dipendenza: con tag_in_value niente multiple_values", {"natural": "tree", "genus": "a=b;c"}, ["tag_in_value"]),
    ("dipendenza: species è un epiteto, niente genus_mismatch",
     {"natural": "tree", "genus": "Quercus", "species": "robur"}, ["species"]),
    ("dipendenza: genus ed epiteto maiuscolo sono una specie della lista, niente genus_mismatch",
     {"natural": "tree", "genus": "Pinus", "species": "Pinea"}, ["species"]),
    ("dipendenza: epiteto maiuscolo che con genus non è una specie della lista, niente genus_mismatch",
     {"natural": "tree", "genus": "Quercus", "species": "Pinea"}, ["species"]),
    ("species di una parola che è un altro genere: genus_mismatch",
     {"natural": "tree", "genus": "Quercus", "species": "Pinus"}, ["species", "genus_mismatch"]),

    # --- leaf_type e leaf_cycle ---
    ("leaf_type", {"natural": "tree", "leaf_type": "broad"}, ["leaf_type"]),
    ("leaf_cycle", {"natural": "tree", "leaf_cycle": "decidous"}, ["leaf_cycle"]),
    ("leaf_type_mismatch dalla specie", {**OAK, "leaf_type": "needleleaved"}, ["leaf_type_mismatch"]),
    ("leaf_type_mismatch dal genere", {"natural": "tree", "genus": "Pinus", "leaf_type": "broadleaved"},
     ["leaf_type_mismatch"]),
    ("leaf_type=palm su una latifoglia", {**OAK, "leaf_type": "palm"}, ["leaf_type_mismatch"]),
    ("leaf_type=palm su una pianta dall'aspetto di palma", {"natural": "tree", "genus": "Yucca", "leaf_type": "palm"},
     []),
    ("leaf_type=palm su una palma che la lista dà broadleaved", {"natural": "tree", "genus": "Trachycarpus", "leaf_type": "palm"},
     []),
    ("leaf_type=broadleaved su una palma", {"natural": "tree", "genus": "Trachycarpus", "leaf_type": "broadleaved"}, []),
    ("leaf_types in whitelist: broadleaved", {"natural": "tree", "genus": "Cycas", "leaf_type": "broadleaved"}, []),
    ("leaf_types in whitelist: palm, genere da species",
     {"natural": "tree", "species": "Cycas revoluta", "leaf_type": "palm"}, []),
    ("leaf_cycle_mismatch", {**OAK, "leaf_cycle": "evergreen"}, ["leaf_cycle_mismatch"]),
    ("leaf_cycle_mismatch: cultivar, dalla specie", {"natural": "tree", "species": "Quercus robur 'Fastigiata'",
                                                     "leaf_cycle": "evergreen"}, ["leaf_cycle_mismatch"]),
    ("leaf_cycle_mismatch: con l'autore, dalla specie", {"natural": "tree", "species": "Quercus robur L.",
                                                         "leaf_cycle": "evergreen"}, ["leaf_cycle_mismatch", "species_author"]),
    ("leaf_type_mismatch: cultivar, dalla specie", {"natural": "tree", "species": "Quercus robur 'Fastigiata'",
                                                    "leaf_type": "needleleaved"}, ["leaf_type_mismatch"]),
    ("leaf_cycle: non dalla prima specie di un ibrido con due genitori",
     {"natural": "tree", "species": "Quercus robur × petraea", "leaf_cycle": "evergreen"}, []),
    ("semi_deciduous vale come deciduous", {**OAK, "leaf_cycle": "semi_deciduous"}, []),
    ("genere con entrambi i leaf_cycle", {"natural": "tree", "genus": "Magnolia", "leaf_cycle": "evergreen"}, []),

    # --- Wikidata ---
    ("wikidata_mismatch: QID diverso da quello della lista", {**OAK, "species:wikidata": "Q920002"},
     ["wikidata_mismatch"]),   # dipendenza: niente wikidata_name_mismatch per lo stesso tag
    ("wikidata_mismatch: QID di un'altra specie della lista",
     {"natural": "tree", "species": "Fagus sylvatica", "species:wikidata": "Q920001"}, ["wikidata_mismatch"]),
    ("wikidata_mismatch: eccezione in whitelist", {**OAK, "species:wikidata": "Q920009"}, []),
    ("wikidata_mismatch: non con il QID della specie proposta da species_typo",
     {"natural": "tree", "species": "Quercus rubur", "species:wikidata": "Q920001"},
     ["species_typo"]),        # né wikidata_name_mismatch: il QID va bene per la specie corretta
    ("wikidata_not_taxon", {**OAK, "species:wikidata": "Q920004"},
     ["wikidata_not_taxon"]),  # dipendenza: niente wikidata_mismatch per lo stesso tag
    ("wikidata_is_taxon", {"natural": "tree", "wikidata": "Q920001"}, ["wikidata_is_taxon"]),
    ("wikidata_rank: genus:wikidata è una specie",
     {"natural": "tree", "genus": "Quercus", "genus:wikidata": "Q920001"},
     ["wikidata_rank"]),       # dipendenza: niente wikidata_name_mismatch per lo stesso tag
    ("wikidata_rank: species:wikidata è un genere", {"natural": "tree", "species:wikidata": "Q920003"},
     ["wikidata_rank"]),
    ("wikidata_name_mismatch: genus", {"natural": "tree", "genus": "Quercus", "genus:wikidata": "Q920005"},
     ["wikidata_name_mismatch"]),
    ("wikidata_name_mismatch: species non in lista",
     {"natural": "tree", "species": "Fagus sylvatica", "species:wikidata": "Q920006"}, ["wikidata_name_mismatch"]),
    ("wikidata_tree_species", {**OAK, "wikidata": "Q910003"}, ["wikidata_tree_species"]),
    ("specie dell'albero su Wikidata uguale a species", {"natural": "tree", "species": "Pinus pinea",
                                                         "wikidata": "Q910003"}, []),
    ("wikidata_tree_species: non se Wikidata ha solo il genere (consiglio hint_wg)", {**OAK, "wikidata": "Q910008"},
     []),
    ("wikidata_tree_species: genere su Wikidata diverso", {"natural": "tree", "species": "Pinus pinea",
                                                           "wikidata": "Q910008"}, ["wikidata_tree_species"]),
    ("specie dell'albero su Wikidata sinonimo di species", {"natural": "tree", "species": "Quercus pedunculata",
                                                            "wikidata": "Q910007"}, []),
    ("wikipedia_is_taxon", {"natural": "tree", "wikipedia": "it:Quercus_robur"}, ["wikipedia_is_taxon"]),
    ("wikipedia_is_taxon: voce di un genere", {"natural": "tree", "wikipedia": "it:Quercus"}, ["wikipedia_is_taxon"]),
    ("wikipedia: voce del singolo albero", {"natural": "tree", "wikipedia": "it:Quercia delle streghe"}, []),
    ("wikipedia: voce non ancora risolta", {"natural": "tree", "wikipedia": "it:Quercus ilex"}, []),
    ("wikidata_far", {"natural": "tree", "wikidata": "Q910002"}, ["wikidata_far"]),
    ("wikidata_photo_far", {"natural": "tree", "wikidata": "Q910006"}, ["wikidata_photo_far"]),

    # --- collegamenti ---
    ("link_format: wikidata", {"natural": "tree", "wikidata": "Quercus"}, ["link_format"]),
    ("link_format: image", {"natural": "tree", "image": "foto.jpg"}, ["link_format"]),
    ("link_format: non per image=File:… (un consiglio)", {"natural": "tree", "image": "File:Etrep-Vierge.JPG"}, []),
    ("link_format: due file in image", {"natural": "tree", "image": "File:A.jpg;File:B.jpg"}, ["link_format"]),
    ("link_format: due indirizzi in image",
     {"natural": "tree", "image": "https://example.org/ok.jpg;https://example.org/ok.jpg"}, ["link_format"]),
    ("link_format: due QID in wikidata", {"natural": "tree", "wikidata": "Q910001;Q910001"}, ["link_format"]),
    ("link_format: wikimedia_commons", {"natural": "tree", "wikimedia_commons": "Tree.jpg"}, ["link_format"]),
    ("link_format: wikimedia_commons con l'indirizzo della pagina",
     {"natural": "tree", "wikimedia_commons": "https://commons.wikimedia.org/wiki/File:Big_tree.jpg"}, ["link_format"]),
    ("link_format: wikimedia_commons=File:… senza estensione", {"natural": "tree", "wikimedia_commons": "File:Big tree"},
     ["link_format"]),
    ("link_format: un file senza estensione tra più valori",
     {"natural": "tree", "wikimedia_commons": "Category:Big trees;File:Big tree"}, ["link_format"]),
    ("link_format: non per Category:… (senza estensione)", {"natural": "tree", "wikimedia_commons": "Category:Big trees"}, []),
    ("link_format: non per File:… con l'estensione in maiuscolo",
     {"natural": "tree", "wikimedia_commons": "File:Big tree.JPEG"}, []),
    ("link_format: panoramax", {"natural": "tree", "panoramax": "12345"}, ["link_format"]),
    ("broken_link: image", {"natural": "tree", "image": "https://example.org/gone.jpg"}, ["broken_link"]),
    ("website_wikipedia: website", {"natural": "tree", "website": "https://en.wikipedia.org/wiki/Kett%27s_Oak"},
     ["website_wikipedia"]),
    ("website_wikipedia: url sul sito per cellulari", {"natural": "tree", "url": "https://fr.m.wikipedia.org/wiki/H%C3%AAtre"},
     ["website_wikipedia"]),
    ("website_wikipedia: anche con wikipedia=*", {"natural": "tree", "wikipedia": "en:Kett's Oak",
     "website": "https://en.wikipedia.org/wiki/Kett%27s_Oak"}, ["website_wikipedia"]),
    ("website_wikipedia: non per un altro sito", {"natural": "tree", "website": "https://example.org/wiki/Oak"}, []),
    ("broken_link: file di Commons mancante", {"natural": "tree", "wikimedia_commons": "File:Missing tree.jpg"},
     ["broken_link"]),
    ("commons_far", {"natural": "tree", "wikimedia_commons": "File:Far_tree.jpg"}, ["commons_far"]),
    ("commons_far: image su upload.wikimedia.org",
     {"natural": "tree", "image": "https://upload.wikimedia.org/wikipedia/commons/a/ab/Far_tree.jpg"}, ["commons_far"]),
    ("commons_far: miniatura su upload.wikimedia.org",
     {"natural": "tree", "image": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Far_tree.jpg/800px-Far_tree.jpg"},
     ["commons_far"]),
    ("file di Commons vicino", {"natural": "tree", "wikimedia_commons": "File:Near tree.jpg"}, []),
    ("dipendenza: con commons_far niente wikidata_photo_far per la stessa foto",
     {"natural": "tree", "wikidata": "Q910006", "wikimedia_commons": "File:Far tree.jpg"}, ["commons_far"]),

    # --- alberi monumentali ---
    ("monument_denotation: ref:masaf", {"natural": "tree", "ref:masaf": LISTED}, ["monument_denotation"]),
    ("monument_denotation: heritage:operator", {"natural": "tree", "heritage:operator": "ICNF"},
     ["monument_denotation"], None, "PT"),
    ("monument_denotation: ref:CRFOP", {"natural": "tree", "ref:CRFOP": "81025"}, ["monument_denotation"], None, "PL"),
    ("monument_denotation: ref:gid", {"natural": "tree", "ref:gid": "243066"}, ["monument_denotation"], None, "PL"),
    ("ref:CRFOP e ref:gid con natural_monument", {**MON, "ref:CRFOP": "81025", "ref:gid": "243066"}, [], None, "PL"),
    ("monument_denotation: non per ref:CRFOP su un ceppo", {"natural": "tree_stump", "ref:CRFOP": "81025"}, [],
     None, "PL"),
    ("monument_denotation: ref:usop", {"natural": "tree", "ref:usop": "103456"}, ["monument_denotation"], None, "CZ"),
    ("ref:usop con natural_monument", {**MON, "ref:usop": "103456"}, [], None, "CZ"),
    ("monument_denotation: ref:masaf su un ceppo", {"natural": "tree_stump", "ref:masaf": LISTED},
     ["monument_denotation"]),
    ("monument_denotation: monumentaltrees.com",
     {"natural": "tree", "website": "https://www.monumentaltrees.com/en/ita/lazio/1/"}, ["monument_denotation"]),
    ("monument_denotation: tutela su Wikidata", {"natural": "tree", "wikidata": "Q910004"}, ["monument_denotation"]),
    # monument=tree vale come denotation=natural_monument (issue #95), ma è anche una fonte di monument_denotation:
    # l'errore resta finché manca denotation=natural_monument
    ("monument_denotation: monument=tree", {"natural": "tree", "monument": "tree"}, ["monument_denotation"]),
    ("monument_denotation: monument=tree tra più valori", {"natural": "tree", "monument": "memorial;tree"},
     ["monument_denotation"]),
    ("monument_denotation: monument=tree su un ceppo", {"natural": "tree_stump", "monument": "tree"},
     ["monument_denotation"]),
    ("monument=tree con natural_monument", {**MON, "monument": "tree"}, []),
    ("monument_denotation: non per monument=memorial", {"natural": "tree", "monument": "memorial"}, []),
    ("monument_denotation: anche con monument=tree", {"natural": "tree", "monument": "tree", "ref:masaf": LISTED},
     ["monument_denotation"]),
    ("monument_denotation: tutela su Wikidata anche con monument=tree", {"natural": "tree", "monument": "tree",
                                                                        "wikidata": "Q910004"}, ["monument_denotation"]),
    ("small_monument: monument=tree", {"natural": "tree", "monument": "tree", "height": "1"},
     ["monument_denotation", "small_monument"]),
    ("small_monument: monument=tree tra più valori", {"natural": "tree", "monument": "memorial;tree", "height": "1"},
     ["monument_denotation", "small_monument"]),
    ("small_monument: non per monument=memorial", {"natural": "tree", "monument": "memorial", "height": "1"}, []),
    ("monument_denotation: non per una tutela solo culturale su Wikidata", {"natural": "tree", "wikidata": "Q910009"}, []),
    ("monument_denotation: tutela culturale e naturale su Wikidata", {"natural": "tree", "wikidata": "Q910010"},
     ["monument_denotation"]),
    ("monument_denotation: id di monumentaltrees.com su Wikidata", {"natural": "tree", "wikidata": "Q910005"},
     ["monument_denotation"]),
    ("fonte monumentale con natural_monument", {**MON, "ref:masaf": LISTED}, []),
    ("fonte monumentale con natural_monument dopo uno spazio: solo lo spazio è segnalato",
     {"natural": "tree", "denotation": "urban; natural_monument", "ref:masaf": LISTED}, ["denotation_value"]),
    ("fonte monumentale con natural_monument tra più valori",
     {"natural": "tree", "denotation": "urban;natural_monument", "ref:masaf": LISTED}, []),
    ("small_monument: altezza", {**MON, "height": "1"}, ["small_monument"]),
    ("small_monument: circonferenza", {**MON, "circumference": "0.1"}, ["small_monument"]),
    ("small_monument: eccezione in whitelist", {**MON, "height": "1"}, [], 4242),
    ("small_monument: non per i ceppi", {"natural": "tree_stump", "denotation": "natural_monument", "height": "1"}, []),
    ("albero piccolo non monumentale", {"natural": "tree", "height": "1"}, []),
    ("dipendenza: con too_stocky niente small_monument per l'altezza", {**MON, "height": "1", "circumference": "5"},
     ["too_stocky"]),
    ("dipendenza: con too_slender niente small_monument per la circonferenza",
     {**MON, "height": "24", "circumference": "0.01"}, ["too_slender"]),
    # membri di un insieme omogeneo MASAF (relazione con ref:masaf, issue #9): monumentali senza natural_monument
    ("insieme omogeneo: membro senza natural_monument non è un errore", {**OAK, "denotation": "park"}, [], MEMBER),
    ("insieme omogeneo: membro con natural_monument", {**MON}, [], MEMBER),
    ("insieme omogeneo: membro con un ref:masaf suo", {"natural": "tree", "ref:masaf": LISTED},
     ["monument_denotation"], MEMBER),
    ("insieme omogeneo: membro con un ref:masaf suo rimosso", {**MON, "ref:masaf": REMOVED}, ["masaf_removed"], MEMBER),
    ("small_monument: membro di un insieme omogeneo", {"natural": "tree", "denotation": "park", "height": "1"},
     ["small_monument"], MEMBER),
    ("small_monument: non per un ceppo membro di un insieme omogeneo", {"natural": "tree_stump", "height": "1"}, [],
     MEMBER),
    ("masaf_criteria", {"natural": "tree", "masaf:criteria": "a1,B"}, ["masaf_criteria"]),
    ("masaf:criteria validi", {"natural": "tree", "masaf:criteria": "a1,b"}, []),
    ("masaf_removed", {**MON, "ref:masaf": REMOVED}, ["masaf_removed"]),
    ("masaf_removed: non per i ceppi", {"natural": "tree_stump", "denotation": "natural_monument",
                                        "ref:masaf": REMOVED}, []),
    ("masaf_unknown", {**MON, "ref:masaf": "003/A003/RM/03"}, ["masaf_unknown"]),
    ("masaf_unknown: lo zero iniziale non conta", {**MON, "ref:masaf": "02/A002/RM/02"}, []),
    ("masaf_unknown: primo numero di una cifra", {**MON, "ref:masaf": "2/A002/RM/02"}, ["masaf_unknown"]),
    ("masaf_unknown: anche sui ceppi", {"natural": "tree_stump", "denotation": "natural_monument",
                                        "ref:masaf": "003/A003/RM/03"}, ["masaf_unknown"]),
    ("masaf_unknown: non fuori dall'Italia", {**MON, "ref:masaf": "003/A003/RM/03"}, ["register_country"], None, "PL"),
    ("register_country: ref:CRFOP in Italia", {**MON, "ref:CRFOP": "81025"}, ["register_country"]),
    ("register_country: ref:masaf in Polonia", {**MON, "ref:masaf": LISTED}, ["register_country"], None, "PL"),
    ("register_country: ref:rami in Polonia", {**MON, "ref:rami": "123"}, ["register_country"], None, "PL"),
    ("ref:rami in Italia", {**MON, "ref:rami": "123"}, []),
    ("register_country: ref:usop in Polonia", {**MON, "ref:usop": "103456"}, ["register_country"], None, "PL"),
    ("register_country: heritage:operator=anf in Portogallo", {**MON, "heritage:operator": "icnf;ANF"},
     ["register_country"], None, "PT"),
    ("register_country: anche sui ceppi", {"natural": "tree_stump", "ref:gid": "243066"}, ["register_country"]),
    ("register_country: heritage:operator di un altro ente", {"natural": "tree", "heritage:operator": "comune"}, []),
    ("register_country: nazione della regione non nota", {**MON, "ref:CRFOP": "81025"}, [], None, ""),

    # --- start_date ---
    ("start_date nelle forme della wiki", {"natural": "tree", "start_date": "1850-05-01"}, []),
    ("start_date approssimativo", {"natural": "tree", "start_date": "~1850"}, []),
    ("start_date: decennio, secolo e intervallo", {"natural": "tree", "start_date": "early 1850s..late C19"}, []),
    ("start_date: before una data passata", {"natural": "tree", "start_date": "before 1900"}, []),
    ("start_date_invalid: before una data futura", {"natural": "tree", "start_date": "before 2999"},
     ["start_date_invalid"]),
    ("start_date_invalid: testo", {"natural": "tree", "start_date": "old"}, ["start_date_invalid"]),
    ("start_date_invalid: formato europeo", {"natural": "tree", "start_date": "05/03/1990"}, ["start_date_invalid"]),
    ("start_date_invalid: giorno inesistente", {"natural": "tree", "start_date": "2020-02-30"}, ["start_date_invalid"]),
    ("start_date_invalid: intervallo con un estremo sbagliato", {"natural": "tree", "start_date": "1850..dopo"},
     ["start_date_invalid"]),
    ("start_date_invalid: mese e giorno senza zero", {"natural": "tree", "start_date": "1990-5-3"},
     ["start_date_invalid"]),
    ("start_date_invalid: intervallo rovesciato", {"natural": "tree", "start_date": "1900..1800"},
     ["start_date_invalid"]),
    ("start_date: intervallo dentro lo stesso secolo", {"natural": "tree", "start_date": "1890s..C19"}, []),
    ("start_date: intervallo nello stesso anno", {"natural": "tree", "start_date": "1900-05..1900"}, []),
    ("start_date_invalid: prefissi ripetuti", {"natural": "tree", "start_date": "before after 1850"},
     ["start_date_invalid"]),
    ("start_date_invalid: secolo con la c minuscola", {"natural": "tree", "start_date": "c16"},
     ["start_date_invalid"]),
    ("start_date_invalid: nel futuro", {"natural": "tree", "start_date": "2999-01"}, ["start_date_invalid"]),
    ("start_date: data giuliana", {"natural": "tree", "start_date": "j:1918-01-31"}, []),
    ("start_date: data giuliana del 29 febbraio, che nel gregoriano non c'è", {"natural": "tree", "start_date": "j:1700-02-29"},
     []),
    ("start_date: data giuliana approssimativa, solo l'anno", {"natural": "tree", "start_date": "~j:1850"}, []),
    ("start_date_invalid: data giuliana inesistente", {"natural": "tree", "start_date": "j:1918-02-30"},
     ["start_date_invalid"]),
    ("start_date_invalid: decennio con j:", {"natural": "tree", "start_date": "j:1850s"}, ["start_date_invalid"]),
    ("dipendenza: con tag_in_value niente start_date_invalid", {"natural": "tree", "start_date": "start_date=1990"},
     ["tag_in_value"]),
    # --- planted_date (issue #81): stesse forme di start_date ---
    ("planted_date nelle forme della wiki", {"natural": "tree", "planted_date": "2020-03"}, []),
    ("planted_date approssimativo", {"natural": "tree", "planted_date": "~1990"}, []),
    ("start_date_invalid: planted_date non valido", {"natural": "tree", "planted_date": "spring 2020"},
     ["start_date_invalid"]),
    ("start_date_invalid: planted_date nel futuro", {"natural": "tree", "planted_date": "2999"}, ["start_date_invalid"]),
    ("start_date_invalid: tutte e due le date, un solo codice", {"natural": "tree", "start_date": "old",
                                                                 "planted_date": "2999"}, ["start_date_invalid"]),
    ("planted_date uguale a start_date", {"natural": "tree", "start_date": "2020-03-01", "planted_date": "2020-03-01"}, []),
    ("planted_date uguale a start_date, spazi in più", {"natural": "tree", "start_date": "2020", "planted_date": " 2020"},
     []),
    ("date_mismatch", {"natural": "tree", "start_date": "1990", "planted_date": "1992-04"}, ["date_mismatch"]),
    ("dipendenza: con start_date non valido niente date_mismatch", {"natural": "tree", "start_date": "old",
                                                                     "planted_date": "1992"}, ["start_date_invalid"]),
    ("dipendenza: con tag_in_value niente date_mismatch", {"natural": "tree", "start_date": "1990",
                                                            "planted_date": "planted_date=1992"}, ["tag_in_value"]),
    ("key_typo: planted:date", {"natural": "tree", "planted:date": "2020"}, ["key_typo"]),
    # --- sinonimi di planted_date (issue #90) ---
    ("key_typo: year_planted", {"natural": "tree", "year_planted": "2020"}, ["key_typo"]),
    ("key_typo: date:planted", {"natural": "tree", "date:planted": "2020"}, ["key_typo"]),
    ("key_typo: date_planted", {"natural": "tree", "date_planted": "2020"}, ["key_typo"]),
    ("key_typo: planted con una data", {"natural": "tree", "planted": "2020"}, ["key_typo"]),
    ("key_typo: non per planted=yes", {"natural": "tree", "planted": "yes"}, []),
    ("key_typo: non per planted=no", {"natural": "tree", "planted": "no"}, []),

    # --- denotation, tag non documentati, chiavi ---
    ("denotation_value: formato", {"natural": "tree", "denotation": "Urban"}, ["denotation_value"]),
    ("denotation_value: valore comune con altro testo attaccato", {"natural": "tree", "denotation": "urbantree"},
     ["denotation_value"]),
    ("denotation_value: valore raro", {"natural": "tree", "denotation": "rarevalue"}, ["denotation_value"]),
    ("denotation_value: spazio dopo il punto e virgola", {"natural": "tree", "denotation": "urban; park"},
     ["denotation_value"]),
    ("denotation_value: più valori senza spazi", {"natural": "tree", "denotation": "urban;park"}, []),
    ("undocumented_tag: tree", {"natural": "tree", "tree": "oak"}, ["undocumented_tag"]),
    ("undocumented_tag: type", {"natural": "tree", "type": "broad_leaved"}, ["undocumented_tag"]),
    ("type=palm non è segnalato", {"natural": "tree", "type": "palm"}, []),
    ("key_typo: lettere scambiate", {"natural": "tree", "heigth": "12"}, ["key_typo"]),
    ("key_typo: maiuscola", {"natural": "tree", "Species": "Quercus robur"}, ["key_typo"]),
    ("key_typo: ref: con il resto scritto male", {"natural": "tree", "ref:massaf": LISTED}, ["key_typo"]),
    ("key_typo: non per un'altra chiave ref: corta", {"natural": "tree", "ref:uid": "12"}, []),
    ("key_typo: ref:usop in maiuscolo", {**MON, "ref:USOP": "103456"}, ["key_typo"], None, "CZ"),
    # altre chiavi dei registri dei monumentali (issue #95)
    ("key_typo: ref:mipaaf", {**MON, "ref:mipaaf": LISTED}, ["key_typo"]),
    ("key_typo: ref:drusop", {**MON, "ref:drusop": "100217"}, ["key_typo"], None, "CZ"),
    ("key_typo: ref:DRUSOP", {**MON, "ref:DRUSOP": "7221"}, ["key_typo"], None, "CZ"),
    ("key_typo: ref:drusop:tree", {**MON, "ref:drusop:tree": "7231"}, ["key_typo"], None, "CZ"),
    ("key_typo: ref:GID", {**MON, "ref:GID": "185044"}, ["key_typo"], None, "PL"),
    ("chiavi nuove note: niente key_typo", {**MON, "ref:baumkunde": "https://www.baumkunde.de/baumregister/1/",
                                           "ref:mbreg": "http://mbreg.de/mbr/mbr_europe.php?q1=id&q=260",
                                           "ref:inaturalist.org": "69612090;172572384"}, []),
    # TROBI e champion trees: chiavi note, nessun errore (né monument_denotation: non è un registro di tutela)
    ("TROBI e champion tree senza errori", {**MON, "ref:TROBI": "107129", "ref:trobi": "107129", "division:champion": "yes",
     "champion:territory": "Ireland and Britain", "champion:category": "Girth/Height", "champion:date": "2012"}, [], None, "IE"),
    ("fixme", {"natural": "tree", "fixme": "check species"}, ["fixme"]),
    ("fixme: anche FIXME, senza key_typo", {"natural": "tree", "FIXME": "survey"}, ["fixme"]),
    ("fixme: anche su un ceppo", {"natural": "tree_stump", "fixme": "survey"}, ["fixme"]),
    ("fixme: non con il valore vuoto", {"natural": "tree", "fixme": " "}, []),
    ("fixme: non per note", {"natural": "tree", "note": "planted 2010"}, []),
    ("chiave numerata e sottochiave", {"natural": "tree", "image2": "https://example.org/ok.jpg",
                                       "circumference:note": "a 1,30 m"}, []),

    # --- più errori insieme ---
    ("più errori sullo stesso albero", {"natural": "tree", "species": "quercus robur", "height": "12,5",
                                        "leaf_type": "broad", "tree": "oak"},
     ["comma", "leaf_type", "species_lowercase", "undocumented_tag"]),
]


# Consigli di hint_props: (nome, tag, id del nodo, proprietà, atteso: True = c'è, False = non c'è, o il valore).
# hint_ms: soglia MASAF di circonferenza, solo nelle regioni italiane; hint_mf: coordinate dell'elenco MASAF lontane
# dall'albero; hint_wg: l'elemento Wikidata dell'albero ha solo il genere della specie
HINT_CASES = [
    ("hint_ms: circonferenza oltre la soglia", {**OAK, "circumference": "5"}, None, "hint_ms", True),
    ("hint_ms: non con natural_monument", {**MON, **OAK, "circumference": "5"}, None, "hint_ms", False),
    ("hint_ms: non per un membro di un insieme omogeneo", {**OAK, "denotation": "park", "circumference": "5"}, MEMBER,
     "hint_ms", False),
    # hint_mf: gli alberi finti sono in POS (feature_failures), GROUP_CODE nell'elenco è in FAR
    ("hint_mf: coordinate MASAF lontane", {**MON, "ref:masaf": GROUP_CODE}, None, "hint_mf", f"{GROUP_CODE}|138.1|Rieti"),
    ("hint_mf: coordinate MASAF vicine", {**MON, "ref:masaf": LISTED}, None, "hint_mf", False),
    ("hint_mf: solo i codici lontani", {**MON, "ref:masaf": f"{LISTED};{GROUP_CODE}"}, None, "hint_mf",
     f"{GROUP_CODE}|138.1|Rieti"),
    ("hint_mf: non su un ceppo", {"natural": "tree_stump", "ref:masaf": GROUP_CODE}, None, "hint_mf", False),
    ("hint_mf: non per un codice senza coordinate", {**MON, "ref:masaf": REMOVED}, None, "hint_mf", False),
    ("hint_wg: Wikidata ha solo il genere", {**OAK, "wikidata": "Q910008"}, None, "hint_wg", "Q910008|Quercus"),
    ("hint_wg: non per un altro genere", {"natural": "tree", "species": "Pinus pinea", "wikidata": "Q910008"}, None,
     "hint_wg", False),
    ("hint_la: species:la senza species", {"natural": "tree", "species:la": "Quercus  robur"}, None, "hint_la",
     "Quercus robur"),
    ("hint_la: fuori lista com'è scritto", {"natural": "tree", "species:la": "Citrus sinensis"}, None, "hint_la",
     "Citrus sinensis"),
    ("hint_la: non con species", {**OAK, "species:la": "Quercus robur"}, None, "hint_la", False),
    ("hint_la: non con un genus diverso", {"natural": "tree", "genus": "Pinus", "species:la": "Quercus robur"}, None,
     "hint_la", False),
    ("hint_la: con lo stesso genus", {"natural": "tree", "genus": "Quercus", "species:la": "Quercus robur"}, None,
     "hint_la", "Quercus robur"),
    ("hint_la: non con una parola sola", {"natural": "tree", "species:la": "Quercus"}, None, "hint_la", False),
    ("hint_la: non su un ceppo", {"natural": "tree_stump", "species:la": "Quercus robur"}, None, "hint_la", False),
    ("hint_tx: taxon è una specie della lista", {"natural": "tree", "taxon": "Quercus robur"}, None, "hint_tx",
     "species|Quercus robur"),
    ("hint_tx: taxon è un genere della lista", {"natural": "tree", "taxon": "pinus"}, None, "hint_tx", "genus|Pinus"),
    ("hint_tx: species uguale", {**OAK, "taxon": "Quercus robur"}, None, "hint_tx", "species|Quercus robur"),
    ("hint_tx: non con un genus diverso", {"natural": "tree", "taxon": "Quercus robur", "genus": "Pinus"}, None,
     "hint_tx", False),
    ("hint_tx: non con una cultivar", {"natural": "tree", "taxon": "Quercus robur 'Fastigiata'"}, None, "hint_tx", False),
    ("hint_tx: non con una species di un altro genere", {"natural": "tree", "taxon": "Quercus", "species": "Pinus pinea"},
     None, "hint_tx", False),
    ("hint_txl: taxon:xx passa in species:xx", {**OAK, "taxon": "Quercus robur", "taxon:en": "Oak"}, None, "hint_txl",
     "taxon:en=Oak"),
    ("hint_txl: taxon:xx passa in genus:xx", {"natural": "tree", "taxon": "pinus", "taxon:en": "Pine"}, None, "hint_txl",
     "taxon:en=Pine"),
    ("hint_tx: non se species:xx ha un altro valore", {**OAK, "taxon": "Quercus robur", "taxon:en": "Oak", "species:en": "Pedunculate oak"},
     None, "hint_tx", False),
    ("hint_txl: niente senza taxon:xx", {**OAK, "taxon": "Quercus robur"}, None, "hint_txl", False),
    ("hint_tx: genere noto ed epiteto, non nella lista", {"natural": "tree", "taxon": "Quercus petraea"}, None, "hint_tx",
     "species|Quercus petraea|b"),
    ("hint_tx: genere ed epiteto, con il segno di ibrido", {"natural": "tree", "taxon": "Quercus × petraea"}, None,
     "hint_tx", "species|Quercus × petraea|b"),
    ("hint_tx: non per genere e sp", {"natural": "tree", "taxon": "Quercus sp"}, None, "hint_tx", False),
    ("hint_tx: non per un genere sconosciuto", {"natural": "tree", "taxon": "Fakegenus alba"}, None, "hint_tx", False),
    ("hint_tx: non per due parole in maiuscolo", {"natural": "tree", "taxon": "Quercus Petraea"}, None, "hint_tx", False),
    ("hint_tx: genere ed epiteto, non con un genus diverso", {"natural": "tree", "taxon": "Quercus petraea", "genus": "Pinus"},
     None, "hint_tx", False),
    # varietà e cultivar scritte in species (sp_base, sp_var, sp_cv: issue #68)
    ("sp_base: varietà", {"natural": "tree", "species": "Quercus robur var. fastigiata"}, None, "sp_base", "Quercus robur"),
    ("sp_var: varietà", {"natural": "tree", "species": "Quercus robur var. fastigiata"}, None, "sp_var", "fastigiata"),
    ("sp_cv: non con la sola varietà", {"natural": "tree", "species": "Quercus robur var. fastigiata"}, None, "sp_cv", False),
    ("sp_base: cultivar", {"natural": "tree", "species": "Acer palmatum 'Atropurpureum'"}, None, "sp_base", "Acer palmatum"),
    ("sp_cv: cultivar senza virgolette", {"natural": "tree", "species": "Acer palmatum 'Atropurpureum'"}, None, "sp_cv",
     "Atropurpureum"),
    ("sp_cv: virgolette tipografiche, più parole e apostrofo", {"natural": "tree", "species": "Fagus sylvatica ‘Dawyck's Gold’"},
     None, "sp_cv", "Dawyck's Gold"),
    ("sp_cv: dopo cv.", {"natural": "tree", "species": "Acer palmatum cv. 'Atropurpureum'"}, None, "sp_base", "Acer palmatum"),
    ("sp_base: varietà e cultivar, con ×", {"natural": "tree", "species": "Platanus × hispanica var. acerifolia 'Pyramidalis'"},
     None, "sp_base", "Platanus × hispanica"),
    ("sp_base: non con l'autore", {"natural": "tree", "species": "Fagus sylvatica L. var. purpurea"}, None, "sp_base",
     False),
    ("sp_base: la sottospecie resta", {"natural": "tree", "species": "Pinus nigra subsp. laricio var. calabrica"}, None,
     "sp_base", "Pinus nigra subsp. laricio"),
    ("sp_base: genere abbreviato, non con l'epiteto abbreviato", {"natural": "tree", "species": "Q. r. var. fastigiata",
                                                                  "genus": "Quercus"}, None, "sp_base", False),
    # genere della lista con la sola cultivar: sp_gen al posto di sp_base
    ("sp_base: non per genere e cultivar", {"natural": "tree", "species": "Magnolia 'Susan'"}, None, "sp_base", False),
    ("sp_gen: genere e cultivar", {"natural": "tree", "species": "magnolia 'Susan'"}, None, "sp_gen", "Magnolia"),
    ("sp_cv: genere e cultivar", {"natural": "tree", "species": "Magnolia 'Susan'"}, None, "sp_cv", "Susan"),
    ("sp_gen: non per un genere fuori lista", {"natural": "tree", "species": "Abcdia 'Susan'"}, None, "sp_gen", False),
    ("sp_gen: non con il segno di ibrido", {"natural": "tree", "species": "Magnolia x 'Susan'"}, None, "sp_gen", False),
    # cultivar senza virgolette dopo una specie della lista, var senza punto
    ("sp_base: cultivar senza virgolette", {"natural": "tree", "species": "Quercus robur Fastigiata"}, None, "sp_base",
     "Quercus robur"),
    ("sp_cv: cultivar senza virgolette, più parole", {"natural": "tree", "species": "Quercus robur Fastigiata Koster"},
     None, "sp_cv", "Fastigiata Koster"),
    ("sp_cv: senza virgolette, con il segno di ibrido", {"natural": "tree", "species": "Quercus x robur Fastigiata"},
     None, "sp_base", "Quercus x robur"),
    ("sp_base: senza virgolette, non per una specie fuori lista", {"natural": "tree", "species": "Acer campestre Elsrijk"},
     None, "sp_base", False),
    ("sp_base: senza virgolette, non con la parola minuscola", {"natural": "tree", "species": "Quercus robur fastigiata"},
     None, "sp_base", False),
    ("sp_base: non con un autore senza punto", {"natural": "tree", "species": "Quercus robur Roth"}, None, "sp_base", False),
    ("sp_base: non con un autore di una lettera", {"natural": "tree", "species": "Quercus robur L"}, None, "sp_base", False),
    ("sp_base: non con un autore con il punto", {"natural": "tree", "species": "Quercus robur L."}, None, "sp_base", False),
    ("sp_var: var senza punto", {"natural": "tree", "species": "Quercus robur var fastigiata"}, None, "sp_var", "fastigiata"),
    # varietà che è un taxon di rango varietà su Wikidata (sp_var_ok): solo per queste c'è la correzione
    ("sp_var_ok: varietà nota", {"natural": "tree", "species": "Pinus pinea var. fragilis"}, None, "sp_var_ok", 1),
    ("sp_var_ok: varietà nota, var senza punto", {"natural": "tree", "species": "Pinus pinea var fragilis"}, None,
     "sp_var_ok", 1),
    ("sp_var_ok: non per una varietà non nota", {"natural": "tree", "species": "Quercus robur var. fastigiata"}, None,
     "sp_var_ok", False),
    # tag che mancano per la specie senza cultivar (sp_base_wd, sp_base_lt, sp_base_lc)
    ("sp_base_wd: species:wikidata della specie", {"natural": "tree", "species": "Quercus robur 'Fastigiata'"}, None,
     "sp_base_wd", "Q920001"),
    ("sp_base_lc: leaf_cycle della specie", {"natural": "tree", "species": "Quercus robur 'Fastigiata'"}, None,
     "sp_base_lc", "deciduous"),
    ("sp_base_lt: non se c'è già", {"natural": "tree", "species": "Quercus robur 'Fastigiata'", "leaf_type": "broadleaved"},
     None, "sp_base_lt", False),
    # genere abbreviato
    ("sp_base: genere abbreviato, da genus", {"natural": "tree", "species": "T. cordata 'Greenspire'", "genus": "Tilia"},
     None, "sp_base", "Tilia cordata"),
    ("sp_abbr: genere abbreviato", {"natural": "tree", "species": "T. cordata", "genus": "Tilia"}, None, "sp_abbr", 1),
    ("sp_base: genere abbreviato, dalla lista", {"natural": "tree", "species": "Q. robur"}, None, "sp_base", "Quercus robur"),
    ("sp_base: genere abbreviato, più generi nella lista", {"natural": "tree", "species": "P. serrulata"}, None,
     "sp_base", "Prunus serrulata"),
    ("sp_base: genere abbreviato, genus con un'altra iniziale", {"natural": "tree", "species": "Q. robur", "genus": "Pinus"},
     None, "sp_base", False),
    ("sp_base: genere abbreviato che non si scioglie", {"natural": "tree", "species": "A. campestre"}, None, "sp_base", False),
    ("sp_abbr: non per il genere scritto intero", {"natural": "tree", "species": "Quercus robur 'Fastigiata'"}, None,
     "sp_abbr", False),
    ("sp_base: non con la varietà tra virgolette", {"natural": "tree", "species": "Fagus sylvatica var. 'Purpurea'"}, None,
     "sp_base", False),
    ("sp_base: non con due cultivar", {"natural": "tree", "species": "Malus domestica 'Gala' 'Fuji'"}, None, "sp_base", False),
    ("sp_base: non con virgolette spaiate", {"natural": "tree", "species": "Quercus robur 'Fastigiata"}, None, "sp_base", False),
    ("sp_base: non con più valori", {"natural": "tree", "species": "Quercus robur 'Fastigiata';Pinus pinea"}, None,
     "sp_base", False),
    ("sp_base: non per una specie e basta", OAK, None, "sp_base", False),
    ("hint_ne: name:en con la minuscola", {"natural": "tree", "name:en": "old oak"}, None, "hint_ne", "old oak"),
    ("hint_ne: non con la maiuscola", {"natural": "tree", "name:en": "Old Oak"}, None, "hint_ne", False),
    ("hint_nm: specie, con genus dello stesso genere", {"natural": "tree", "name": "bagolaro", "genus": "Celtis"}, None,
     "hint_nm", "species|Celtis australis"),
    ("hint_nm: specie, con genus dello stesso genere (genere noto)", {"natural": "tree", "name": "English oak",
     "genus": "Quercus"}, None, "hint_nm", "species|Quercus robur"),
    ("hint_nm: nome scientifico, con genus dello stesso genere", {"natural": "tree", "name": "Quercus robur",
     "genus": "Quercus"}, None, "hint_nm", "species|Quercus robur"),
    ("hint_nm: non senza tag del taxon (è un check)", {"natural": "tree", "name": "English oak"}, None, "hint_nm", False),
    ("hint_nm: non con species", {**OAK, "name": "English oak"}, None, "hint_nm", False),
    ("hint_nm: genere, non con genus", {"natural": "tree", "name": "Quercia", "genus": "Quercus"}, None, "hint_nm", False),
    ("hint_nt: note", {"natural": "tree", "note": "planted 2010"}, None, "hint_nt", "note=planted 2010"),
    ("hint_nt: note e NOTE", {"natural": "tree", "note": "a", "NOTE": "b"}, None, "hint_nt", "NOTE=b\nnote=a"),
    ("hint_nt: non per fixme", {"natural": "tree", "fixme": "survey"}, None, "hint_nt", False),
    ("hint_nt: non per note:it", {"natural": "tree", "note:it": "piantato nel 2010"}, None, "hint_nt", False),
    # image=* pagina web (hint_ip, hint_mly) e website=*/url=* voce di Wikipedia (hint_wpw)
    ("hint_ip: non per Google Foto (accettato)", {"natural": "tree", "image": "https://photos.app.goo.gl/Yqzit3vNyopEEK2q7"},
     None, "hint_ip", False),
    ("hint_ip: pagina di Google Drive", {"natural": "tree", "image": "https://drive.google.com/file/d/1OPC/view"},
     None, "hint_ip", "drive.google.com"),
    ("hint_ip: sottodominio di Wikipedia", {"natural": "tree", "image": "https://es.wikipedia.org/wiki/Pino#/media/Archivo:P.jpg"},
     None, "hint_ip", "es.wikipedia.org"),
    ("hint_ip: non per il file su i.imgur.com", {"natural": "tree", "image": "https://i.imgur.com/xhUYiJn.jpg"},
     None, "hint_ip", False),
    ("hint_ip: non per drive.google.com/uc", {"natural": "tree", "image": "https://drive.google.com/uc?id=abc"},
     None, "hint_ip", False),
    ("hint_ip: non per il file di Mapillary", {"natural": "tree", "image": "https://images.mapillary.com/R9Vb4/thumb-2048.jpg"},
     None, "hint_ip", False),
    ("hint_ip: non con più valori", {"natural": "tree", "image": "https://imgur.com/a/x;https://imgur.com/a/y"},
     None, "hint_ip", False),
    ("hint_mly: pagina di Mapillary (map/im)", {"natural": "tree", "image": "https://www.mapillary.com/map/im/uR9gcZafDIbmKH6vSeqpvg"},
     None, "hint_mly", "uR9gcZafDIbmKH6vSeqpvg"),
    ("hint_mly: pagina di Mapillary (pKey)", {"natural": "tree",
     "image": "https://www.mapillary.com/app/?lat=55.86&lng=26.54&z=17&pKey=200327278592861&focus=photo"},
     None, "hint_mly", "200327278592861"),
    ("hint_mly: non con mapillary=*", {"natural": "tree", "mapillary": "1", "image": "https://www.mapillary.com/app/?pKey=1"},
     None, "hint_mly", False),
    # ibrido della lista scritto senza × (hint_hx): vale come la specie della lista (hint_sw dà il suo QID)
    ("hint_hx: ibrido della lista senza ×", {"natural": "tree", "species": "Platanus acerifolia"}, None, "hint_hx",
     "Platanus × acerifolia"),
    ("hint_hx: senza × vale come la specie della lista", {"natural": "tree", "species": "Platanus acerifolia"}, None,
     "hint_sw", "Q920014"),
    ("hint_hx: non con il segno ×", {"natural": "tree", "species": "Platanus × acerifolia"}, None, "hint_hx", False),
    ("hint_hx: non con x (è il consiglio sul segno x)", {"natural": "tree", "species": "Platanus x acerifolia"}, None,
     "hint_hx", False),
    # specie con la forma di una specie, non nella lista wiki (hint_nl)
    ("hint_nl: specie non nella lista", {"natural": "tree", "species": "Pinus halepensis"}, None, "hint_nl", True),
    ("hint_nl: anche con il segno ×", {"natural": "tree", "species": "Tilia × euchlora"}, None, "hint_nl", True),
    ("hint_nl: anche un ibrido intergenerico", {"natural": "tree", "species": "× Chitalpa tashkentensis"}, None,
     "hint_nl", True),
    ("hint_nl: non per una specie della lista", OAK, None, "hint_nl", False),
    ("hint_nl: non per un sinonimo", {"natural": "tree", "species": "Quercus pedunculata"}, None, "hint_nl", False),
    ("hint_nl: non per un ibrido della lista senza ×", {"natural": "tree", "species": "Platanus acerifolia"}, None,
     "hint_nl", False),
    ("hint_nl: non con la cultivar", {"natural": "tree", "species": "Pinus halepensis 'Nana'"}, None, "hint_nl", False),
    ("hint_nl: non per un genere", {"natural": "tree", "species": "Pinus"}, None, "hint_nl", False),
    ("hint_nl: non con l'epiteto maiuscolo", {"natural": "tree", "species": "Pinus Halepensis"}, None, "hint_nl", False),
    ("hint_nl: non con il genere minuscolo", {"natural": "tree", "species": "pinus halepensis"}, None, "hint_nl", False),
    ("hint_nl: non con l'autore", {"natural": "tree", "species": "Pinus halepensis Mill."}, None, "hint_nl", False),
    ("hint_nl: non con più valori", {"natural": "tree", "species": "Pinus halepensis;Pinus pinea"}, None, "hint_nl", False),
    ("hint_nl: non su un ceppo", {"natural": "tree_stump", "species": "Pinus halepensis"}, None, "hint_nl", False),
]

# Proposta di species_author (sp_clean, quello che «Fix with JOSM» scrive in species): species -> valore atteso,
# None = nessuna proposta
SP_CLEAN_CASES = [
    ("Quercus robur L.", "Quercus robur"),
    ("Camellia sinensis (L.) Kuntze", "Camellia sinensis"),
    ("Taxus cuspidata Siebold et Zucc.", "Taxus cuspidata"),                     # autore senza punto prima di "et"
    ("Betula platyphylla Sukaczev var. japonica (Miq.) H.Hara", "Betula platyphylla var. japonica"),
    ("Acer amoenum Carriere var. matsumurae", "Acer amoenum var. matsumurae"),  # autore senza punto prima del rango
    ("Salix hultenii Flod. var. angustifolia Kimura", "Salix hultenii var. angustifolia"),
    ("Malus toringo (Siebold) Siebold ex de Vriese", "Malus toringo"),
    ("Pinus parviflora Siebold & Zucc. 'Glauca'", "Pinus parviflora 'Glauca'"),
    ("Prunus serrulata (Siebold & Zucc.) Lindl.", "Prunus serrulata"),           # autore tra parentesi su più parole
    ("Platanus x hispanica Mill.", "Platanus x hispanica"),
    ("Tilia X europaea L.", "Tilia × europaea"),                                 # segno di ibrido maiuscolo
    ("Cryptomeria japonica (Thunb. ex L. f.) D. Don var. elegans", "Cryptomeria japonica var. elegans"),
    ("Insieme omogeneo di Fagus sylvatica L.", None),                            # resta altro oltre al nome
]

# Correzione con JOSM di species_typo e species_author (sp_fix e i tag che mancano per la specie corretta, sp_fix_*):
# (nome, tag, proprietà attese; None = la proprietà non c'è)
SP_FIX_CASES = [
    # wikimedia_commons con l'indirizzo al posto del titolo (wc_fix: correzione di link_format in index.html)
    ("wc_fix: pagina del file, con i trattini bassi",
     {"natural": "tree", "wikimedia_commons":
      "https://commons.wikimedia.org/wiki/File:Eastern_European_Editathon,_Sofia_(_1070653).jpg"},
     {"wc_fix": "File:Eastern European Editathon, Sofia ( 1070653).jpg"}),
    ("wc_fix: indirizzo codificato", {"natural": "tree", "wikimedia_commons":
     "https://commons.m.wikimedia.org/wiki/File%3AQuercia_di_San_Zeno%C2%B2.JPG?uselang=it"},
     {"wc_fix": "File:Quercia di San Zeno².JPG"}),
    ("wc_fix: pagina di una categoria", {"natural": "tree", "wikimedia_commons":
     "https://commons.wikimedia.org/wiki/Category:Big_trees_in_Italy"}, {"wc_fix": "Category:Big trees in Italy"}),
    ("wc_fix: file aperto dalla categoria", {"natural": "tree", "wikimedia_commons":
     "https://commons.wikimedia.org/wiki/Category:Big_trees#/media/File:Old_oak.png"}, {"wc_fix": "File:Old oak.png"}),
    ("wc_fix: miniatura su upload.wikimedia.org", {"natural": "tree", "wikimedia_commons":
     "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Old_oak.jpg/800px-Old_oak.jpg"}, {"wc_fix": "File:Old oak.jpg"}),
    ("wc_fix: solo il valore sbagliato tra più valori", {"natural": "tree", "wikimedia_commons":
     "Category:Big trees;https://commons.wikimedia.org/wiki/File:Old_oak.jpg"}, {"wc_fix": "Category:Big trees;File:Old oak.jpg"}),
    ("wc_fix: niente se un valore non si sa correggere", {"natural": "tree", "wikimedia_commons":
     "Old oak.jpg;https://commons.wikimedia.org/wiki/File:Old_oak.jpg"}, {"wc_fix": None}),
    ("wc_fix: niente per un file senza estensione", {"natural": "tree", "wikimedia_commons": "File:Old oak"}, {"wc_fix": None}),
    ("wc_fix: niente per la pagina di un file senza estensione", {"natural": "tree", "wikimedia_commons":
     "https://commons.wikimedia.org/wiki/File:Old_oak"}, {"wc_fix": None}),
    ("genere abbreviato: species con il genere per esteso", {"natural": "tree", "species": "Q. robur 'Fastigiata'"},
     {"sp_full": "Quercus robur 'Fastigiata'"}),
    ("genere abbreviato che non si scioglie: nessuna proposta", {"natural": "tree", "species": "A. campestre"}, {"sp_full": None}),
    ("genere abbreviato con l'autore: la proposta è di species_author", {"natural": "tree", "species": "Q. robur L."},
     {"sp_full": None, "sp_clean": "Q. robur"}),
    ("nome comune con un genere certo in species: genus proposto", {"natural": "tree", "species": "Horse Chestnut"},
     {"sp_cn_genus": "Aesculus", "sp_sci": None}),
    ("nome comune che inizia con il nome di un genere ma non è un nome noto: nessuna proposta",
     {"natural": "tree", "species": "Quercia Grande"}, {"sp_cn_genus": None}),
    ("species:fr nome di una sottospecie: species e taxon:subspecies", {"natural": "tree", "species:fr": "Mirabellier"},
     {"sp_lang_sp": "species:fr=Mirabellier|Prunus domestica", "sp_lang_ssp": "species:fr=Mirabellier|Prunus domestica subsp. syriaca"}),
    ("species:fr nome di una sottospecie, taxon:subspecies c'è già: solo species",
     {"natural": "tree", "species:fr": "Mirabellier", "taxon:subspecies": "Prunus domestica subsp. syriaca"},
     {"sp_lang_sp": "species:fr=Mirabellier|Prunus domestica", "sp_lang_ssp": None}),
    ("species:xx nome di una specie: niente sottospecie", {"natural": "tree", "species:pl": "Klon zwyczajny"},
     {"sp_lang_ssp": None}),
    ("nome comune di una specie in species: la specie, il nome in species:xx e i tag che mancano",
     {"natural": "tree", "species": "English Oak"},
     {"sp_sci": "Quercus robur", "sp_cn_sp": 1, "sp_cn_kv": "species:en=English Oak", "sp_cn_wd": "Q920001",
      "sp_cn_lt": "broadleaved", "sp_cn_genus": None}),
    ("nome comune di una specie in species, species:en c'è già: solo la specie",
     {"natural": "tree", "species": "English Oak", "species:en": "Oak", "species:wikidata": "Q920001"},
     {"sp_sci": "Quercus robur", "sp_cn_sp": 1, "sp_cn_kv": None, "sp_cn_wd": None}),
    ("nome comune di una specie in species con un genus diverso: nessuna proposta",
     {"natural": "tree", "species": "English Oak", "genus": "Pinus"}, {"sp_sci": None, "sp_cn_sp": None}),
    ("nome comune con un genere certo (non di GENUS_NAMES): niente genus:xx",
     {"natural": "tree", "species": "Horse Chestnut"}, {"sp_cn_kv": None}),
    ("name_taxon_missing: la specie da aggiungere, con species:wikidata e foglie", {"natural": "tree", "name": "English oak"},
     {"nm_tx": "species|Quercus robur", "nm_wd": "Q920001", "nm_lt": "broadleaved", "nm_lc": "deciduous"}),
    ("name_taxon_missing: il genere da aggiungere", {"natural": "tree", "name": "Quercia"},
     {"nm_tx": "genus|Quercus", "nm_wd": None, "nm_sci": None}),
    ("name_taxon_missing: nome scientifico, la specie della lista", {"natural": "tree", "name": "quercus robur"},
     {"nm_tx": "species|Quercus robur", "nm_wd": "Q920001", "nm_sci": 1}),
    ("name_taxon_missing: nome scientifico, il genere della lista", {"natural": "tree", "name": "pinus"},
     {"nm_tx": "genus|Pinus", "nm_sci": 1}),
    ("name_taxon_mismatch: il tag diverso", {"natural": "tree", "genus": "Pinus", "name": "English oak"},
     {"nm_tx": "species|Quercus robur", "nm_tag": "genus"}),
    ("name_botanical: genere della lista", {"natural": "tree", "name:botanical": "Tilia"},
     {"nb": "Tilia", "nb_tx": "genus|Tilia", "nb_wd": None}),
    ("name_botanical: genere con sp.", {"natural": "tree", "name:botanical": "Tilia sp."},
     {"nb": "Tilia sp.", "nb_tx": "genus|Tilia"}),
    ("name_botanical: genere con spp", {"natural": "tree", "name:botanical": "Pinus spp"}, {"nb_tx": "genus|Pinus"}),
    ("name_botanical: specie della lista, con species:wikidata e foglie", {"natural": "tree", "name:botanical": "quercus robur"},
     {"nb_tx": "species|Quercus robur", "nb_wd": "Q920001", "nb_lt": "broadleaved", "nb_lc": "deciduous"}),
    ("name_botanical: specie fuori lista con la forma di una specie", {"natural": "tree", "name:botanical": "Tilia cordata"},
     {"nb_tx": "species|Tilia cordata|b"}),
    ("name_botanical: species uguale, niente tag da aggiungere", {**OAK, "name:botanical": "Quercus robur"},
     {"nb_tx": "species|Quercus robur", "nb_wd": None}),
    ("name_botanical: genere di species", {**OAK, "name:botanical": "Quercus sp."}, {"nb_tx": "genus|Quercus"}),
    ("name_botanical: species di un'altra specie, nessuna proposta", {**OAK, "name:botanical": "Quercus suber"},
     {"nb": "Quercus suber", "nb_tx": None}),
    ("name_botanical: genus di un altro genere, nessuna proposta", {"natural": "tree", "genus": "Pinus",
     "name:botanical": "Tilia"}, {"nb_tx": None}),
    ("name_botanical: nome sconosciuto, nessuna proposta", {"natural": "tree", "name:botanical": "Fagaceae"},
     {"nb": "Fagaceae", "nb_tx": None}),
    ("species di una parola, nome comune di un genere: genus e genus:xx", {"natural": "tree", "species": "świerk"},
     {"sp_common_genus": "Picea", "sp_common_kv": "genus:pl=Świerk"}),
    ("species di una parola, nome comune di un genere, genus:xx c'è già: niente genus:xx",
     {"natural": "tree", "species": "Quercia", "genus:it": "Quercia"},
     {"sp_common_genus": "Quercus", "sp_common_kv": None}),
    ("genus nome comune minuscolo: genus e genus:xx con la maiuscola", {"natural": "tree", "genus": "świerk"},
     {"g_common": "Picea", "g_common_lang": "pl", "g_common_kv": "genus:pl=Świerk"}),
    ("genus nome comune, genus:xx c'è già: genus:xx non si propone", {"natural": "tree", "genus": "świerk",
     "genus:pl": "Świerk"}, {"g_common": "Picea", "g_common_kv": None}),
    ("species:wikidata di rango genere: il genere da spostare", {"natural": "tree", "species:wikidata": "Q920003"},
     {"wd_rank_g": "Quercus"}),
    ("species:wikidata di rango genere con un altro genus: nessuna proposta",
     {"natural": "tree", "species:wikidata": "Q920003", "genus": "Pinus"}, {"wd_rank_g": None}),
    ("species:wikidata di rango genere con genus:wikidata: nessuna proposta",
     {"natural": "tree", "species:wikidata": "Q920003", "genus:wikidata": "Q920005"}, {"wd_rank_g": None}),
    ("maiuscole: nome della lista", {"natural": "tree", "species": "QUERCUS ROBUR"}, {"sp_sci": "Quercus robur"}),
    ("maiuscole: nome della lista, la cultivar resta", {"natural": "tree", "species": "Quercus Robur 'Fastigiata'"},
     {"sp_sci": "Quercus robur 'Fastigiata'"}),
    ("maiuscole: nome della lista, il resto resta com'è", {"natural": "tree", "species": "Quercus X Robur Fastigiata"},
     {"sp_sci": "Quercus robur Fastigiata"}),
    ("maiuscole: genere noto ed epiteto della lista", {"natural": "tree", "species": "Tilia Pinea"},
     {"sp_sci": "Tilia pinea"}),
    ("maiuscole: genere noto, la seconda parola non è un epiteto: nessuna proposta",
     {"natural": "tree", "species": "Tilia Greenspire"}, {"sp_sci": None}),
    ("maiuscole e segno di ibrido in fondo: il nome della lista", {"natural": "tree", "species": "Platanus Acerifolia x"},
     {"sp_sci": "Platanus × acerifolia"}),
    ("maiuscole e segno di ibrido in fondo, non un ibrido nella lista: il segno resta",
     {"natural": "tree", "species": "Quercus Robur x"}, {"sp_sci": "Quercus robur x", "sp_hyb": None}),
    ("segno di ibrido in fondo: il nome della lista", {"natural": "tree", "species": "Platanus acerifolia x"},
     {"sp_hyb": "Platanus × acerifolia"}),
    ("segno di ibrido in fondo, iniziale minuscola: il nome della lista", {"natural": "tree", "species": "platanus acerifolia X"},
     {"sp_hyb": "Platanus × acerifolia"}),
    ("segno di ibrido in fondo, non un ibrido nella lista: nessuna proposta", {"natural": "tree", "species": "Quercus robur x"},
     {"sp_hyb": None}),
    ("segno di ibrido in fondo e refuso, la specie della lista è un ibrido: la correzione del refuso",
     {"natural": "tree", "species": "Platanus acerifolai x"}, {"sp_fix": "Platanus × acerifolia", "sp_fix_wd": "Q920014",
                                                              "sp_hyb": None}),
    ("segno di ibrido in fondo e refuso, la specie della lista non è un ibrido: nessuna correzione",
     {"natural": "tree", "species": "Quercus rubur x"}, {"sp_fix": None}),
    ("refuso: species della lista e i tag che mancano", {"natural": "tree", "species": "Quercus rubur"},
     {"sp_fix": "Quercus robur", "sp_fix_wd": "Q920001", "sp_fix_lt": "broadleaved", "sp_fix_lc": "deciduous"}),
    ("refuso con l'autore: la stessa species per i due errori", {"natural": "tree", "species": "Quercus rubur L.",
                                                                "leaf_type": "broadleaved"},
     {"sp_clean": "Quercus rubur", "sp_fix": "Quercus robur", "sp_fix_wd": "Q920001", "sp_fix_lt": None}),
    ("refuso con la cultivar: nessuna correzione", {"natural": "tree", "species": "Quercus rubur 'Fastigiata'"},
     {"sp_fix": None}),
    ("refuso con le stesse lettere in un altro ordine: quella specie, non la più vicina",
     {"natural": "tree", "species": "Prunus serratula"}, {"sp_fix": "Prunus serrulata"}),
    ("circumference non leggibile: il limite da diameter", {"natural": "tree", "circumference": "big", "diameter": "20000"},
     {"diam_used": True}),
    ("solo l'autore: species:wikidata della specie senza autore",
     {"natural": "tree", "species": "Quercus robur L.", "species:wikidata": "Q920001"},
     {"sp_clean": "Quercus robur", "sp_fix": None, "sp_fix_wd": None, "sp_fix_lc": "deciduous"}),
    ("solo l'autore, senza species:wikidata", {"natural": "tree", "species": "Quercus robur L."},
     {"sp_fix_wd": "Q920001"}),
    # species di una parola che con genus è una specie della lista (sp_binom) e i tag che mancano (sp_bin_*)
    ("genus ed epiteto maiuscolo: la specie della lista", {"natural": "tree", "genus": "Pinus", "species": "Pinea"},
     {"sp_binom": "Pinus pinea", "sp_bin_wd": "Q920002", "sp_bin_lt": "needleleaved", "sp_bin_lc": "evergreen"}),
    ("genus ed epiteto minuscolo: la specie della lista", {"natural": "tree", "genus": "Quercus", "species": "robur",
                                                          "leaf_type": "broadleaved"},
     {"sp_binom": "Quercus robur", "sp_bin_lt": None}),
    ("genus ed epiteto dopo il segno di ibrido: l'ibrido della lista",
     {"natural": "tree", "genus": "Platanus", "species": "x acerifolia"}, {"sp_binom": "Platanus × acerifolia"}),
    ("genus ed epiteto che non sono una specie della lista: nessuna proposta",
     {"natural": "tree", "genus": "Quercus", "species": "Pinea"}, {"sp_binom": None}),
    ("species di una parola senza genus: nessuna proposta", {"natural": "tree", "species": "robur"}, {"sp_binom": None}),
    ("species di una parola che è un genere, senza genus: sp_as_genus", {"natural": "tree", "species": "Pinus"},
     {"sp_binom": None, "sp_as_genus": "Pinus"}),
    ("species di una parola che è lo stesso genere di genus: sp_as_genus",
     {"natural": "tree", "genus": "Pinus", "species": "pinus"}, {"sp_as_genus": "Pinus"}),
    ("species di una parola che è un altro genere: niente sp_as_genus né sp_binom",
     {"natural": "tree", "genus": "Quercus", "species": "Pinus"}, {"sp_binom": None, "sp_as_genus": None}),
    # altre proposte delle correzioni con JOSM: genus (gm_fix, g_common) e il rango della voce di wikipedia (wp_genus)
    ("genus scritto male: genus di species", {**OAK, "genus": "Quercos"}, {"gm_fix": "Quercus"}),
    ("genus diverso, ma un genere noto: nessuna correzione", {**OAK, "genus": "Pinus"}, {"gm_fix": None}),
    ("genus diverso e lontano: nessuna correzione", {**OAK, "genus": "Fraxinus"}, {"gm_fix": None}),
    ("genus nome comune: il genere e la lingua", {"natural": "tree", "genus": "quercia"},
     {"g_common": "Quercus", "g_common_lang": "it"}),
    ("wikipedia di un genere: wp_genus", {"natural": "tree", "wikipedia": "it:Quercus"}, {"wp_genus": 1}),
    ("wikipedia di una specie: niente wp_genus", {"natural": "tree", "wikipedia": "it:Quercus_robur"}, {"wp_genus": None}),
    # taxon:xx senza taxon (taxon_lang_moves): righe tx_lang_mv e tag da aggiungere tx_lang_add
    ("taxon:it nome di un genere: genus:it e genus", {"natural": "tree", "taxon:it": "quercia"},
     {"tx_lang_mv": "taxon:it=quercia|g|Quercus|genus:it=Quercia", "tx_lang_add": "genus=Quercus"}),
    ("taxon:it nome di un genere, genus c'è già", {"natural": "tree", "taxon:it": "quercia", "genus": "Quercus"},
     {"tx_lang_mv": "taxon:it=quercia|g|Quercus|genus:it=Quercia", "tx_lang_add": None}),
    ("taxon:it nome di un genere, species dello stesso genere: niente genus", {**OAK, "taxon:it": "Quercia"},
     {"tx_lang_mv": "taxon:it=Quercia|g|Quercus|genus:it=Quercia", "tx_lang_add": None}),
    ("taxon:it nome di un genere, species di un altro genere: niente", {"natural": "tree", "species": "Pinus pinea",
                                                                      "taxon:it": "quercia"}, {"tx_lang_mv": None}),
    ("taxon:it nome di un genere, genus:it diverso: niente", {"natural": "tree", "taxon:it": "quercia", "genus:it": "Rovere"},
     {"tx_lang_mv": None, "tx_lang_add": None}),
    ("taxon:it nome di una specie: species:it, species e i tag che mancano", {"natural": "tree", "taxon:it": "bagolaro"},
     {"tx_lang_mv": "taxon:it=bagolaro|s|Celtis australis|species:it=Bagolaro", "tx_lang_add": "species=Celtis australis"}),
    ("taxon:en con species già taggata: species:en", {**OAK, "taxon:en": "Pedunculate oak"},
     {"tx_lang_mv": "taxon:en=Pedunculate oak|p||species:en=Pedunculate oak", "tx_lang_add": None}),
    ("taxon:en con species e species:en diverso: niente", {**OAK, "taxon:en": "Oak", "species:en": "Pedunculate oak"},
     {"tx_lang_mv": None}),
    ("taxon:en nome sconosciuto senza species: niente", {"natural": "tree", "taxon:en": "Oak"}, {"tx_lang_mv": None}),
]

# Nomi dei generi esclusi in una regione (genus_names_except): (nome, tag, codice ISO della regione, proprietà attese)
GENUS_NAME_CASES = [
    ("species:en=plane: genus=Platanus", {"natural": "tree", "species:en": "plane"}, "GB-WLS",
     {"sp_lang_genus": "species:en=plane|Platanus"}),
    ("species:en=plane: niente consiglio in Scozia", {"natural": "tree", "species:en": "plane"}, "GB-SCT",
     {"sp_lang_genus": None}),
    ("species=plane: genus=Platanus", {"natural": "tree", "species": "plane"}, "GB-WLS", {"sp_common_genus": "Platanus"}),
    ("species=plane: niente consiglio in Scozia", {"natural": "tree", "species": "plane"}, "GB-SCT",
     {"sp_common_genus": None}),
]

# Frasi sugli anni (year_event): (chiave, codice ISO della regione, voci, atteso)
_EV = {"1159": "tutti", "JP:1159": "Giappone", "1150s": "decennio", "FR:1160s": "Francia, decennio",
       "C16": "secolo", "IT-21:C16": "Piemonte, secolo", "IT:C16": "Italia, secolo"}
YEAR_EVENT_CASES = [
    ("1159", "", _EV, "tutti"), ("1159", "JP", _EV, "Giappone"), ("1159", "IT", _EV, "tutti"),
    ("1153", "JP", _EV, "decennio"), ("1163", "FR", _EV, "Francia, decennio"), ("1163", "IT", _EV, None),
    ("1159", "JP", {"JP:1150s": "Giappone, decennio", "1159": "tutti"}, "tutti"),   # prima l'anno, poi il decennio
    ("C16", "IT-21", _EV, "Piemonte, secolo"), ("C16", "IT-25", _EV, "Italia, secolo"), ("C16", "FR", _EV, "secolo"),
    ("1163", "FR-ARA", _EV, "Francia, decennio"),                                   # la regione eredita dal paese
]

# Relazioni con ref:masaf (masaf_groups): elementi finti di una risposta di Overpass -> righe "mg" attese per nodo
_REL = lambda i, tags, members: {"type": "relation", "id": i, "tags": tags,
                                  "members": [{"type": k, "ref": r, "role": ""} for k, r in members]}
GROUP_ELEMENTS = [
    # insieme omogeneo come dice la wiki, con due codici (uno rimosso): membri due nodi, una via e una relazione
    _REL(10, {"type": "site", "natural": "tree_group", "denotation": "natural_monument", "ref:masaf": f"{LISTED};{REMOVED}",
              "masaf:criteria": "a1,a2.3", "name": "Glicini | del chiostro"},
         [("node", 1), ("node", 2), ("way", 3), ("relation", 4)]),
    _REL(11, {"ref:masaf": LISTED}, [("node", 1)]),                          # solo ref:masaf: vale lo stesso
    _REL(12, {"type": "site", "denotation": "natural_monument"}, [("node", 5)]),   # senza ref:masaf: niente
    _REL(13, {"ref:masaf": LISTED}, [("way", 6)]),                           # senza nodi membri: niente
]
GROUP_EXPECTED = {1: [f"10|1|{LISTED};{REMOVED}|a1,a2.3|Glicini | del chiostro", f"11|0|{LISTED}||"],
                  2: [f"10|1|{LISTED};{REMOVED}|a1,a2.3|Glicini | del chiostro"]}


def feature_failures():
    """Membri di un insieme omogeneo nelle feature (to_feature, minimal) e lettura delle relazioni (masaf_groups)."""
    out = []
    ft.IN_ITALY, ft.COUNTRY = True, "IT"
    for name, tags, osm_id, key, want in HINT_CASES:
        val = ft.hint_props(dict(tags), osm_id, POS).get(key)
        got = (val is not None) if isinstance(want, bool) else val
        if got != want:
            out.append(f"FALLITO: {name}\n  tag:     {tags}\n  atteso:  {key}={want}\n  uscito:  {key}={val}")
    for sp, want in SP_CLEAN_CASES:
        if (got := ft.checks({"natural": "tree", "species": sp}, POS)[1].get("sp_clean")) != want:
            out.append(f"FALLITO: sp_clean di species={sp}\n  atteso:  {want}\n  uscito:  {got}")
    for name, tags, iso, want in GENUS_NAME_CASES:
        ft.REGION_ISO, ft.COUNTRY = iso, iso[:2]
        props = ft.checks(dict(tags), POS)[1]
        if bad := {k: props.get(k) for k, v in want.items() if props.get(k) != v}:
            out.append(f"FALLITO: {name}\n  tag:     {tags}\n  atteso:  {want}\n  uscito:  {bad}")
    ft.REGION_ISO, ft.COUNTRY = "IT", "IT"
    for name, tags, want in SP_FIX_CASES:
        props = ft.checks(dict(tags), POS)[1]
        if bad := {k: props.get(k) for k, v in want.items() if props.get(k) != v}:
            out.append(f"FALLITO: {name}\n  tag:     {tags}\n  atteso:  {want}\n  uscito:  {bad}")
    for key, cc, events, want in YEAR_EVENT_CASES:
        if (got := ft.year_event(key, events, cc)) != want:
            out.append(f"FALLITO: year_event({key!r}, nazione {cc!r})\n  atteso:  {want}\n  uscito:  {got}")
    if (got := ft.masaf_groups(GROUP_ELEMENTS)) != GROUP_EXPECTED:
        out.append(f"FALLITO: masaf_groups\n  atteso:  {GROUP_EXPECTED}\n  uscito:  {got}")
    node = lambda i, tags: {"type": "node", "id": i, "lat": POS[1], "lon": POS[0], "tags": tags}
    tags = {**OAK, "denotation": "park", "circumference": "5"}
    try:
        mem, other = ft.to_feature(node(MEMBER, tags))["properties"], ft.to_feature(node(9002, tags))["properties"]
        stump = ft.to_feature(node(MEMBER, {"natural": "tree_stump"}))["properties"]
        small = ft.minimal({"features": [{"properties": mem}, {"properties": other}]})
    except Exception as e:
        return out + [f"ERRORE: to_feature dei membri si è fermata: {type(e).__name__}: {e}"]
    # uscita rapida degli alberi con il solo tag natural (bare_feature): stesse proprietà, nello stesso ordine, del
    # percorso completo; in Italia e fuori, membro di un insieme omogeneo o no, con e senza data
    for nat in ("tree", "tree_stump"):
        for i in (MEMBER, 9002, 3000000000):
            for italy, extra in ((True, {}), (False, {"timestamp": "2026-11-15T10:00:00Z"})):
                ft.IN_ITALY, ft.COUNTRY = italy, "IT" if italy else "FR"
                el = {**node(i, {"natural": nat}), **extra}
                try:
                    fast, full = ft.to_feature(el), ft.to_feature(el, fast=False)
                except Exception as e:
                    return out + [f"ERRORE: to_feature di natural={nat} si è fermata: {type(e).__name__}: {e}"]
                if list(fast["properties"].items()) != list(full["properties"].items()) or fast["geometry"] != full["geometry"]:
                    out.append(f"FALLITO: uscita rapida di natural={nat} (id {i}) diversa dal percorso completo: aggiornare "
                               f"bare_feature in fetch_trees.py\n  rapida:    {fast['properties']}\n"
                               f"  completa:  {full['properties']}")
    ft.IN_ITALY, ft.COUNTRY = True, "IT"
    # filari (tree_rows, issue #56): senza alberi tra i nodi, con leaf_type/leaf_cycle dal tag o stimati
    g = lambda *pts: [{"lon": x, "lat": y} for x, y in pts]
    way = lambda i, tags, nodes, geom: {"type": "way", "id": i, "tags": {"natural": "tree_row", **tags}, "nodes": nodes, "geometry": geom}
    row_els = [node(500, {"natural": "tree"}), node(501, {"natural": "tree_stump"}), node(502, {"barrier": "gate"}),
               way(1, {"species": "Pinus pinea"}, [502, 503], g((12, 42), (12.001, 42))),          # stima dalla specie
               way(2, {}, [500, 504], g((12, 42), (12.001, 42))),                                  # albero tra i nodi
               way(3, {}, [501, 504], g((12, 42), (12.001, 42))),                                  # ceppo tra i nodi
               way(4, {"leaf_type": "leafless", "leaf_cycle": "deciduous"}, [505, 506], g((12, 42), (12, 42.001))),
               way(5, {"genus": "Phoenix"}, [507, 508], g((12, 42), (12, 42))),                    # un solo punto
               way(6, {"genus": "Phoenix", "leaf_type": "broadleaved"}, [509, 510], g((12, 42), (12, 42.001)))]
    rows = ft.tree_rows(row_els)
    want_rows = [{"id": 1, "c": [[12, 42], [12.001, 42]], "lt": "needleleaved", "lc": "evergreen"},
                 {"id": 4, "c": [[12, 42], [12, 42.001]], "lc": "deciduous"},
                 {"id": 6, "c": [[12, 42], [12, 42.001]], "lt": "palm"}]
    if rows != want_rows:
        out.append(f"FALLITO: tree_rows\n  atteso:  {want_rows}\n  uscito:  {rows}")
    if "rows" in ft.build({"elements": [node(500, {"natural": "tree"})]}):
        out.append("FALLITO: build senza filari: niente membro rows")
    checks = [("exp_genus solo insieme a un errore", ft.checks({"natural": "tree", "genus": "Quercus"}, POS)[1] == {}
               and ft.checks({"natural": "tree", "genus": "Pinus", "leaf_type": "broadleaved"}, POS)[1].get("exp_genus") is True),
              ("sacred_tree=no non è un albero sacro", not ft.is_sacred({"sacred_tree": "no"})
               and ft.is_sacred({"sacred_tree": "rag_tree"})),
              ("file ridotto: natural_monument dopo uno spazio", b'"id":7' in ft.minimal({"features": [{"properties": {
                  "id": 7, "issues": "|", "denotation": "urban; natural_monument"}}]})),
              ("membro: proprietà mg", mem.get("mg") == FIXTURES["MASAF_GROUPS"][MEMBER][0]),
              # la chioma di ripiego la ricava il sito (fallbackCrown in index.html): nei dati "cr" c'è solo con diameter_crown
              ("chioma di ripiego: niente cr né cr_est", not {"cr", "cr_est"} & (set(mem) | set(other))),
              ("diameter_crown: cr = metà, senza cr_est", (pc := ft.to_feature(node(9002, {**tags, "diameter_crown": "8"}))["properties"])
               .get("cr") == 4.0 and "cr_est" not in pc),
              ("membro: niente hint_ms", "hint_ms" not in mem),
              # consigli sulla lista wiki (hint_nl, hint_hx): non con un errore sul nome, che propone già una correzione
              ("hint_nl nella feature", ft.to_feature(node(9013, {"natural": "tree", "species": "Pinus halepensis"}))
               ["properties"].get("hint_nl") == 1),
              ("hint_nl: non con un errore sul nome", "hint_nl" not in ft.to_feature(node(9014, {"natural": "tree",
               "species": "Agave americana"}))["properties"]),
              ("sp_key: ibrido senza × contato con la specie della lista", ft.to_feature(node(9015, {"natural": "tree",
               "species": "Platanus acerifolia"}))["properties"].get("sp_key") == "Platanus × acerifolia"),
              # ref=* per il titolo della scheda (issue #89)
              ("ref nella feature", ft.to_feature(node(9008, {**tags, "ref": "1543"}))["properties"].get("ref") == "1543"),
              # TROBI e champion trees: tag per la riga ★ della scheda
              ("TROBI e champion nella feature", all(ft.to_feature(node(9011, {**tags, **ch}))["properties"].get(k) == v
                  for ch in [{"ref:TROBI": "107129", "ref:trobi": "107129", "division:champion": "yes",
                              "champion:territory": "Ireland and Britain", "champion:category": "Girth/Height",
                              "champion:date": "2012"}] for k, v in ch.items())),
              # foto di Google Foto (cache gphotos.json finta): gp_img solo con un indirizzo letto
              ("Google Foto: gp_img dalla cache", (ft.GPHOTOS.update({"https://photos.app.goo.gl/x": {"i": "https://lh3.googleusercontent.com/pw/a", "d": "2026-11-16"},
                                                                      "https://photos.app.goo.gl/y": {"i": "", "d": "2026-11-16"}})
                                                   or ft.to_feature(node(9003, {**tags, "image": "https://photos.app.goo.gl/x"}))["properties"]
                                                   .get("gp_img") == "https://lh3.googleusercontent.com/pw/a")
               and "gp_img" not in ft.to_feature(node(9004, {**tags, "image": "https://photos.app.goo.gl/y"}))["properties"]),
              ("Google Foto: og:image senza la dimensione", ft.GPHOTOS_OG.search(
                  '<meta property="og:image" content="https://lh3.googleusercontent.com/pw/AP1G-x_y=w600-h315-p-k">').group(1)
               == "https://lh3.googleusercontent.com/pw/AP1G-x_y"),
              ("non membro: niente mg, hint_ms", "mg" not in other and "hint_ms" in other),
              # circonferenza oltre il limite della specie (o generico, anche da diameter): solo l'errore, niente hint_ms
              ("circumference_species: niente hint_ms", all("hint_ms" not in ft.to_feature(node(9012, {**OAK, **m}))["properties"]
                  for m in ({"circumference": "20"}, {"circumference": "50"}, {"diameter": "20000"}))),
              ("ceppo membro: resta un ceppo, senza mg", stump.get("stump") == 1 and "mg" not in stump),
              ("file ridotto: il membro c'è, l'albero qualsiasi no", f'"id":{MEMBER}' in small.decode() and '"id":9002' not in small.decode())]
    # palme (#55): disegnate come palme (lt=palm), ma il leaf_type da proporre è quello della lista (lts), mai palm
    palm = ft.to_feature(node(9005, {"natural": "tree", "genus": "Trachycarpus"}))["properties"]
    palm_tagged = ft.to_feature(node(9006, {"natural": "tree", "genus": "Trachycarpus", "leaf_type": "palm"}))["properties"]
    palm_nolist = ft.to_feature(node(9007, {"natural": "tree", "genus": "Washingtonia"}))["properties"]
    checks += [("palma senza leaf_type: lt=palm, lts dalla lista", palm.get("lt") == "palm" and palm.get("lts") == "broadleaved"),
               ("palma con leaf_type=palm: niente lt né lts", not {"lt", "lts"} & set(palm_tagged)),
               ("palma fuori lista: lt=palm, niente lts né lgk=t", palm_nolist.get("lt") == "palm" and "lts" not in palm_nolist
                and "t" not in palm_nolist.get("lgk", "")),
               # foglie stimate da taxon senza species né genus (leaf_tags); non con un genus diverso
               ("taxon specie: lt e lc dalla specie", (lambda q: q.get("lt") == "broadleaved" and q.get("lc") == "deciduous"
                and "lg" not in q)(ft.to_feature(node(9013, {"natural": "tree", "taxon": "Quercus robur"}))["properties"])),
               ("taxon genere: lt e lc dal genere, lg", (lambda q: q.get("lt") == "needleleaved" and q.get("lc") == "evergreen"
                and q.get("lg") == "Pinus")(ft.to_feature(node(9014, {"natural": "tree", "taxon": "Pinus"}))["properties"])),
               ("taxon con genus: valgono i tag", ft.to_feature(node(9015, {"natural": "tree", "taxon": "Quercus robur",
                "genus": "Pinus"}))["properties"].get("lt") == "needleleaved"),
               ("taxon: i check sulle foglie non lo leggono", "leaf_type_mismatch" not in ft.to_feature(node(9016, {
                "natural": "tree", "taxon": "Pinus", "leaf_type": "broadleaved"}))["properties"]["issues"]),
               ("palma: species_lang_extras propone broadleaved",
                ft.species_lang_extras({"natural": "tree"}, "Trachycarpus fortunei").get("sp_lang_lt") == "broadleaved")]
    # data dell'albero per «Più vecchio» (issue #81): start_date o, se manca, planted_date
    sd_pd = ft.to_feature(node(9009, {"natural": "tree", "start_date": "1900", "planted_date": "1850"}))["properties"]
    pd_only = ft.to_feature(node(9010, {"natural": "tree", "planted_date": "~1850"}))["properties"]
    checks += [("planted_date nella feature", pd_only.get("planted_date") == "~1850"),
               ("Più vecchio: planted_date senza start_date", ft.record_values(pd_only).get("oldest") == 1850
                and ft.tree_date(pd_only) == "~1850"),
               ("Più vecchio: con tutte e due conta start_date", ft.record_values(sd_pd).get("oldest") == 1900
                and ft.tree_date_key(sd_pd) == "start_date"),
               ("data giuliana convertita nel calendario gregoriano", ft.start_date_key("j:1918-01-31") == 19180213
                and ft.start_date_key("j:1700-02-29") == 17000311 and ft.start_date_key("j:1850") == 18500000),
               ("data giuliana: anno gregoriano per le frasi sull'anno", ft.start_date_event_key("j:1700-12-25") == "1701"
                and ft.start_date_precision("j:1918-01-31") == 0),
               ("Più vecchio: ⚠ da tag_in_value su planted_date", ft.record_warned("oldest", {
                   "planted_date": "planted_date=1850", "issues": "|tag_in_value|", "eq_keys": "planted_date"}))]
    # codici MASAF confrontati tra gli alberi (masaf_code_checks, issue #95): LISTED è un albero singolo, GROUP_CODE un
    # filare singolo. Con lo stesso punto c'è anche same_point: qui conta solo masaf_*
    def masaf_build(els, groups=None):
        saved, ft.MASAF_GROUPS = ft.MASAF_GROUPS, groups if groups is not None else {}
        try:
            return {f["properties"]["id"]: f["properties"] for f in ft.build({"elements": els})["features"]}
        finally:
            ft.MASAF_GROUPS = saved
    has = lambda p, c: f"|{c}|" in p["issues"]
    mk = lambda i, code, nat="tree": node(i, {"natural": nat, "denotation": "natural_monument", "ref:masaf": code})
    fb = masaf_build([mk(9101, LISTED), mk(9102, f"{LISTED};{REMOVED}"), mk(9103, LISTED, "tree_stump"),
                      mk(9104, GROUP_CODE), mk(9105, REMOVED)])
    fb2 = masaf_build([mk(9104, GROUP_CODE), mk(9106, GROUP_CODE)])
    fb_rel = masaf_build([mk(9104, GROUP_CODE)], {9999: [f"1|0|{GROUP_CODE}||Filare"]})
    ft.IN_ITALY = False
    fb_out = masaf_build([mk(9104, GROUP_CODE)])
    ft.IN_ITALY = True
    checks += [("masaf_shared: albero singolo su due alberi", has(fb[9101], "masaf_shared") and fb[9101].get("msh") == f"{LISTED}|9102"
                and fb[9102].get("msh") == f"{LISTED}|9101"),
               ("masaf_shared: il ceppo non conta e non è segnalato", not has(fb[9103], "masaf_shared")
                and "9103" not in fb[9101].get("msh", "")),
               ("masaf_shared: non per un codice senza tipo", not has(fb[9105], "masaf_shared")),
               ("masaf_group_single: filare su un solo albero", has(fb[9104], "masaf_group_single")
                and fb[9104].get("mgs") == f"{GROUP_CODE}|filare singolo" and not has(fb[9104], "masaf_shared")),
               ("masaf_group_single: non su due alberi", not any(has(p, "masaf_group_single") for p in fb2.values())),
               ("masaf_group_single: non con una relazione con il codice", not has(fb_rel[9104], "masaf_group_single")),
               ("masaf_group_single: non fuori dall'Italia", not has(fb_out[9104], "masaf_group_single")),
               ("monument=tree nel file ridotto", b'"id":8' in ft.minimal({"features": [{"properties": {
                   "id": 8, "issues": "|", "monument": "tree"}}]}))]
    out += [f"FALLITO: {n}" for n, ok in checks if not ok]
    return out


def known_codes():
    """Codici che checks() può segnalare, letti dal codice: flag("…") in checks() e issues.append("…") in
    species_list_mismatches()."""
    return (set(re.findall(r'flag\("(\w+)"', inspect.getsource(ft.checks)))
            | set(re.findall(r'issues\.append\("(\w+)"\)', inspect.getsource(ft.species_list_mismatches))))


def install_fixtures():
    for name, value in FIXTURES.items():
        if not hasattr(ft, name):
            sys.exit(f"test_checks.py: fetch_trees.py non ha più la variabile {name}: aggiornare FIXTURES")
        setattr(ft, name, value)
    for f in vars(ft).values():       # risultati memorizzati con le tabelle vere
        if hasattr(f, "cache_clear"):
            f.cache_clear()


def main():
    for stream in (sys.stdout, sys.stderr):   # nomi e tag con lettere non ASCII, anche su una console Windows
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    install_fixtures()
    failures, covered, names = [], set(), set()
    for name, tags, expected, *rest in CASES:
        if name in names:
            failures.append(f"CASO DOPPIO: {name}\n  due casi hanno lo stesso nome")
        names.add(name)
        # nazione della regione (check register_country), o il codice completo della regione (GB-SCT)
        ft.REGION_ISO = rest[1] if len(rest) > 1 else COUNTRY
        ft.COUNTRY = ft.REGION_ISO[:2]
        try:
            got = ft.checks(dict(tags), POS, rest[0] if rest else None)[0]
        except Exception as e:   # un errore in un caso non ferma gli altri
            failures.append(f"ERRORE: {name}\n  tag:    {tags}\n  checks() si è fermata: {type(e).__name__}: {e}")
            continue
        covered.update(expected)
        if sorted(got) != sorted(expected):
            lines = [f"FALLITO: {name}", f"  tag:     {tags}", f"  atteso:  {sorted(expected) or 'nessun errore'}",
                     f"  uscito:  {sorted(got) or 'nessun errore'}"]
            if missing := sorted(set(expected) - set(got)):
                lines.append(f"  manca:   {', '.join(missing)}")
            if extra := sorted(set(got) - set(expected)):
                lines.append(f"  in più:  {', '.join(extra)}")
            if len(got) != len(set(got)):
                lines.append("  un codice è uscito due volte")
            failures.append("\n".join(lines))
    failures += feature_failures()
    # data dei dati di una risposta di Overpass (osm_base): solo nella forma attesa, altrimenti ""
    for data, want in (({"osm3s": {"timestamp_osm_base": "2026-11-15T10:00:00Z"}}, "2026-11-15T10:00:00Z"),
                       ({"osm3s": {"timestamp_osm_base": "15/11/2026"}}, ""), ({"elements": []}, "")):
        if ft.osm_base(data) != want:
            failures.append(f"FALLITO: osm_base\n  dati:    {data}\n  atteso:  {want!r}\n  uscito:  {ft.osm_base(data)!r}")
    codes = known_codes()
    if uncovered := sorted(codes - covered):
        failures.append("CHECK SENZA CASI: " + ", ".join(uncovered)
                        + "\n  checks() può segnalarli, ma nessun caso di CASES se li aspetta: aggiungere un caso")
    if unknown := sorted(covered - codes):
        failures.append("CODICI SCONOSCIUTI: " + ", ".join(unknown)
                        + "\n  sono attesi da un caso di CASES, ma checks() non li segnala più")
    if failures:
        print("\n\n".join(failures))
        print(f"\nTest dei check: {len(failures)} problemi su {len(CASES)} casi e {len(codes)} check")
        sys.exit(1)
    print(f"Test dei check: {len(CASES)} casi, {len(codes)} check, {len(HINT_CASES)} casi dei consigli, "
          "insiemi omogenei MASAF: tutto a posto")


if __name__ == "__main__":
    main()
