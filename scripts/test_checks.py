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

HINT_CASES prova i consigli hint_ms e hint_wg; GROUP_ELEMENTS e feature_failures la lettura delle relazioni con ref:masaf
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
LISTED = "002/A002/RM/02"   # codice MASAF finto, iscritto
MEMBER = 9001               # id di un nodo membro di un insieme omogeneo MASAF (FIXTURES["MASAF_GROUPS"])

_SPECIES = {
    "Quercus robur": {"wikidata": "Q920001", "leaf_type": "broadleaved", "leaf_cycle": "deciduous"},
    "Pinus pinea": {"wikidata": "Q920002", "leaf_type": "needleleaved", "leaf_cycle": "evergreen"},
}
_GENERA = {
    "Quercus": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous", "evergreen"]},
    "Pinus": {"leaf_type": "needleleaved", "leaf_cycle": ["evergreen"]},
    "Tilia": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous"]},
    "Magnolia": {"leaf_type": "broadleaved", "leaf_cycle": ["deciduous", "evergreen"]},
    "Yucca": {"leaf_type": "broadleaved", "leaf_cycle": ["evergreen"]},
    "Cycas": {"leaf_type": "needleleaved", "leaf_cycle": ["evergreen"]},
}
_DEN = {"natural_monument": 50000, "urban": 100000, "avenue": 50000, "park": 20000, "rarevalue": 3}

