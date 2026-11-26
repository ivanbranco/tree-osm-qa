"""Schede delle specie (issue #79): una pagina HTML per ogni specie della pagina wiki «Tree species records», con
primati, misure tipiche, distribuzione nelle regioni del sito, monumentali e aspetto sulla mappa; per le specie dioiche
anche i tratti che distinguono maschi e femmine (sex_info, dai testi di index.html).

Nelle schede degli alberi delle mappe la specie è un link alla sua pagina, per le specie elencate in
species/index.json (scritto alla fine, con tutte le pagine della cartella). Si generano solo le specie indicate:

    python.exe -I scripts\\build_species.py "Acer platanoides" "Quercus robur" "Liriodendron tulipifera"

Con --keep-isolated non si interroga QLever (qualche minuto per specie): il «più isolato» resta quello della pagina
generata prima. Serve quando è cambiato solo il modello. species/index.html (l'elenco delle schede) è scritta a mano
e legge species/index.json.

Legge data/regions/*.geojson.gz (gli alberi delle regioni), data/world/monumental.geojson.gz (i monumentali di tutto
il mondo, anche fuori dalle regioni), scripts/masaf_trees.json (l'elenco MASAF), scripts/year_events.json (le frasi
sull'anno di «Più vecchio», copiate nella pagina), data/regions_boundaries.geojson e data/index.json; scrive species/<slug>.html dal modello scripts/species_template.html, con i dati della specie dentro
la pagina, e species/<slug>.mon.json con i monumentali della specie ([lon, lat, id, nome]), che la pagina scarica
solo quando si accende la casella «Monumentali». I contorni delle regioni li carica la pagina da
data/regions_boundaries.geojson.
"""
import collections
import gzip
import html
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
SITE = "https://ivanbranco.github.io/tree-osm-qa/"      # indirizzo del sito, per l'anteprima sui social (og:url)
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


def species_spots():
    """«Chiome a macchiette» di index.html (SPECIES_SPOTS): {specie: {"leaf": [chioma, macchietta, macchietta],
    "autumn": [...]}}. Anche queste fanno dedicato il disegno della specie."""
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    block = src[src.index("const SPECIES_SPOTS = {"):]
    block = block[:block.index("};")]
    cols = r'\[((?:"#[0-9a-fA-F]{6}",? ?){3})\]'
    return {m.group(1): {"leaf": re.findall(r"#[0-9a-fA-F]{6}", m.group(2)), "autumn": re.findall(r"#[0-9a-fA-F]{6}", m.group(3))}
            for m in re.finditer(r'"([A-Z][a-z]+ [a-z-]+)": \{ leaf: ' + cols + r', autumn: ' + cols + r' \}', block)}


# corteccia di un colore proprio (TRUNK_COLOR in index.html: tenerli uguali; Populus alba: il colore del tronco giovane)
BARK = {"Betula utilis": "#f4f1e8", "Acer griseum": "#b5532b", "Populus alba": "#d3d3ca"}


_SEX_ENTRY = re.compile(r'(?:"([^"]+)"|([A-Z][a-z]+)): \{ female: ("(?:[^"\\]|\\.)*"),\s*male: ("(?:[^"\\]|\\.)*") \}')


def _sex_entries(text):
    """Voci «chiave: { female: "...", male: "..." }» di un pezzo di index.html: {chiave: {"female", "male"}}."""
    return {m.group(1) or m.group(2): {"female": json.loads(m.group(3)), "male": json.loads(m.group(4))}
            for m in _SEX_ENTRY.finditer(text)}


def sex_info(n, counts):
    """I due sessi di una specie dioica, per il paragrafo «Maschio e femmina» della scheda: i tratti che li distinguono
    (L.sexTraits di index.html, in italiano e in inglese: tenuti lì, qui si leggono soltanto), la foto di Commons e la
    sua didascalia se ci sono (SEX_PHOTO, L.sexPhotoAlt) e gli alberi con sex=male / sex=female nelle regioni.
    La chiave è la specie o, per tutte le sue specie, il genere. None se la specie non è in L.sexTraits."""
    src = (ROOT / "index.html").read_text(encoding="utf-8")

    def blocks(start, end):         # i pezzi tra start e end: il primo è dei testi italiani, il secondo degli inglesi
        out, i = [], src.find(start)
        while i >= 0:
            j = src.index(end, i)
            out.append(_sex_entries(src[i:j]))
            i = src.find(start, j)
        return out
    traits, alts = blocks("sexTraits: {", "\n    },"), blocks("sexPhotoAlt: {", "\n")
    photos = (blocks("const SEX_PHOTO = {", "\n") or [{}])[0]
    if len(traits) != 2:
        sys.exit("index.html: sexTraits non trovato due volte (it, en)")
    keys = [n, n.split()[0]]
    key = next((k for k in keys if k in traits[0] and k in traits[1]), None)
    if key is None:
        return None
    pk = next((k for k in keys if k in photos), None)
    out = {"n": {x: counts.get(x, 0) for x in ("male", "female")}}
    for x in ("male", "female"):
        out[x] = {"it": traits[0][key][x], "en": traits[1][key][x]}
        if pk:
            out[x]["img"] = photos[pk][x]
            out[x]["alt"] = {lang: a[pk][x] for lang, a in zip(("it", "en"), alts) if pk in a}
    return out


