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
REMOVED = "001/A001/RM/01"  # codice MASAF finto, rimosso dall'elenco
LISTED = "002/A002/RM/02"   # codice MASAF finto, iscritto

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
    "GENUS_NAMES": {("fr", "tilleul"): "Tilia"},
    "SPECIES_NAMES": {("pl", "klon zwyczajny"): "Acer platanoides"},
    "SINGLE_SPECIES": {},
    "DEN_COUNTS": _DEN,
    "DEN_COMMON": sorted((v for v, n in _DEN.items() if n >= ft.DEN_COMMON_MIN), key=len, reverse=True),
    "MASAF_LIST": {ft.masaf_code(REMOVED): ["morte naturale", "D.M. 1/2020"], ft.masaf_code(LISTED): None},
    # elementi di wikidata=* (i singoli alberi)
    "WD_ITEMS": {
        "Q910001": {"taxon": False, "coords": [POS]},                 # vicino
        "Q910002": {"taxon": False, "coords": [FAR]},                 # lontano
        "Q910003": {"taxon": False, "taxa": ["Q920002"]},             # individuo di Pinus pinea (P10241)
        "Q910004": {"taxon": False, "heritage": [["monumento naturale", "natural monument"]]},   # tutela (P1435)
        "Q910005": {"taxon": False, "mt": "12345"},                   # id di monumentaltrees.com (P9704)
        "Q910006": {"taxon": False, "image": "Far_tree.jpg"},         # foto (P18), senza coordinate
    },
    # elementi di species:wikidata e genus:wikidata
    "WD_TAXA": {
        "Q920001": {"taxon": True, "names": ["Quercus robur"], "ranks": ["Q7432"]},
        "Q920002": {"taxon": True, "names": ["Pinus pinea"], "ranks": ["Q7432"]},
        "Q920003": {"taxon": True, "names": ["Quercus"], "ranks": ["Q34740"]},
        "Q920004": {"taxon": False},                                  # non è un taxon
        "Q920005": {"taxon": True, "names": ["Pinus"], "ranks": ["Q34740"]},
        "Q920006": {"taxon": True, "names": ["Acer campestre"], "ranks": ["Q7432"]},   # specie non in lista
    },
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

