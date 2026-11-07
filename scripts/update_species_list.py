#!/usr/bin/env python3
"""Scarica la lista di specie della wiki OSM (Tag:natural=tree/List of Species) e la salva in
scripts/species_list.json: per ogni specie genus, species:wikidata, leaf_cycle e leaf_type.
Contenuto della wiki sotto licenza CC BY-SA 2.0."""
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import requests

URL = "https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Species"
OUT = Path(__file__).resolve().parent / "species_list.json"
HEADERS = {"User-Agent": "tree-osm-qa/1.0 (https://github.com/ivanbranco/tree-osm-qa)"}
LEAF_TYPE = {"broadleaved", "needleleaved", "leafless", "palm"}
LEAF_CYCLE = {"deciduous", "semi_deciduous", "evergreen", "semi_evergreen"}


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


def parse(html):
    p = TableParser()
    p.feed(html)
    for rows in p.tables:
        header = [h.lower().replace("=*", "").strip() for h in rows[0]] if rows else []
        if "species" in header and "species:wikidata" in header:
            return header, rows[1:]
    raise SystemExit("Tabella delle specie non trovata: la struttura della pagina è cambiata?")


def build(html):
    header, rows = parse(html)
    col = {name: header.index(name) for name in ("species", "genus", "species:wikidata", "leaf_cycle", "leaf_type")
           if name in header}
    species = {}
    for r in rows:
        if len(r) < len(header):
            continue
        name = r[col["species"]]
        entry = {"genus": r[col["genus"]] if "genus" in col else ""}
        wd = r[col["species:wikidata"]]
        entry["wikidata"] = wd if re.fullmatch(r"Q[1-9]\d*", wd) else None
        lc, lt = r[col["leaf_cycle"]], r[col["leaf_type"]]
        entry["leaf_cycle"] = lc if lc in LEAF_CYCLE else None
        entry["leaf_type"] = lt if lt in LEAF_TYPE else None
        for key, val in (("species:wikidata", wd), ("leaf_cycle", lc), ("leaf_type", lt)):
            if entry[{"species:wikidata": "wikidata"}.get(key, key)] is None:
                print(f"  [!] {name}: {key} '{val}' non valido, ignorato")
        if name in species:
            print(f"  [!] {name}: riga duplicata, tengo la prima")
            continue
        species[name] = entry
    return {"source": URL, "license": "CC BY-SA 2.0 (OpenStreetMap Wiki)", "count": len(species), "species": species}


def main():
    r = requests.get(URL, headers=HEADERS, timeout=120)
    r.raise_for_status()
    data = build(r.text)
    if data["count"] < 100:
        sys.exit(f"Solo {data['count']} specie trovate: controlla la pagina prima di sovrascrivere il file")
    text = json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == text:
        print(f"{data['count']} specie: nessuna modifica")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"{data['count']} specie -> {OUT}")


if __name__ == "__main__":
    main()