def synonyms_of(n, counts):
    """Sinonimi di whitelist.json che valgono come la specie n, contati con lei in tutte le statistiche della pagina:
    [[nome come in whitelist.json, alberi nelle regioni con quel nome]], dal più usato. Vuoto se non ne ha."""
    target = ft.norm_species(n)
    out = [[alt, counts.get(ft.norm_species(alt), 0)] for alt, name in ft._WL.get("synonyms", {}).items()
           if ft.norm_species(name) == target]
    return sorted(out, key=lambda r: (-r[1], r[0]))


# Sottospecie, varietà e cultivar degli alberi contati come la specie: i tag letti, in ordine (il primo che c'è).
# Solo i tag: gli alberi con la cultivar o la varietà scritta in species non hanno il nome della lista (wiki_name)
INFRA_KEYS = {"ssp": ("taxon:subspecies",), "var": ("taxon:variety", "variety"), "cv": ("taxon:cultivar", "cultivar")}
RANK_WORD = {"ssp": r"(?:subsp|ssp)\.?", "var": r"var\.?", "cv": r"cv\.?"}
NO_QUOTES = str.maketrans("", "", "'‘’`´\"“”")


def infra_value(n, k, v):
    """Valore di sottospecie, varietà o cultivar da contare per la specie n, per riunire le scritture diverse dello
    stesso nome: senza virgolette e spazi in più, senza la specie e il rango davanti ("Acer platanoides 'Globosum'" ->
    "Globosum", "Acer platanoides var. Emerald Queen" -> "Emerald Queen"). La sottospecie è il nome intero con
    "subsp." ("laricio" e "Pinus nigra ssp. laricio" -> "Pinus nigra subsp. laricio"). "" se non resta niente."""
    v = " ".join(v.translate(NO_QUOTES).split())
    # la specie davanti, anche con x o senza il segno di ibrido
    sp = r"\s+".join("[×xX]?" if w == "×" else re.escape(w) for w in n.split()).replace(r"\s+[×xX]?\s+", r"\s+(?:[×xX]\s+)?")
    v = re.sub(rf"^{sp}(?:\s+|$)", "", v, flags=re.I)
    v = re.sub(rf"^{RANK_WORD[k]}\s+", "", v, flags=re.I).strip()
    if k == "ssp" and v:
        return f"{n} subsp. {v}" if " " not in v else re.sub(r"(?<!\S)ssp\.?(?!\S)", "subsp.", v)
    return v


def infra_of(counts):
    """{"ssp" | "var" | "cv": [[nome, alberi]]}, dal più usato; le scritture che differiscono solo per le maiuscole
    sono un nome solo, scritto come la più usata. Senza le chiavi vuote."""
    out = {}
    for k, c in counts.items():
        by = {}
        for v, m in c.items():
            by.setdefault(v.casefold(), collections.Counter())[v] = m
        rows = [[f.most_common(1)[0][0], sum(f.values())] for f in by.values()]
        if rows:
            out[k] = sorted(rows, key=lambda r: (-r[1], r[0]))
    return out


def wikipedia_info(qid):
    """Voci di Wikipedia (it, en) dell'elemento Wikidata della specie e la loro immagine principale (pageimages, la
    foto in testa alla voce): {"it": {"url", "img"}, "en": {...}}. Autore e licenza della foto li chiede la pagina a
    Commons, come la scheda dell'albero. Vuoto senza qid; None se Wikidata, Wikipedia o Commons non rispondono (main
    riprende allora le voci della pagina generata prima, se c'è)."""
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
        # anteprima sui social (og:image): l'indirizzo diretto della foto della voce inglese (o italiana, o P18),
        # larga 1200 px, chiesto a Commons
        img = (out.get("en") or {}).get("img") or (out.get("it") or {}).get("img") or out.get("p18")
        if img:
            q = get("https://commons.wikimedia.org/w/api.php?action=query&format=json&formatversion=2&prop=imageinfo"
                    f"&iiprop=url&iiurlwidth=1200&titles={urllib.parse.quote('File:' + img)}")
            info = (q["query"]["pages"][0].get("imageinfo") or [{}])[0]
            if info.get("thumburl"):
                out["og"] = info["thumburl"].split("?")[0]      # senza i parametri utm_* aggiunti dall'API
    except Exception as e:  # noqa: BLE001 - la foto non è indispensabile
        print(f"{qid}: Wikipedia non risponde ({e})", file=sys.stderr)
        return None
    return out


