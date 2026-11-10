#!/usr/bin/env python3
"""Legge il KML dell'elenco degli alberi monumentali d'Italia del MASAF (scaricato a mano dalla pagina
https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/11260) e scrive scripts/masaf_list.json, la lista
compatta dei codici usata dal check masaf_removed di fetch_trees.py:
{"version": versione del KML caricato (dalla sua descrizione, es. "X Aggiornamento, D.Dir. n. 364243 del 24/07/2026"),
 "source": citazione richiesta dalla licenza, "license": "CC BY 4.0",
 "codes": {"<codice>": null se iscritto, oppure [motivo della rimozione, decreto di modifica]}}
Prima di caricare un KML nuovo, confrontare la sua versione con "version": se è la stessa non serve rilanciare.
Motivi: "death" (morte naturale), "felled" (abbattimento), "requirements" (perdita di requisiti), "downgraded"
(declassamento), "other". I codici sono normalizzati come in fetch_trees.py (masaf_code: senza spazi, maiuscoli, primo
numero a tre cifre).

Uso: python scripts/update_masaf.py "Alberi monumentali d'Italia.kml"
Il KML (circa 30 MB) non va nel repository: basta questa lista."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "masaf_list.json"
NS = {"k": "http://www.opengis.net/kml/2.2"}
SOURCE = ('Masaf - Direzione generale dell\'economia montana e delle foreste - Difor4 - "dataset AMI - Censimento '
          'alberi monumentali d\'Italia vs. 11_2024"')


def masaf_code(s):
    """Codice MASAF normalizzato: senza spazi, maiuscolo, primo numero a tre cifre (03/G114/CN/01 = 003/G114/CN/01)."""
    parts = re.sub(r"\s+", "", str(s)).upper().split("/")
    if parts and parts[0].isdigit():
        parts[0] = parts[0].zfill(3)
    return "/".join(parts)


def reason(stato):
    s = stato.lower()
    for key, word in (("death", "morte"), ("felled", "abbattimento"), ("requirements", "requisiti"),
                      ("downgraded", "declassamento")):
        if word in s:
            return key
    return "other"


def kml_version(root):
    """Versione dell'elenco dalla descrizione del KML: "X Aggiornamento dell'Elenco…" e "Versione D.Dir. n. … del …"."""
    desc = re.sub(r"<[^>]+>", " ", root.findtext("k:Document/k:description", default="", namespaces=NS))
    upd = re.search(r"\b([IVXLC]+)\s+Aggiornamento", desc)
    ver = re.search(r"Versione\s+(.+?\d{2}/\d{2}/\d{4})", desc)
    return ", ".join(x for x in ((upd.group(1) + " Aggiornamento") if upd else "",
                                 ver.group(1).strip() if ver else "") if x) or "sconosciuta"


def main(path):
    codes = {}
    root = ET.parse(path).getroot()
    version = kml_version(root)
    old = json.loads(OUT.read_text(encoding="utf-8")).get("version") if OUT.exists() else None
    if old == version:
        print(f"Versione già caricata ({version}): scripts/masaf_list.json viene riscritto comunque")
    for p in root.findall(".//k:Placemark", NS):
        d = {x.get("name"): (x.findtext("k:value", namespaces=NS) or "").strip() for x in p.findall(".//k:Data", NS)}
        code = masaf_code(p.findtext("k:name", namespaces=NS) or "")
        stato = d.get("STATO", "")
        codes[code] = None if "iscritt" in stato.lower() else [reason(stato), d.get("DECR DI MODIFICA", "")]
    removed = sum(1 for v in codes.values() if v)
    OUT.write_text(json.dumps({"version": version, "source": SOURCE, "license": "CC BY 4.0",
                               "codes": dict(sorted(codes.items()))},
                              ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Versione {version}: {len(codes)} codici ({removed} rimossi dall'elenco) -> {OUT}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
