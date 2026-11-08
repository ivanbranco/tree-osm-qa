#!/usr/bin/env python3
"""Scarica le liste di riferimento sugli alberi:
- wiki OSM, List of Species  -> scripts/species_list.json (species:wikidata, leaf_cycle, leaf_type per specie)
- wiki OSM, List of Genus, Leaf cycle, Leaf type -> scripts/genus_list.json (leaf_cycle e leaf_type per genere)
- Taginfo, valori di denotation sui nodi -> scripts/denotation_values.json (numero di usi di ogni valore)
Contenuto della wiki sotto licenza CC BY-SA 2.0; dati di Taginfo © contributori di OpenStreetMap (ODbL)."""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SPECIES_URL = "https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Species"
GENUS_URL = "https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Genus,_Leaf_cycle,_Leaf_type"
HEADERS = {"User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)"}
LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}
LICENSE = "CC BY-SA 2.0 (OpenStreetMap Wiki)"


class TableParser(HTMLParser):
    """Raccoglie le righe (liste di celle di testo) di ogni tabella della pagina."""

    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None


def header_key(h):
    """'leaf_cycle=* (deciduous or evergreen)' -> 'leaf_cycle'; 'genus=*[1]' -> 'genus'; 'genus:en' resta uguale."""
    return re.split(r"[=\[\s(]", h.strip().lower())[0]


def find_table(html, required):
    p = TableParser()
    p.feed(html)
    for rows in p.tables:
        header = [header_key(h) for h in rows[0]] if rows else []
        if all(k in header for k in required):
            return header, rows[1:]
    raise SystemExit(f"Tabella con colonne {required} non trovata: la struttura della pagina è cambiata?")


def cell(row, header, name):
    return row[header.index(name)] if name in header and header.index(name) < len(row) else ""


def build_species(html):
    header, rows = find_table(html, ["species", "species:wikidata", "leaf_cycle", "leaf_type"])
    species = {}
    for r in rows:
        name = cell(r, header, "species")
        if not name:
            continue
        wd, lc, lt = cell(r, header, "species:wikidata"), cell(r, header, "leaf_cycle"), cell(r, header, "leaf_type")
        entry = {"wikidata": wd if re.fullmatch(r"Q[1-9]\d*", wd) else None,
                 "leaf_cycle": lc if lc in LEAF_CYCLE else None,
                 "leaf_type": lt if lt in LEAF_TYPE else None}
        for key, val, ok in (("species:wikidata", wd, entry["wikidata"]), ("leaf_cycle", lc, entry["leaf_cycle"]),
                             ("leaf_type", lt, entry["leaf_type"])):
            if ok is None:
                print(f"  [!] {name}: {key} '{val}' non valido, ignorato")
        if name in species:
            print(f"  [!] {name}: riga duplicata, tengo la prima")
            continue
        species[name] = entry
    return {"source": SPECIES_URL, "license": LICENSE, "count": len(species), "species": species}


def build_genera(html):
    header, rows = find_table(html, ["genus", "leaf_cycle", "leaf_type"])
    genera = {}
    for r in rows:
        name = cell(r, header, "genus")
        if not re.fullmatch(r"×?\s?[A-Z][a-z-]+", name):   # salta la riga finale "all others" e simili
            continue
        cycles = list(dict.fromkeys(re.findall(r"deciduous|evergreen", cell(r, header, "leaf_cycle").lower())))
        lt = cell(r, header, "leaf_type")
        entry = {"leaf_cycle": cycles, "leaf_type": lt if lt in LEAF_TYPE else None}
        if not cycles or entry["leaf_type"] is None:
            print(f"  [!] {name}: leaf_cycle/leaf_type non validi, riga incompleta")
        syn = cell(r, header, "synonym")
        if syn:
            entry["synonym"] = syn
        if name in genera:
            print(f"  [!] {name}: riga duplicata, tengo la prima")
            continue
        genera[name] = entry
    return {"source": GENUS_URL, "license": LICENSE, "count": len(genera), "genera": genera}


TAGINFO_URL = "https://taginfo.openstreetmap.org/api/4/key/values"


def build_denotation():
    """Tutti i valori di denotation usati sui nodi, con il numero di usi (da Taginfo, a pagine da 999)."""
    values, page = {}, 1
    while True:
        r = requests.get(TAGINFO_URL, headers=HEADERS, timeout=120, params={
            "key": "denotation", "filter": "nodes", "sortname": "count", "sortorder": "desc", "rp": 999, "page": page})
        r.raise_for_status()
        data = r.json()
        for row in data["data"]:
            values[row["value"]] = row["count"]
        if not data["data"] or len(values) >= data["total"]:
            break
        page += 1
    return {"source": "https://taginfo.openstreetmap.org/keys/denotation#values", "filter": "nodes",
            "license": "ODbL (© OpenStreetMap contributors)", "count": len(values), "values": values}


def save(path, data, minimum, unit):
    if data["count"] < minimum:
        sys.exit(f"{path.name}: solo {data['count']} {unit} trovate, controlla la pagina prima di sovrascrivere il file")
    text = json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        print(f"{path.name}: {data['count']} {unit}, nessuna modifica")
    else:
        path.write_text(text, encoding="utf-8")
        print(f"{path.name}: {data['count']} {unit} salvate")


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    return r.text


def main():
    save(HERE / "species_list.json", build_species(fetch(SPECIES_URL)), 100, "specie")
    save(HERE / "genus_list.json", build_genera(fetch(GENUS_URL)), 100, "generi")
    save(HERE / "denotation_values.json", build_denotation(), 20, "valori di denotation")


if __name__ == "__main__":
    main()
