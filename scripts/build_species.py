"""Schede delle specie (issue #79): una pagina HTML per ogni specie della pagina wiki «Tree species records», con
primati, misure tipiche, distribuzione nelle regioni del sito, monumentali e aspetto sulla mappa.

Per ora le pagine sono di prova: non hanno link da nessuna parte e chiedono ai motori di ricerca di non indicizzarle
(meta robots noindex). Si generano solo le specie indicate:

    python.exe -I scripts\\build_species.py "Acer platanoides" "Quercus robur" "Liriodendron tulipifera"

Legge data/regions/*.geojson.gz (gli alberi delle regioni), data/world/monumental.geojson.gz (i monumentali di tutto
il mondo, anche fuori dalle regioni), scripts/masaf_trees.json (l'elenco MASAF), data/regions_boundaries.geojson e
data/index.json; scrive species/<slug>.html dal modello scripts/species_template.html, con i dati della specie dentro
la pagina. I contorni delle regioni li carica la pagina da data/regions_boundaries.geojson.
"""
import collections
import gzip
import json
import math
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import fetch_trees as ft  # noqa: E402

OUT_DIR = ROOT / "species"
TEMPLATE = HERE / "species_template.html"
GRID = 0.1            # lato in gradi delle celle della mappa di densità
TOP = 5               # alberi per primato
MIN_SAMPLE = 20       # alberi minimi per mostrare una mediana
GROUP_EN = {"Italia": "Italy", "Austria": "Austria", "Francia": "France", "Spagna": "Spain", "Irlanda": "Ireland",
            "Polonia": "Poland", "Regno Unito": "United Kingdom", "Stati Uniti": "United States"}

# nome della lista wiki (con maiuscole) per ogni voce di species_limits.json
RAW_NAMES = {" ".join(k.split()).lower(): k for k in ft._RAW_LIMITS if not k.startswith("_")}
BY_OBJ = {id(v): k for k, v in ft.SPECIES_LIMITS.items()}


def wiki_name(props):
    """Nome della lista wiki per la specie dell'albero (sinonimi e scritture diverse compresi), o None."""
    lim = ft.species_limit(props)
    return RAW_NAMES.get(BY_OBJ.get(id(lim))) if lim is not None else None


def slug_of(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower().replace("×", "x")).strip("-")


def ring_area(ring):
    """Area in km² di un anello [lon, lat] sulla sfera."""
    r, s = 6371.0088, 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += math.radians(x2 - x1) * (2 + math.sin(math.radians(y1)) + math.sin(math.radians(y2)))
    return abs(s) * r * r / 2


def geom_area(g):
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    return sum(ring_area(p[0]) - sum(ring_area(h) for h in p[1:]) for p in polys)


def median(vals):
    return round(statistics.median(vals), 2) if len(vals) >= MIN_SAMPLE else None


