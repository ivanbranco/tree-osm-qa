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

Scrive anche scripts/masaf_trees.json, gli alberi iscritti con i loro dati, per mostrare sulla mappa dei monumentali
quelli non ancora su OpenStreetMap (fetch_world.py li confronta con OSM a ogni aggiornamento):
{"version", "source", "license", "trees": [{"c": codice, "lat", "lon", "g": genere, "s": specie, "v": nome volgare,
 "co": comune, "pr": provincia, "lo": località, "t": tipologia, "h": altezza (m), "ci": circonferenza (m),
 "cr": criteri (codici di masaf:criteria: a1, a2.1, a2.2, a2.3, b–g)}]}; per gruppi e filari h e ci sono i massimi.

Uso: python scripts/update_masaf.py "Alberi monumentali d'Italia.kml"
Il KML (circa 30 MB) non va nel repository: bastano queste due liste."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "masaf_list.json"
OUT_TREES = HERE / "masaf_trees.json"
# campi "CR …" del KML -> codici dei criteri di masaf:criteria (vedi masafCriteria in index.html)
CRITERIA = {"CR ETA'": "a1", "CR CIRCONFERENZA": "a2.1", "CR ALTEZZA": "a2.2", "CR AMPIEZZA CHIOMA": "a2.3",
            "CR FORMA O PORTAMENTO": "b", "CR VALORE ECOLOGICO": "c", "CR RARITA' BOTANICA": "d", "CR ARCHITETTURA": "e",
            "CR PAESAGGISTICO": "f", "CR STORICO": "g"}


def measure(v, unit):
    """"8,5 m" -> 8.5; "314 cm" -> 3.14 (in metri); None se manca o non è un numero."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", v or "")
    return round(float(m.group(1).replace(",", ".")) / (100 if unit == "cm" else 1), 2) if m else None


def field(d, *names):
    """Primo campo non vuoto tra quelli indicati (alcuni nomi del KML hanno spazi doppi)."""
    norm = {re.sub(r"\s+", " ", k): v for k, v in d.items()}
    return next((norm[n] for n in names if norm.get(n)), "")
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
    codes, trees = {}, []
    root = ET.parse(path).getroot()
    version = kml_version(root)
    old = json.loads(OUT.read_text(encoding="utf-8")).get("version") if OUT.exists() else None
    if old == version:
        print(f"Versione già caricata ({version}): scripts/masaf_list.json viene riscritto comunque")
    for p in root.findall(".//k:Placemark", NS):
        d = {x.get("name"): (x.findtext("k:value", namespaces=NS) or "").strip() for x in p.findall(".//k:Data", NS)}
        code = masaf_code(p.findtext("k:name", namespaces=NS) or "")
        stato = d.get("STATO", "")
        listed = "iscritt" in stato.lower()
        codes[code] = None if listed else [reason(stato), d.get("DECR DI MODIFICA", "")]
        if not listed:
            continue
        lon, lat = map(float, p.find(".//k:coordinates", NS).text.strip().split(",")[:2])
        tree = {"c": code, "lat": round(lat, 6), "lon": round(lon, 6), "g": d.get("GENERE", ""), "s": d.get("SPECIE", ""),
                "v": d.get("NOME VOLGARE", ""), "co": d.get("COMUNE", ""), "pr": d.get("PROVINCIA", ""),
                "lo": d.get("LOCALITA'", ""), "t": d.get("TIPOLOGIA", ""),
                "h": measure(field(d, "ALBERO ISOLATO - ALTEZZA", "INSIEME - ALTEZZA MAX"), "m"),
                "ci": measure(field(d, "ALBERO ISOLATO - CIRCONFERENZA", "INSIEME - CIRC MAX",
                                    "INSIEME OMOGENEO - CIRCONFERENZA max"), "cm"),
                "cr": [c for k, c in CRITERIA.items() if d.get(k, "").lower().startswith("si")]}
        trees.append({k: v for k, v in tree.items() if v not in ("", None, [])})
    removed = sum(1 for v in codes.values() if v)
    head = {"version": version, "source": SOURCE, "license": "CC BY 4.0"}
    OUT.write_text(json.dumps({**head, "codes": dict(sorted(codes.items()))},
                              ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    OUT_TREES.write_text("{" + ",".join(f"{json.dumps(k)}:{json.dumps(v, ensure_ascii=False)}" for k, v in head.items())
                         + ',"trees":[\n' + ",\n".join(json.dumps(t, ensure_ascii=False, separators=(",", ":"))
                                                       for t in sorted(trees, key=lambda t: t["c"])) + "\n]}\n",
                         encoding="utf-8")
    print(f"Versione {version}: {len(codes)} codici ({removed} rimossi dall'elenco) -> {OUT}; "
          f"{len(trees)} alberi iscritti -> {OUT_TREES}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