def most_common(c):
    return c.most_common(1)[0][0] if c else None


def main(names, keep_isolated=False):
    for n in names:
        if " ".join(n.split()).lower() not in RAW_NAMES:
            sys.exit(f"{n}: non è nella lista wiki (species_limits.json)")
    # il nome scritto come nella lista wiki ("quercus robur" -> "Quercus robur"): è quello che dà wiki_name agli alberi
    names = list(dict.fromkeys(RAW_NAMES[" ".join(n.split()).lower()] for n in names))
    want = {n: slug_of(n) for n in names}
    regions = ft.load_json(HERE / "regions.json")
    rinfo = {r["slug"]: r for r in regions}
    index = ft.load_json(ROOT / "data" / "index.json")["regions"]
    bounds = ft.load_json(ROOT / "data" / "regions_boundaries.geojson")["features"]
    group_of = {f["properties"]["key"]: f["properties"]["group"] for f in bounds}
    area = {f["properties"]["key"]: round(geom_area(f["geometry"]), 1) for f in bounds}

    S = {n: {"feats": [], "per_region": collections.Counter(), "grid": collections.Counter(), "ids": set(),
             "lt": collections.Counter(), "lc": collections.Counter(), "fruit": collections.Counter(),
             "it": collections.Counter(), "en": collections.Counter(), "qid": collections.Counter(),
             "sex": collections.Counter(), "syn": collections.Counter(), "issues": 0, "sample": None,
             "infra": {k: collections.Counter() for k in INFRA_KEYS}} for n in names}
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
            if (ns := ft.norm_species(pr["species"])) in ft.SYNONYMS:      # contato con il nome di un sinonimo
                s["syn"][ns] += 1
            for k, keys in INFRA_KEYS.items():          # sottospecie, varietà e cultivar dei tag (infra_of)
                for v in {infra_value(n, k, x) for x in str(next((pr[t] for t in keys if pr.get(t)), "")).split(";")} - {""}:
                    s["infra"][k][v] += 1
            lon, lat = f["geometry"]["coordinates"]
            s["grid"][(round(math.floor(lon / GRID) * GRID + GRID / 2, 3), round(math.floor(lat / GRID) * GRID + GRID / 2, 3))] += 1
            if pr.get("issues", "|") != "|":   # segnalati: ceppi compresi, come il totale (per_region)
                s["issues"] += 1
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
            if pr.get("sex"):
                s["sex"][pr["sex"]] += 1
            if pr.get("hint_sw"):
                s["qid"][pr["hint_sw"]] += 1
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
    rels = [rinfo[slug]["rel"] for slug in index if slug in rinfo and rinfo[slug].get("rel")]
    for n, s in S.items():
        data = build_data(n, s, rinfo, index, sp_trees, group_of, area, updated)
        if data["wp"] is None:      # Wikipedia non risponde: foto e link della pagina generata prima, se c'è
            data["wp"] = prev_wp(want[n])
            print(f"{n}: {'foto e link di Wikipedia dalla pagina precedente' if data['wp'] else 'pagina senza foto né link a Wikipedia'}",
                  file=sys.stderr)
        iso = None if keep_isolated else species_isolated(n, s, rels)
        if iso is None:     # QLever non risponde o --keep-isolated: resta la lista della pagina generata prima, se c'è
            iso = prev_isolated(want[n])
        if iso:
            data["records"]["isolated"] = iso
        js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        sci = n
        en = data["names"].get("en") or ""
        # anteprima sui social: con la foto la scheda grande, senza solo titolo e descrizione
        og = data["wp"].get("og")
        og_img = (f'<meta property="og:image" content="{html.escape(og)}">\n'
                  f'<meta property="og:image:alt" content="{html.escape(sci)}">\n') if og else ""
        page = (template.replace("{{DATA}}", js)
                .replace("{{TITLE}}", html.escape(f"{sci}{' (' + en + ')' if en else ''} · DendrOSM"))
                .replace("{{DESC}}", html.escape(f"{sci} on OpenStreetMap: records, typical sizes, where it is mapped "
                                                 f"and monumental trees, from {data['total']:,} mapped trees."))
                .replace("{{URL}}", f"{SITE}species/{want[n]}.html")
                .replace("{{OG_IMAGE}}", og_img)
                .replace("{{CARD}}", "summary_large_image" if og else "summary"))
        out = OUT_DIR / f"{want[n]}.html"
        out.write_text(page, encoding="utf-8")
        mon = json.dumps(s["mon"], ensure_ascii=False, separators=(",", ":"))
        (OUT_DIR / f"{want[n]}.mon.json").write_text(mon + "\n", encoding="utf-8")
        print(f"{out.relative_to(ROOT)}: {data['total']} alberi, {len(js) // 1024} kB di dati, "
              f"{len(mon) // 1024} kB di monumentali")
    write_index()