def histogram(vals, step, cap_q=0.99):
    """Conteggi per classi di larghezza step, fino al 99° percentile (le code lunghe sono quasi sempre errori)."""
    if len(vals) < MIN_SAMPLE:
        return None
    vs = sorted(vals)
    top = vs[min(len(vs) - 1, int(len(vs) * cap_q))]
    n = max(1, math.ceil(top / step))
    bins = [0] * n
    for v in vs:
        if v <= top:
            bins[min(n - 1, int(v // step))] += 1
    return {"step": step, "bins": bins, "over": sum(1 for v in vs if v > top)}


def scatter(pairs, nx=120, ny=80, cap_q=0.99):
    """Nuvola di punti (x, y) per i grafici dei rapporti: le coppie raccolte in una griglia di nx × ny celle fino al 99°
    percentile di ciascun asse (le code lunghe sono quasi sempre errori). pts = [i, j, alberi, i, j, alberi, ...], con
    i e j indici della cella (x da i·sx a (i+1)·sx); over = coppie fuori dal grafico. Le misure su OSM sono quasi
    sempre arrotondate, quindi le celle occupate sono poche rispetto agli alberi."""
    if len(pairs) < MIN_SAMPLE:
        return None
    def top(vals):
        vs = sorted(vals)
        return vs[min(len(vs) - 1, int(len(vs) * cap_q))]
    tx, ty = top([x for x, _ in pairs]), top([y for _, y in pairs])
    if tx <= 0 or ty <= 0:
        return None
    sx, sy = tx / nx, ty / ny
    cells, over = collections.Counter(), 0
    for x, y in pairs:
        if x > tx or y > ty:
            over += 1
            continue
        cells[(min(nx - 1, int(x / sx)), min(ny - 1, int(y / sy)))] += 1
    pts = [v for (i, j), c in sorted(cells.items()) for v in (i, j, c)]
    return {"sx": round(sx, 5), "sy": round(sy, 5), "nx": nx, "ny": ny, "pts": pts, "over": over}


def species_colors():
    """«Colori delle specie» di index.html (SPECIES_COLORS): {specie: [colore, mesi o None, autunnale]}. Servono a dire
    se il disegno della specie è dedicato o quello generico."""
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    block = src[src.index("const SPECIES_COLORS = {"):]
    block = block[:block.index("};")]
    out = {}
    for m in re.finditer(r'"([A-Z][a-z]+ [a-z-]+)": \["(#[0-9a-fA-F]{6})", (null|\[[\d, ]+\])(, true)?\]', block):
        out[m.group(1)] = [m.group(2), json.loads(m.group(3)), bool(m.group(4))]
    return out


# corteccia di un colore proprio (TRUNK_COLOR in index.html: tenerli uguali)
BARK = {"Betula utilis": "#f4f1e8", "Acer griseum": "#b5532b"}


def wikipedia_info(qid):
    """Voci di Wikipedia (it, en) dell'elemento Wikidata della specie e la loro immagine principale (pageimages, la
    foto in testa alla voce): {"it": {"url", "img"}, "en": {...}}. Autore e licenza della foto li chiede la pagina a
    Commons, come la scheda dell'albero. Vuoto se la rete non risponde."""
    import urllib.parse
    import urllib.request

    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": ft.HEADERS["User-Agent"]})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    out = {}
    if not qid:
        return out
    try:
        ent = get(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")["entities"]
        ent = next(iter(ent.values()))
        for lang in ("it", "en"):
            title = (ent.get("sitelinks", {}).get(f"{lang}wiki") or {}).get("title")
            if not title:
                continue
            q = get(f"https://{lang}.wikipedia.org/w/api.php?action=query&format=json&formatversion=2&prop=pageimages"
                    f"&piprop=name&titles={urllib.parse.quote(title)}")
            page = q["query"]["pages"][0]
            out[lang] = {"url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                         "img": page.get("pageimage")}
        # ripiego: l'immagine dell'elemento Wikidata (P18)
        p18 = ent.get("claims", {}).get("P18")
        if p18:
            out["p18"] = p18[0]["mainsnak"]["datavalue"]["value"]
    except Exception as e:  # noqa: BLE001 - la foto non è indispensabile
        print(f"{qid}: Wikipedia non risponde ({e})", file=sys.stderr)
    return out


def most_common(c):
    return c.most_common(1)[0][0] if c else None


def main(names):
    want = {n: slug_of(n) for n in names}
    for n in names:
        if " ".join(n.split()).lower() not in RAW_NAMES:
            sys.exit(f"{n}: non è nella lista wiki (species_limits.json)")
    regions = ft.load_json(HERE / "regions.json")
    rinfo = {r["slug"]: r for r in regions}
    index = ft.load_json(ROOT / "data" / "index.json")["regions"]
    bounds = ft.load_json(ROOT / "data" / "regions_boundaries.geojson")["features"]
    group_of = {f["properties"]["key"]: f["properties"]["group"] for f in bounds}
    area = {f["properties"]["key"]: round(geom_area(f["geometry"]), 1) for f in bounds}

    S = {n: {"feats": [], "per_region": collections.Counter(), "grid": collections.Counter(), "ids": set(),
             "lt": collections.Counter(), "lc": collections.Counter(), "fruit": collections.Counter(),
             "it": collections.Counter(), "en": collections.Counter(), "qid": collections.Counter(),
             "issues": 0, "sample": None} for n in names}
    sp_trees = collections.Counter()      # alberi con species=* per regione
    for r in regions:
        slug = r["slug"]
        p = ROOT / "data" / "regions" / f"{slug}.geojson.gz"
        if not p.exists():
            continue
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            feats = json.load(fh)["features"]
        for f in feats:
            pr = f["properties"]
            if not pr.get("species"):
                continue
            sp_trees[slug] += 1
            n = wiki_name(pr)
            if n not in S:
                continue
            s = S[n]
            s["per_region"][slug] += 1
            s["ids"].add(pr["id"])
            lon, lat = f["geometry"]["coordinates"]
            s["grid"][(round(math.floor(lon / GRID) * GRID + GRID / 2, 3), round(math.floor(lat / GRID) * GRID + GRID / 2, 3))] += 1
            if pr.get("stump"):
                continue
            s["feats"].append((slug, f))
            s["lt"][pr.get("leaf_type") or pr.get("lt")] += 1
            s["lc"][pr.get("leaf_cycle") if pr.get("leaf_cycle") in ft.LEAF_CYCLE else pr.get("lc")] += 1
            if pr.get("fruit"):
                s["fruit"][(pr["fruit"], pr.get("fm") or "")] += 1
            for k in ("it", "en"):
                if pr.get("sp_" + k):
                    s[k][pr["sp_" + k]] += 1
            if pr.get("hint_sw"):
                s["qid"][pr["hint_sw"]] += 1
            if pr.get("issues", "|") != "|":
                s["issues"] += 1
            # albero d'esempio per «vedi sulla mappa»: nella regione più piccola, senza errori, con la chioma mappata
            if pr.get("issues", "|") == "|" and pr.get("diameter_crown"):
                key = (index.get(slug, {}).get("count", 1e12), pr["id"])
                if s["sample"] is None or key < s["sample"][0]:
                    s["sample"] = (key, slug, pr["id"])
        print(slug, len(feats), file=sys.stderr)

    # monumentali (mappa mondiale): conteggio, punti e, se non sono già nelle regioni, candidati ai primati
    with gzip.open(ROOT / "data" / "world" / "monumental.geojson.gz", "rt", encoding="utf-8") as fh:
        mon = json.load(fh)["features"]
    for n in names:
        S[n]["mon"] = []
    for f in mon:
        pr = f["properties"]
        n = wiki_name(pr)
        if n not in S:
            continue
        lon, lat = f["geometry"]["coordinates"]
        S[n]["mon"].append([round(lon, 5), round(lat, 5), pr["id"], pr.get("name") or ""])
        if pr["id"] not in S[n]["ids"] and not pr.get("stump"):
            S[n]["feats"].append((None, f))
    masaf = ft.load_json(HERE / "masaf_trees.json")["trees"]
    for n in names:
        b = ft.binomial(n)
        S[n]["masaf"] = sum(1 for t in masaf if ft.binomial(t.get("s") or "") == b)

    updated = max(v.get("updated", "") for v in index.values())
    template = TEMPLATE.read_text(encoding="utf-8")
    OUT_DIR.mkdir(exist_ok=True)
    for n, s in S.items():
        data = build_data(n, s, rinfo, index, sp_trees, group_of, area, updated)
        js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        sci = n
        en = data["names"].get("en") or ""
        html = (template.replace("{{DATA}}", js)
                .replace("{{TITLE}}", f"{sci}{' (' + en + ')' if en else ''} · DendrOSM")
                .replace("{{DESC}}", f"{sci} on OpenStreetMap: records, typical sizes, where it is mapped and "
                                     f"monumental trees, from {data['total']:,} mapped trees."))
        out = OUT_DIR / f"{want[n]}.html"
        out.write_text(html, encoding="utf-8")
        print(f"{out.relative_to(ROOT)}: {data['total']} alberi, {len(js) // 1024} kB di dati")


def build_data(n, s, rinfo, index, sp_trees, group_of, area, updated):
    lim = ft._RAW_LIMITS.get(n, {})
    # primati: come il tab «Alberi da record» (record_values, record_order, ⚠ da record_warned)
    cands = collections.defaultdict(list)
    meas = collections.defaultdict(list)       # valori non segnalati, per mediane e istogrammi
    cr_c_pts, h_c_pts = [], []                 # coppie (circonferenza, chioma o altezza) per i grafici dei rapporti
    for slug, f in s["feats"]:
        pr = f["properties"]
        vals = ft.record_values(pr)
        for k, v in vals.items():
            cands[k].append((ft.record_order(k, v, pr), v, slug, f))
            if not ft.record_warned(k, pr):
                meas[k].append(v)
        if "crown" in vals and "trunk" in vals and not ft.record_warned("crown", pr) and not ft.record_warned("trunk", pr):
            meas["crown_circ"].append(vals["crown"] / vals["trunk"])
            cr_c_pts.append((vals["trunk"], vals["crown"]))
    def rec_tree(k, v, slug, f):
        return {"v": round(v, 3), "w": ft.record_warned(k, f["properties"]), "r": slug,
                "id": f["properties"]["id"], "name": f["properties"].get("name") or "",
                "tags": {t: f["properties"][t] for t in ("height", "circumference", "diameter", "diameter_crown",
                                                        "start_date") if t in f["properties"]}}
    # per ogni primato i primi TOP, valori segnalati compresi (⚠), come il tab «Alberi da record»
    records = {}
    for k, rows in cands.items():
        rows.sort(key=lambda r: r[0])
        records[k] = [rec_tree(k, v, slug, f) for _, v, slug, f in rows[:TOP]]
    h_c = []                                   # rapporto altezza / circonferenza, come la colonna della wiki
    for slug, f in s["feats"]:
        pr = f["properties"]
        vals = ft.record_values(pr)
        if "height" in vals and "trunk" in vals and not ft.record_warned("slender", pr):
            h_c.append(vals["height"] / vals["trunk"])
            h_c_pts.append((vals["trunk"], vals["height"]))
    stats = {
        "height": {"med": median(meas["height"]), "n": len(meas["height"]), "hist": histogram(meas["height"], 1)},
        "trunk": {"med": median(meas["trunk"]), "n": len(meas["trunk"]), "hist": histogram(meas["trunk"], 0.25)},
        "crown": {"med": median(meas["crown"]), "n": len(meas["crown"]), "hist": histogram(meas["crown"], 1)},
        "h_c": {"med": median(h_c), "n": len(h_c), "sc": scatter(h_c_pts)},
        "cr_c": {"med": median(meas["crown_circ"]), "n": len(meas["crown_circ"]), "sc": scatter(cr_c_pts)},
        "oldest_n": len(meas["oldest"]),
    }
    # distribuzione: per regione e per nazione (contorno "g:<gruppo>" o la regione stessa se è una nazione)
    regs, nations = {}, collections.defaultdict(lambda: {"n": 0, "sp": 0, "all": 0, "regions": 0})
    for slug, info in index.items():
        if slug not in rinfo:
            continue
        c = s["per_region"].get(slug, 0)
        regs[slug] = {"n": c, "sp": sp_trees.get(slug, 0), "all": info.get("count", 0), "km2": area.get(slug),
                      "name": rinfo[slug]["name"], "name_en": rinfo[slug].get("name_en") or rinfo[slug]["name"]}
        g = group_of.get(slug)
        nk = f"g:{g}" if g else slug
        nat = nations[nk]
        nat.update(name=g or rinfo[slug]["name"], name_en=GROUP_EN.get(g, g) if g else regs[slug]["name_en"],
                   km2=area.get(nk))
        nat["n"] += c
        nat["sp"] += regs[slug]["sp"]
        nat["all"] += regs[slug]["all"]
        nat["regions"] += 1 if c else 0
    fruit = most_common(s["fruit"])
    sample = s["sample"]
    qid = most_common(s["qid"])
    return {
        "wp": wikipedia_info(qid),
        "spColor": species_colors().get(n),
        "bark": BARK.get(n),
        "name": n, "slug": slug_of(n), "genus": n.split()[0],
        "names": {k: most_common(s[k]) for k in ("it", "en") if s[k]},
        "qid": qid,
        "lt": most_common(s["lt"]), "lc": most_common(s["lc"]),
        "fruit": {"color": fruit[0], "months": [int(m) for m in fruit[1].split(",") if m]} if fruit else None,
        "total": sum(s["per_region"].values()), "issues": s["issues"],
        "n_regions": sum(1 for v in s["per_region"].values() if v),
        "mon": s["mon"], "masaf": s["masaf"],
        "records": records,
        "wiki": {k: lim.get(k) for k in ("height", "circumference", "height_circ_ratio", "crown_circ_ratio") if lim.get(k)},
        "stats": stats,
        "regions": regs, "nations": dict(nations),
        "grid": [[x, y, c] for (x, y), c in sorted(s["grid"].items())],
        "sample": {"r": sample[1], "id": sample[2]} if sample else None,
        "updated": updated,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