# (nome del caso, tag, codici attesi[, id del nodo]). L'ordine dei codici non conta.
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
    ("species_common_name", {"natural": "tree", "species": "Black Matipo"}, ["species_common_name"]),
    ("dipendenza: con species_common_name niente species_parentheses",
     {"natural": "tree", "species": "Black Matipo (Pittosporum tenuifolium)"}, ["species_common_name"]),
    ("species_author", {"natural": "tree", "species": "Quercus robur L."}, ["species_author"]),
    ("dipendenza: con species_author niente species_parentheses",
     {"natural": "tree", "species": "Camellia sinensis (L.) Kuntze"}, ["species_author"]),
    ("species_parentheses", {"natural": "tree", "species": "Quercus robur (red)"}, ["species_parentheses"]),
    ("underscore", {"natural": "tree", "genus": "Tilia_cordata"}, ["underscore"]),
    ("species_typo", {"natural": "tree", "species": "Quercus rubur"}, ["species_typo"]),
    ("species_typo: eccezione in whitelist", {"natural": "tree", "species": "Quercus robar"}, []),
    ("species_lang_only", {"natural": "tree", "species:it": "Leccio"}, ["species_lang_only"]),
    ("species:xx insieme a species", {**OAK, "species:it": "Farnia"}, []),
    ("genus: più parole", {"natural": "tree", "genus": "Quercus robur"}, ["genus"]),
    ("genus_lowercase", {"natural": "tree", "genus": "quercus"}, ["genus_lowercase"]),
    ("genus_mismatch", {**OAK, "genus": "Pinus"}, ["genus_mismatch"]),
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
    ("leaf_cycle_mismatch", {**OAK, "leaf_cycle": "evergreen"}, ["leaf_cycle_mismatch"]),
    ("semi_deciduous vale come deciduous", {**OAK, "leaf_cycle": "semi_deciduous"}, []),
    ("genere con entrambi i leaf_cycle", {"natural": "tree", "genus": "Magnolia", "leaf_cycle": "evergreen"}, []),

    # --- Wikidata ---
    ("wikidata_mismatch: QID diverso da quello della lista", {**OAK, "species:wikidata": "Q920002"},
     ["wikidata_mismatch"]),   # dipendenza: niente wikidata_name_mismatch per lo stesso tag
    ("wikidata_mismatch: QID di un'altra specie della lista",
     {"natural": "tree", "species": "Fagus sylvatica", "species:wikidata": "Q920001"}, ["wikidata_mismatch"]),
    ("wikidata_mismatch: eccezione in whitelist", {**OAK, "species:wikidata": "Q920009"}, []),
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
    ("wikidata_far", {"natural": "tree", "wikidata": "Q910002"}, ["wikidata_far"]),
    ("wikidata_photo_far", {"natural": "tree", "wikidata": "Q910006"}, ["wikidata_photo_far"]),

    # --- collegamenti ---
    ("link_format: wikidata", {"natural": "tree", "wikidata": "Quercus"}, ["link_format"]),
    ("link_format: image", {"natural": "tree", "image": "foto.jpg"}, ["link_format"]),
    ("link_format: wikimedia_commons", {"natural": "tree", "wikimedia_commons": "Tree.jpg"}, ["link_format"]),
    ("link_format: panoramax", {"natural": "tree", "panoramax": "12345"}, ["link_format"]),
    ("broken_link: image", {"natural": "tree", "image": "https://example.org/gone.jpg"}, ["broken_link"]),
    ("broken_link: file di Commons mancante", {"natural": "tree", "wikimedia_commons": "File:Missing tree.jpg"},
     ["broken_link"]),
    ("commons_far", {"natural": "tree", "wikimedia_commons": "File:Far_tree.jpg"}, ["commons_far"]),
    ("file di Commons vicino", {"natural": "tree", "wikimedia_commons": "File:Near tree.jpg"}, []),
    ("dipendenza: con commons_far niente wikidata_photo_far per la stessa foto",
     {"natural": "tree", "wikidata": "Q910006", "wikimedia_commons": "File:Far tree.jpg"}, ["commons_far"]),

    # --- alberi monumentali ---
    ("monument_denotation: ref:masaf", {"natural": "tree", "ref:masaf": LISTED}, ["monument_denotation"]),
    ("monument_denotation: heritage:operator", {"natural": "tree", "heritage:operator": "ICNF"},
     ["monument_denotation"]),
    ("monument_denotation: monumentaltrees.com",
     {"natural": "tree", "website": "https://www.monumentaltrees.com/en/ita/lazio/1/"}, ["monument_denotation"]),
    ("monument_denotation: tutela su Wikidata", {"natural": "tree", "wikidata": "Q910004"}, ["monument_denotation"]),
    ("monument_denotation: id di monumentaltrees.com su Wikidata", {"natural": "tree", "wikidata": "Q910005"},
     ["monument_denotation"]),
    ("fonte monumentale con natural_monument", {**MON, "ref:masaf": LISTED}, []),
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
    ("masaf_criteria", {"natural": "tree", "masaf:criteria": "a1,B"}, ["masaf_criteria"]),
    ("masaf:criteria validi", {"natural": "tree", "masaf:criteria": "a1,b"}, []),
    ("masaf_removed", {**MON, "ref:masaf": REMOVED}, ["masaf_removed"]),
    ("masaf_removed: non per i ceppi", {"natural": "tree_stump", "denotation": "natural_monument",
                                        "ref:masaf": REMOVED}, []),

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
    ("chiave numerata e sottochiave", {"natural": "tree", "image2": "https://example.org/ok.jpg",
                                       "circumference:note": "a 1,30 m"}, []),

    # --- più errori insieme ---
    ("più errori sullo stesso albero", {"natural": "tree", "species": "quercus robur", "height": "12,5",
                                        "leaf_type": "broad", "tree": "oak"},
     ["comma", "leaf_type", "species_lowercase", "undocumented_tag"]),
]


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
    print(f"Test dei check: {len(CASES)} casi, {len(codes)} check, tutto a posto")


if __name__ == "__main__":
    main()