# nome della variabile di fetch_trees.py -> valore finto
FIXTURES = {
    "SPECIES_LIMITS": {"quercus robur": {"circumference": 15.0, "height": 45.0, "height_circ_ratio": 10.0}},
    "SPECIES_REF": {ft.norm_species(k): v for k, v in _SPECIES.items()},
    "QID_TO_SPECIES": {v["wikidata"]: k for k, v in _SPECIES.items()},
    "SPECIES_BIN": {ft.binomial(k): k for k in _SPECIES},
    "GENUS_REF": {k.casefold(): (k, v) for k, v in _GENERA.items()},
    "KNOWN_GENERA": {k.casefold() for k in _GENERA},
    # eccezioni (come in whitelist.json)
    "WD_WHITELIST": {"quercus robur": {"Q920009"}},
    "TYPO_WHITELIST": {"quercus robar"},
    "SYNONYMS": {"quercus pedunculata": "Quercus robur"},
    "GROUP_NAME": {"quercus pedunculata": "Quercus robur", "quercus robur": "Quercus robur"},
    "SMALL_MON_WHITELIST": {4242},
    "GENUS_NAMES": {("fr", "tilleul"): "Tilia", ("it", "quercia"): "Quercus", ("en", "plane"): "Platanus"},
    "GENUS_NAMES_EXCEPT": {("en", "plane"): {"GB-SCT"}},
    "SPECIES_NAMES": {("pl", "klon zwyczajny"): "Acer platanoides", ("it", "bagolaro"): "Celtis australis"},
    "SPECIES_NAME_FORM": {("pl", "klon zwyczajny"): "Klon zwyczajny", ("it", "bagolaro"): "Bagolaro"},
    "SINGLE_SPECIES": {},
    "NOT_TREES": {"Agave americana": "Q920007"},
    "LEAF_TYPE_GENERA": {"cycas": {"needleleaved", "broadleaved", "palm"}},
    "DEN_COUNTS": _DEN,
    "DEN_COMMON": sorted((v for v, n in _DEN.items() if n >= ft.DEN_COMMON_MIN), key=len, reverse=True),
    "MASAF_LIST": {ft.masaf_code(REMOVED): ["morte naturale", "D.M. 1/2020"], ft.masaf_code(LISTED): None},
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
    ("diameter_unit", {"natural": "tree", "diameter": "0.5"}, ["diameter_unit"]),
    ("unit_format: parola intera", {"natural": "tree", "height": "8 metres"}, ["unit_format"]),
    ("unit_format: senza spazio", {"natural": "tree", "circumference": "150cm"}, ["unit_format"]),
    ("number_invalid: testo", {"natural": "tree", "height": "tall"}, ["number_invalid"]),
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
    ("dipendenza: con species_epithet_case niente species_lowercase", {"natural": "tree", "species": "quercus Robur"},
     ["species_epithet_case"]),
    ("dipendenza: con species_author niente species_epithet_case", {"natural": "tree", "species": "Quercus roBur L."},
     ["species_author"]),
    ("dipendenza: con species_author niente species_lowercase", {"natural": "tree", "species": "quercus robur L."},
     ["species_author"]),
    ("species_common_name", {"natural": "tree", "species": "Black Matipo"}, ["species_common_name"]),
    ("dipendenza: con species_common_name niente species_parentheses",
     {"natural": "tree", "species": "Black Matipo (Pittosporum tenuifolium)"}, ["species_common_name"]),
    ("species_author", {"natural": "tree", "species": "Quercus robur L."}, ["species_author"]),
    ("dipendenza: con species_author niente species_parentheses",
     {"natural": "tree", "species": "Camellia sinensis (L.) Kuntze"}, ["species_author"]),
    ("species_parentheses", {"natural": "tree", "species": "Quercus robur (red)"}, ["species_parentheses"]),
    ("species_parentheses: sinonimi tra parentesi, non un autore",
     {"natural": "tree", "species": "Ulmus laevis (Ulmus pedenculata, Ulmus effusa)"}, ["species_parentheses"]),
    ("species_author: con altro prima del nome", {"natural": "tree", "species": "Insieme omogeneo di Fagus sylvatica L."},
     ["species_author"]),
    ("underscore", {"natural": "tree", "genus": "Tilia_cordata"}, ["underscore"]),
    ("species_typo", {"natural": "tree", "species": "Quercus rubur"}, ["species_typo"]),
    ("species_typo: eccezione in whitelist", {"natural": "tree", "species": "Quercus robar"}, []),
    ("species_typo: eccezione in whitelist, con × e cultivar", {"natural": "tree", "species": "Quercus × robar 'Alba'"}, []),
    ("species_typo: non per una cultivar senza epiteto", {"natural": "tree", "species": "Quercus 'Rubur'"}, []),
    ("species: nome comune di un genere", {"natural": "tree", "species": "tilleul"}, ["species"]),
    ("species: genere con autore", {"natural": "tree", "species": "Tilia L."}, ["species"]),
    ("ibrido con x iniziale", {"natural": "tree", "species": "x Cupressocyparis leylandii",
                               "genus": "Cupressocyparis"}, []),
    ("ibrido con x iniziale, genere minuscolo", {"natural": "tree", "species": "x cupressocyparis leylandii"},
     ["species_lowercase"]),
    ("x ed epiteto senza genere", {"natural": "tree", "genus": "Quercus", "species": "x robur"}, ["species"]),
    ("species_lang_only: nome di una specie", {"natural": "tree", "species:pl": "Klon zwyczajny"}, ["species_lang_only"]),
    ("species_lang_only: nome di una specie, maiuscole diverse", {"natural": "tree", "species:pl": "klon ZWYCZAJNY"},
     ["species_lang_only"]),
    ("species_lang_only", {"natural": "tree", "species:it": "Leccio"}, ["species_lang_only"]),
    ("species_lang_only: nome italiano di una specie", {"natural": "tree", "species:it": "Bagolaro"},
     ["species_lang_only"]),
    ("species_lang_only: nome italiano di un genere", {"natural": "tree", "species:it": "Quercia"},
     ["species_lang_only"]),
    ("species:xx insieme a species", {**OAK, "species:it": "Farnia"}, []),
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
    ("dipendenza: con genus_common_name dello stesso genere niente genus_mismatch", {**OAK, "genus": "Quercia"},
     ["genus_common_name"]),
    ("genus_common_name e genus_mismatch: un altro genere", {**OAK, "genus": "Tilleul"},
     ["genus_common_name", "genus_mismatch"]),
    ("genus_mismatch", {**OAK, "genus": "Pinus"}, ["genus_mismatch"]),
    ("genus_mismatch: genus scritto male", {**OAK, "genus": "Quercos"}, ["genus_mismatch"]),
    ("taxon_lang_only", {"natural": "tree", "taxon:en": "Oak"}, ["taxon_lang_only"]),
    ("taxon:xx insieme a taxon", {"natural": "tree", "taxon": "Quercus", "taxon:en": "Oak"}, []),
    ("taxon:cultivar non è una lingua", {"natural": "tree", "taxon:cultivar": "Fastigiata"}, []),
    ("multiple_values: genus e species, senza genus_mismatch",
     {"natural": "tree", "genus": "Quercus;Pinus", "species": "Quercus robur;Pinus pinea"}, ["multiple_values"]),
    ("multiple_values: solo species", {"natural": "tree", "species": "Quercus robur; Pinus pinea"}, ["multiple_values"]),
    ("dipendenza: con tag_in_value niente multiple_values", {"natural": "tree", "genus": "a=b;c"}, ["tag_in_value"]),
    ("dipendenza: species è un epiteto, niente genus_mismatch",
     {"natural": "tree", "genus": "Quercus", "species": "robur"}, ["species"]),

    # --- leaf_type e leaf_cycle ---
    ("leaf_type", {"natural": "tree", "leaf_type": "broad"}, ["leaf_type"]),
    ("leaf_cycle", {"natural": "tree", "leaf_cycle": "decidous"}, ["leaf_cycle"]),
    ("leaf_type_mismatch dalla specie", {**OAK, "leaf_type": "needleleaved"}, ["leaf_type_mismatch"]),
    ("leaf_type_mismatch dal genere", {"natural": "tree", "genus": "Pinus", "leaf_type": "broadleaved"},
     ["leaf_type_mismatch"]),
    ("leaf_type=palm su una latifoglia", {**OAK, "leaf_type": "palm"}, ["leaf_type_mismatch"]),
    ("leaf_type=palm su una pianta dall'aspetto di palma", {"natural": "tree", "genus": "Yucca", "leaf_type": "palm"},
     []),
    ("leaf_types in whitelist: broadleaved", {"natural": "tree", "genus": "Cycas", "leaf_type": "broadleaved"}, []),
    ("leaf_types in whitelist: palm, genere da species",
     {"natural": "tree", "species": "Cycas revoluta", "leaf_type": "palm"}, []),
    ("leaf_cycle_mismatch", {**OAK, "leaf_cycle": "evergreen"}, ["leaf_cycle_mismatch"]),
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
    ("link_format: due indirizzi in image",
     {"natural": "tree", "image": "https://example.org/ok.jpg;https://example.org/ok.jpg"}, ["link_format"]),
    ("link_format: due QID in wikidata", {"natural": "tree", "wikidata": "Q910001;Q910001"}, ["link_format"]),
    ("link_format: wikimedia_commons", {"natural": "tree", "wikimedia_commons": "Tree.jpg"}, ["link_format"]),
    ("link_format: panoramax", {"natural": "tree", "panoramax": "12345"}, ["link_format"]),
    ("broken_link: image", {"natural": "tree", "image": "https://example.org/gone.jpg"}, ["broken_link"]),
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
    ("monument_denotation: ref:masaf su un ceppo", {"natural": "tree_stump", "ref:masaf": LISTED},
     ["monument_denotation"]),
    ("monument_denotation: monumentaltrees.com",
     {"natural": "tree", "website": "https://www.monumentaltrees.com/en/ita/lazio/1/"}, ["monument_denotation"]),
    ("monument_denotation: tutela su Wikidata", {"natural": "tree", "wikidata": "Q910004"}, ["monument_denotation"]),
    ("monument_denotation: id di monumentaltrees.com su Wikidata", {"natural": "tree", "wikidata": "Q910005"},
     ["monument_denotation"]),
    ("fonte monumentale con natural_monument", {**MON, "ref:masaf": LISTED}, []),
    ("fonte monumentale con natural_monument dopo uno spazio",
     {"natural": "tree", "denotation": "urban; natural_monument", "ref:masaf": LISTED}, []),
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
    ("dipendenza: con tag_in_value niente start_date_invalid", {"natural": "tree", "start_date": "start_date=1990"},
     ["tag_in_value"]),

    # --- denotation, tag non documentati, chiavi ---
    ("denotation_value: formato", {"natural": "tree", "denotation": "Urban"}, ["denotation_value"]),
    ("denotation_value: valore comune con altro testo attaccato", {"natural": "tree", "denotation": "urbantree"},
     ["denotation_value"]),
    ("denotation_value: valore raro", {"natural": "tree", "denotation": "rarevalue"}, ["denotation_value"]),
    ("undocumented_tag: tree", {"natural": "tree", "tree": "oak"}, ["undocumented_tag"]),
    ("undocumented_tag: type", {"natural": "tree", "type": "broad_leaved"}, ["undocumented_tag"]),
    ("type=palm non è segnalato", {"natural": "tree", "type": "palm"}, []),
    ("key_typo: lettere scambiate", {"natural": "tree", "heigth": "12"}, ["key_typo"]),
    ("key_typo: maiuscola", {"natural": "tree", "Species": "Quercus robur"}, ["key_typo"]),
    ("key_typo: ref: con il resto scritto male", {"natural": "tree", "ref:massaf": LISTED}, ["key_typo"]),
    ("key_typo: non per un'altra chiave ref: corta", {"natural": "tree", "ref:uid": "12"}, []),
    ("chiave numerata e sottochiave", {"natural": "tree", "image2": "https://example.org/ok.jpg",
                                       "circumference:note": "a 1,30 m"}, []),

    # --- più errori insieme ---
    ("più errori sullo stesso albero", {"natural": "tree", "species": "quercus robur", "height": "12,5",
                                        "leaf_type": "broad", "tree": "oak"},
     ["comma", "leaf_type", "species_lowercase", "undocumented_tag"]),
]