def species_isolated(n, s, rels):
    """«Più isolato» della specie (come il tab «Alberi da record», ft.isolated_top): i primi TOP tra gli alberi della
    specie delle pagine (regioni del sito e monumentali fuori dalle regioni), con la distanza dal più vicino tra gli
    altri alberi, i boschi e i filari di tutto il mondo. A QLever si chiedono gli alberi con uno dei valori di species
    usati da quegli alberi, dentro le relazioni delle regioni (rels) o tra i monumentali fuori dalle regioni; [] senza
    alberi, None se QLever non risponde."""
    where = {f["properties"]["id"]: slug for slug, f in s["feats"]}
    if not where:
        return []
    names = sorted({f["properties"]["species"] for _, f in s["feats"] if f["properties"].get("species")})
    extra = [i for i, slug in where.items() if slug is None]
    left = (f"VALUES ?sp {{ {' '.join(json.dumps(x, ensure_ascii=False) for x in names)} }}\n"
            f"  ?tree osmkey:species ?sp .\n"
            f"  {{ VALUES ?rel {{ {' '.join(f'osmrel:{r}' for r in rels)} }} ?rel ogc:sfContains ?tree }}"
            + (f" UNION {{ VALUES ?tree {{ {' '.join(f'osmnode:{i}' for i in extra)} }} }}" if extra else ""))
    rows = ft.isolated_top(left, where, n, TOP)
    if rows is None:
        return None
    feats = {f["properties"]["id"]: f for _, f in s["feats"]}
    out = []
    for tid, km, ref, kind, cap in rows:
        t = {"v": round(km, 3), "w": False, "r": where[tid], "id": tid,
             "name": feats[tid]["properties"].get("name") or "", "tags": {}}
        if cap:
            t["cap"] = 1
        else:
            t["nk"] = kind
        out.append(t)
    return out


def prev_isolated(slug):
    """«Più isolato» della pagina species/<slug>.html generata prima (None se manca)."""
    p = OUT_DIR / f"{slug}.html"
    d = page_data(p) if p.exists() else None
    return (d.get("records") or {}).get("isolated") if d else None


def prev_wp(slug):
    """Voci di Wikipedia e foto della pagina species/<slug>.html generata prima ({} se manca)."""
    p = OUT_DIR / f"{slug}.html"
    d = page_data(p) if p.exists() else None
    return (d.get("wp") or {}) if d else {}


def page_data(p):
    """Dati dentro una pagina species/<slug>.html già scritta (None se non è una scheda)."""
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', p.read_text(encoding="utf-8"), re.S)
    try:
        return json.loads(m.group(1).replace("<\\/", "</")) if m else None
    except ValueError:
        return None


def write_index():
    """species/index.json, da tutte le schede della cartella (anche quelle generate prima):
    - "pages": {nome in minuscolo: slug}. Lo legge index.html per il link sulla specie nella scheda dell'albero; il
      nome è quello della lista wiki, come sp_key degli alberi;
    - "list": una voce per scheda, in ordine di nome, per l'elenco species/index.html (pagina scritta a mano): nome,
      slug, nomi comuni, alberi, monumentali, tipo di foglia e ciclo, foto (quella della scheda in inglese)."""
    pages, rows = {}, []
    for p in sorted(OUT_DIR.glob("*.html")):
        d = page_data(p)
        if not d or d.get("slug") != p.stem:
            continue
        pages[" ".join(d["name"].split()).lower()] = p.stem
        wp = d.get("wp") or {}
        img = (wp.get("en") or {}).get("img") or (wp.get("it") or {}).get("img") or wp.get("p18")
        row = {"name": d["name"], "slug": p.stem, "it": d["names"].get("it"), "en": d["names"].get("en"),
               "n": d["total"], "mon": d.get("mon_n", 0), "lt": d.get("lt"), "lc": d.get("lc"), "img": img}
        rows.append({k: v for k, v in row.items() if v is not None})
    rows.sort(key=lambda r: r["name"].lower())
    (OUT_DIR / "index.json").write_text(json.dumps({"pages": pages, "list": rows}, ensure_ascii=False, indent=1) + "\n",
                                        encoding="utf-8")
    print(f"species/index.json: {len(pages)} pagine")


