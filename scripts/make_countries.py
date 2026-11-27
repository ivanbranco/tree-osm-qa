#!/usr/bin/env python3
"""Scrive scripts/countries.json, l'elenco delle nazioni (e delle suddivisioni delle nazioni coperte in parte) da
disegnare sulla mappa come zone non coperte, con un clic che invita a chiederne l'aggiunta (update_boundaries.py
ne scarica i confini in data/uncovered_boundaries.geojson).

Legge uno o più risultati di query Overpass in JSON grezzo (overpass turbo: Export → raw OSM data), per esempio
nazioni e territori con un proprio codice ISO 3166-1 (Guyana francese, Sahara Occidentale…) più gli stati USA:
  area(id:3600148838)->.us;
  ( relation["type"="boundary"]["ISO3166-1"];
    relation["boundary"="administrative"]["admin_level"="4"]["ISO3166-2"~"^US-"](area.us); );
  out tags;
Le relation con ISO3166-1 diventano nazioni, quelle con solo ISO3166-2 suddivisioni (parent: le prime due lettere);
un territorio con entrambi i codici (PR e US-PR) è una nazione con parent la nazione del codice ISO3166-2.
Uso: python scripts/make_countries.py nazioni.json [stati.json ...]"""
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "countries.json"


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    units = {}
    for path in sys.argv[1:]:
        for e in json.loads(Path(path).read_text(encoding="utf-8-sig"))["elements"]:
            t = e.get("tags", {})
            iso = (t.get("ISO3166-1") or t.get("ISO3166-2") or "").upper()
            # solo confini amministrativi o contesi: niente continenti (Antartide), zone statistiche (Svalbard e Jan
            # Mayen), legali o marittime (zona economica esclusiva della Nuova Caledonia)
            if e.get("type") != "relation" or not iso or t.get("boundary") not in ("administrative", "disputed"):
                continue
            u = {"iso": iso, "rel": e["id"], "name": t.get("name:it") or t.get("name:en") or t.get("name", iso),
                 "name_en": t.get("name:en") or t.get("name", iso)}
            # parent: la nazione di una suddivisione, o di un territorio che ha anche un codice ISO3166-2 (Porto Rico
            # PR ha US-PR): se quella nazione è coperta in parte, il territorio si vede solo aprendola, come gli stati
            iso2 = (t.get("ISO3166-2") or "").upper()
            if "-" in iso:
                u["parent"] = iso[:2]
            elif "-" in iso2 and iso2[:2] != iso:
                u["parent"] = iso2[:2]
            # più relation con lo stesso codice: vale quella di admin_level più basso (la nazione, non una sua parte)
            u["_lvl"] = int(t["admin_level"]) if str(t.get("admin_level", "")).isdigit() else 99
            if iso in units and units[iso]["rel"] != e["id"]:
                keep = min(units[iso], u, key=lambda x: (x["_lvl"], x["rel"]))
                print(f"{iso}: più relation ({units[iso]['rel']}, {e['id']}), tenuta {keep['rel']}")
                u = keep
            units[iso] = u
    for u in units.values():
        del u["_lvl"]
    text = "[\n" + ",\n".join("  " + json.dumps(u, ensure_ascii=False) for _, u in sorted(units.items())) + "\n]\n"
    OUT.write_text(text, encoding="utf-8")
    print(f"{len(units)} unità -> {OUT}")


if __name__ == "__main__":
    main()