# Consigli di hint_props: (nome, tag, id del nodo, proprietà, atteso: True = c'è, False = non c'è, o il valore).
# hint_ms: soglia MASAF di circonferenza, solo nelle regioni italiane; hint_wg: l'elemento Wikidata dell'albero ha
# solo il genere della specie
HINT_CASES = [
    ("hint_ms: circonferenza oltre la soglia", {**OAK, "circumference": "5"}, None, "hint_ms", True),
    ("hint_ms: non con natural_monument", {**MON, **OAK, "circumference": "5"}, None, "hint_ms", False),
    ("hint_ms: non per un membro di un insieme omogeneo", {**OAK, "denotation": "park", "circumference": "5"}, MEMBER,
     "hint_ms", False),
    ("hint_wg: Wikidata ha solo il genere", {**OAK, "wikidata": "Q910008"}, None, "hint_wg", "Q910008|Quercus"),
    ("hint_wg: non per un altro genere", {"natural": "tree", "species": "Pinus pinea", "wikidata": "Q910008"}, None,
     "hint_wg", False),
    ("hint_tx: taxon è una specie della lista", {"natural": "tree", "taxon": "Quercus robur"}, None, "hint_tx",
     "species|Quercus robur"),
    ("hint_tx: taxon è un genere della lista", {"natural": "tree", "taxon": "pinus"}, None, "hint_tx", "genus|Pinus"),
    ("hint_tx: species uguale", {**OAK, "taxon": "Quercus robur"}, None, "hint_tx", "species|Quercus robur"),
    ("hint_tx: non con un genus diverso", {"natural": "tree", "taxon": "Quercus robur", "genus": "Pinus"}, None,
     "hint_tx", False),
    ("hint_tx: non con una cultivar", {"natural": "tree", "taxon": "Quercus robur 'Fastigiata'"}, None, "hint_tx", False),
    ("hint_tx: non con una species di un altro genere", {"natural": "tree", "taxon": "Quercus", "species": "Pinus pinea"},
     None, "hint_tx", False),
    ("hint_ne: name:en con la minuscola", {"natural": "tree", "name:en": "old oak"}, None, "hint_ne", "old oak"),
    ("hint_ne: non con la maiuscola", {"natural": "tree", "name:en": "Old Oak"}, None, "hint_ne", False),
    # image=* pagina web (hint_ip, hint_mly) e website=*/url=* voce di Wikipedia (hint_wpw)
    ("hint_ip: pagina di Google Foto", {"natural": "tree", "image": "https://photos.app.goo.gl/Yqzit3vNyopEEK2q7"},
     None, "hint_ip", "photos.app.goo.gl"),
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
    ("hint_wpw: website su Wikipedia", {"natural": "tree", "website": "https://en.wikipedia.org/wiki/Kett%27s_Oak"},
     None, "hint_wpw", "website|en:Kett's Oak"),
    ("hint_wpw: url sul sito per cellulari", {"natural": "tree", "url": "https://fr.m.wikipedia.org/wiki/H%C3%AAtre_de_Ponthus"},
     None, "hint_wpw", "url|fr:Hêtre de Ponthus"),
    ("hint_wpw: non con wikipedia=*", {"natural": "tree", "wikipedia": "en:Kett's Oak",
     "website": "https://en.wikipedia.org/wiki/Kett%27s_Oak"}, None, "hint_wpw", False),
    ("hint_wpw: non per un altro sito", {"natural": "tree", "website": "https://example.org/wiki/Oak"},
     None, "hint_wpw", False),
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
    ("Cryptomeria japonica (Thunb. ex L. f.) D. Don var. elegans", "Cryptomeria japonica var. elegans"),
    ("Insieme omogeneo di Fagus sylvatica L.", None),                            # resta altro oltre al nome
]