def year_events(n, oldest, rinfo):
    """Frase sull'anno per gli alberi di «Più vecchio» (come il tab «Alberi da record»): la voce di
    scripts/year_events.json per la data dell'albero (ft.year_event, con il codice ISO della sua regione; i monumentali
    fuori dalle regioni hanno solo le voci per tutti) va nella pagina come "ev". Le date senza una voce, né per l'anno
    né per il decennio, si scrivono nel log, da aggiungere a mano; dopo averle aggiunte si rifà la pagina."""
    events = ft.load_json(ft.YEAR_EVENTS)
    missing = []
    for t in oldest:
        key = ft.start_date_event_key(ft.tree_date(t["tags"]))   # start_date o, se manca, planted_date
        if not key:
            continue
        ev = ft.year_event(key, events, (rinfo.get(t["r"]) or {}).get("iso", ""))
        if ev:
            t["ev"] = ev
        else:
            missing.append(key)
    if missing:
        print(f"{n}: anni senza una voce in scripts/year_events.json: {', '.join(dict.fromkeys(missing))}",
              file=sys.stderr)


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
                                                        "start_date", "planted_date") if t in f["properties"]}}
    # per ogni primato i primi TOP, valori segnalati compresi (⚠), come il tab «Alberi da record»
    records = {}
    for k, rows in cands.items():
        rows.sort(key=lambda r: r[0])
        records[k] = [rec_tree(k, v, slug, f) for _, v, slug, f in rows[:TOP]]
    year_events(n, records.get("oldest", []), rinfo)
    h_c = []                                   # rapporto altezza / circonferenza, come la colonna della wiki
    for slug, f in s["feats"]:
        pr = f["properties"]
        vals = ft.record_values(pr)
        if "height" in vals and "trunk" in vals and not ft.record_warned("slender", pr):
            h_c.append(vals["height"] / vals["trunk"])
            h_c_pts.append((vals["trunk"], vals["height"]))
    # max: il valore più grande non segnalato, per il righello sotto il grafico (mediana, max, record della wiki)
    top = lambda k: round(max(meas[k]), 2) if meas[k] else None   # noqa: E731
    stats = {
        "height": {"med": median(meas["height"]), "n": len(meas["height"]), "hist": histogram(meas["height"], 1),
                   "max": top("height")},
        "trunk": {"med": median(meas["trunk"]), "n": len(meas["trunk"]), "hist": histogram(meas["trunk"], 0.25),
                  "max": top("trunk")},
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
        "spots": species_spots().get(n),
        "bark": BARK.get(n),
        "sex": sex_info(n, s["sex"]),
        "synonyms": synonyms_of(n, s["syn"]),
        "infra": infra_of(s["infra"]),
        "name": n, "slug": slug_of(n), "genus": n.split()[0],
        "names": {k: most_common(s[k]) for k in ("it", "en") if s[k]},
        "qid": qid,
        "lt": most_common(s["lt"]), "lc": most_common(s["lc"]),
        "fruit": {"color": fruit[0], "months": [int(m) for m in fruit[1].split(",") if m]} if fruit else None,
        "total": sum(s["per_region"].values()), "issues": s["issues"],
        "n_regions": sum(1 for v in s["per_region"].values() if v),
        "mon_n": len(s["mon"]), "masaf": s["masaf"],      # i punti dei monumentali sono in <slug>.mon.json
        "records": records,
        "wiki": {k: lim.get(k) for k in ("height", "circumference", "height_circ_ratio", "crown_circ_ratio") if lim.get(k)},
        "stats": stats,
        "regions": regs, "nations": dict(nations),
        "grid": [[x, y, c] for (x, y), c in sorted(s["grid"].items())],
        "sample": {"r": sample[1], "id": sample[2]} if sample else None,
        "updated": updated,
    }


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--keep-isolated"]
    if not args:
        sys.exit(__doc__)
    main(args, keep_isolated=len(args) < len(sys.argv) - 1)