# Correzione con JOSM di species_typo e species_author (sp_fix e i tag che mancano per la specie corretta, sp_fix_*):
# (nome, tag, proprietà attese; None = la proprietà non c'è)
SP_FIX_CASES = [
    ("refuso: species della lista e i tag che mancano", {"natural": "tree", "species": "Quercus rubur"},
     {"sp_fix": "Quercus robur", "sp_fix_wd": "Q920001", "sp_fix_lt": "broadleaved", "sp_fix_lc": "deciduous"}),
    ("refuso con l'autore: la stessa species per i due errori", {"natural": "tree", "species": "Quercus rubur L.",
                                                                "leaf_type": "broadleaved"},
     {"sp_clean": "Quercus rubur", "sp_fix": "Quercus robur", "sp_fix_wd": "Q920001", "sp_fix_lt": None}),
    ("refuso con la cultivar: nessuna correzione", {"natural": "tree", "species": "Quercus rubur 'Fastigiata'"},
     {"sp_fix": None}),
    ("solo l'autore: species:wikidata della specie senza autore",
     {"natural": "tree", "species": "Quercus robur L.", "species:wikidata": "Q920001"},
     {"sp_clean": "Quercus robur", "sp_fix": None, "sp_fix_wd": None, "sp_fix_lc": "deciduous"}),
    ("solo l'autore, senza species:wikidata", {"natural": "tree", "species": "Quercus robur L."},
     {"sp_fix_wd": "Q920001"}),
    # altre proposte delle correzioni con JOSM: genus (gm_fix, g_common) e il rango della voce di wikipedia (wp_genus)
    ("genus scritto male: genus di species", {**OAK, "genus": "Quercos"}, {"gm_fix": "Quercus"}),
    ("genus diverso, ma un genere noto: nessuna correzione", {**OAK, "genus": "Pinus"}, {"gm_fix": None}),
    ("genus diverso e lontano: nessuna correzione", {**OAK, "genus": "Fraxinus"}, {"gm_fix": None}),
    ("genus nome comune: il genere e la lingua", {"natural": "tree", "genus": "quercia"},
     {"g_common": "Quercus", "g_common_lang": "it"}),
    ("wikipedia di un genere: wp_genus", {"natural": "tree", "wikipedia": "it:Quercus"}, {"wp_genus": 1}),
    ("wikipedia di una specie: niente wp_genus", {"natural": "tree", "wikipedia": "it:Quercus_robur"}, {"wp_genus": None}),
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
        val = ft.hint_props(dict(tags), osm_id).get(key)
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
              ("non membro: niente mg, hint_ms", "mg" not in other and "hint_ms" in other),
              ("ceppo membro: resta un ceppo, senza mg", stump.get("stump") == 1 and "mg" not in stump),
              ("file ridotto: il membro c'è, l'albero qualsiasi no", f'"id":{MEMBER}' in small.decode() and '"id":9002' not in small.decode())]
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
