# Plugin «DendrOSM» per Every Door (issue #52), documentato in docs/everydoor.md.
# Genera everydoor/dendrosm.edp (il file da installare) e il suo contenuto non compresso in everydoor/plugin/
# (plugin.yaml, icons/, langs/; non va nel repository), e aggiorna everydoor/version.json.
# I tag di ogni specie vengono da scripts/species_list.json e scripts/genus_list.json (le liste di riferimento del
# sito); i nomi comuni da Wikidata (everydoor/names.json, scritto da everydoor/fetch_names.py).
# Uso (dalla cartella del repository):  python -I everydoor/build_plugin.py [--preview]
# --preview rifà anche docs/img/everydoor-icons.png (da usare quando cambiano le icone).
# plugin.yaml e langs/*.yaml sono scritti in JSON, che è YAML valido: così non serve una libreria YAML.
import hashlib, json, math, re, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "everydoor"
PLUGIN = OUT / "plugin"
SPECIES = json.loads((ROOT / "scripts/species_list.json").read_text(encoding="utf-8"))["species"]
GENERA = json.loads((ROOT / "scripts/genus_list.json").read_text(encoding="utf-8"))["genera"]

# Colori e forme della legenda di index.html
DECIDUOUS, EVERGREEN, NOCYCLE, OUTLINE, STUMP, ISSUE = "#a5d64c", "#1f6b35", "#5fa36f", "#1b6b2c", "#5a3a1a", "#d00020"
# gruppo -> (forma, colore, etichetta per l'anteprima)
GROUPS = {
    "broad_dec":  ("blob",  DECIDUOUS, "Latifoglia decidua"),
    "broad_ev":   ("blob",  EVERGREEN, "Latifoglia sempreverde"),
    "needle_dec": ("spiky", DECIDUOUS, "Aghifoglia decidua"),
    "needle_ev":  ("spiky", EVERGREEN, "Aghifoglia sempreverde"),
    "palm":       ("palm",  EVERGREEN, "Palma"),
}

# Nomi comuni da Wikidata (everydoor/fetch_names.py). Senza il file il plugin si genera lo stesso, solo coi nomi scientifici.
_names = OUT / "names.json"
NAMES = json.loads(_names.read_text(encoding="utf-8")) if _names.exists() else {}
# Altri nomi che devono portare alla specie cercando: i sinonimi di scripts/whitelist.json (nome alternativo -> nome
# della lista), gli stessi che il sito conta come una sola specie
EXTRA_WORDS = {}
for _alt, _name in json.loads((ROOT / "scripts/whitelist.json").read_text(encoding="utf-8")).get("synonyms", {}).items():
    if isinstance(_name, str) and not _alt.startswith("_"):
        EXTRA_WORDS.setdefault(_name, []).append(_alt)
FIRST_VERSION = 4     # le prove sul telefono sono arrivate alla 3
# Generi di palma: gli stessi del sito (scripts/fetch_trees.py). Nelle liste sono latifoglie sempreverdi.
PALM_GENERA = set(re.search(r'PALM_GENERA = frozenset\("""(.*?)"""', (ROOT / "scripts/fetch_trees.py").read_text(encoding="utf-8"), re.S).group(1).split())
# Altri tag da cui il sito riconosce una palma (is_palm in scripts/fetch_trees.py). Every Door confronta i valori
# così come sono scritti: valgono solo queste grafie.
PALM_TAGS = [("taxon:family", "Arecaceae"), ("family", "Arecaceae"), ("taxon", "Arecaceae"), ("taxon:en", "Palm"), ("crown_type", "I")]
# Voci della lista che non sono un nome «Genere epiteto» (anche ibrido): non diventano scorciatoie
NAME_RE = re.compile(r"^(× )?[A-Z][a-z-]+ (× )?[a-z-]+( × [a-z-]+)?$")
# Every Door 7.1 e 8.0-alpha: ogni valore di «when» riceve come regole l'intera mappa di «when» (vedi chain()).
# Quando il difetto sarà corretto nell'app, mettere False e rigenerare.
WHEN_BUG = True
# Tipi di oggetto del plugin: nome italiano, nome inglese
TYPE_NAMES = {
    "broad_dec": ("Latifoglia decidua", "Broadleaved deciduous"),
    "broad_ev": ("Latifoglia sempreverde", "Broadleaved evergreen"),
    "needle_dec": ("Aghifoglia decidua", "Needleleaved deciduous"),
    "needle_ev": ("Aghifoglia sempreverde", "Needleleaved evergreen"),
    "palm": ("Palma", "Palm"),
}
LEAF = {"broad": "broadleaved", "needle": "needleleaved", "dec": "deciduous", "ev": "evergreen"}
# Alberi con uno solo dei due tag delle foglie, come sul sito: con leaf_type la forma della chioma nel colore di
# «ciclo fogliare non specificato»; con leaf_cycle il cerchio nel colore del ciclo.
# id -> (forma, colore, tag che lo riconosce, nome italiano, nome inglese)
PARTIAL_TYPES = {
    "broad": ("blob", NOCYCLE, ("leaf_type", "broadleaved"), "Latifoglia", "Broadleaved"),
    "needle": ("spiky", NOCYCLE, ("leaf_type", "needleleaved"), "Aghifoglia", "Needleleaved"),
    "any_dec": ("circle", DECIDUOUS, ("leaf_cycle", "deciduous"), "Caducifoglia", "Deciduous"),
    "any_ev": ("circle", EVERGREEN, ("leaf_cycle", "evergreen"), "Sempreverde", "Evergreen"),
}


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower().replace("×", "x")).strip("_")


def genus_of(name):
    return re.sub(r"^× ", "", name).split()[0]


def group_of(name, leaf_type, leaf_cycle):
    """Gruppo della legenda, o «neutral» se tipo o ciclo non sono tra quelli della legenda."""
    if genus_of(name).lower() in PALM_GENERA:
        return "palm"
    lt = {"broadleaved": "broad", "needleleaved": "needle"}.get(leaf_type)
    lc = {"deciduous": "dec", "evergreen": "ev"}.get(leaf_cycle)
    return f"{lt}_{lc}" if lt and lc else "neutral"


# ---------------------------------------------------------------- icone (riquadro 64×64, centro 32,32)
def shape_pts(kind, r=24.0, c=32.0):
    """Sagome semplificate di quelle di crownOutline (index.html): meno lobi e punte, incavi più profondi."""
    pts = []
    if kind == "blob":                     # sito: 6-8 lobi profondi il 16% del raggio; qui 7 lobi, 22%
        n = 84
        for k in range(n):
            a = k / n * 2 * math.pi
            rr = r * (0.78 + 0.22 * abs(math.sin(3.5 * a)))
            pts.append((c + rr * math.cos(a - math.pi / 2), c + rr * math.sin(a - math.pi / 2)))
        # 7 lobi non sono simmetrici tra alto e basso: si riporta il centro dell'ingombro al centro del riquadro
        dy = c - (min(y for _, y in pts) + max(y for _, y in pts)) / 2
        pts = [(x, y + dy) for x, y in pts]
    elif kind == "spiky":                  # sito: 14 punte, raggio interno 0.74; qui 10 punte, 0.64
        n, kin = 10, 0.64
        for k in range(2 * n):
            a = -math.pi / 2 + k * math.pi / n
            rr = r if k % 2 == 0 else r * kin
            pts.append((c + rr * math.cos(a), c + rr * math.sin(a)))
    elif kind == "palm":                   # sito: 8-10 fronde, incavo 0.3; qui 8 fronde uguali, incavo 0.34
        n, m, kin = 8, 8, 0.34
        rr = [kin + (1 - kin) * (1 - abs(2 * j / m - 1)) ** 0.6 for _ in range(n) for j in range(m)]
        da = 2 * math.pi / len(rr)
        rot = -math.pi / 2 - (m / 2) * da
        r = r * 1.08                       # le fronde sono sottili: un po' più grande, a parità di ingombro visivo
        pts = [(c + r * v * math.cos(rot + k * da), c + r * v * math.sin(rot + k * da)) for k, v in enumerate(rr)]
    return pts


def icon_prims(kind, color, variant=None):
    """Primitive dell'icona: ('poly', punti, riempimento, contorno, spessore) | ('circle', cx, cy, r, riemp., cont., sp.)
    | ('line', x1, y1, x2, y2, contorno, spessore). variant: None, 'part' (mancano tag: punto bianco al centro, come
    il punto bianco di Every Door) o 'bad' (tag che non corrispondono: anello rosso, come «Con problemi» sul sito)."""
    p = []
    if kind == "stump":
        for col, w in (("#ffffff", 15), (STUMP, 10)):
            p += [("line", 17, 17, 47, 47, col, w), ("line", 47, 17, 17, 47, col, w)]
        return p
    if kind == "circle":
        p += [("circle", 32, 32, 23, "#ffffff", "#ffffff", 8), ("circle", 32, 32, 23, color, OUTLINE, 4)]
    else:
        pts = shape_pts(kind)
        p += [("poly", pts, "#ffffff", "#ffffff", 8), ("poly", pts, color, OUTLINE, 4)]   # alone bianco, poi la chioma
    if variant == "part":
        p.append(("circle", 32, 32, 8.5, "#ffffff", "#111111", 3.5))
    elif variant == "bad":
        p.append(("circle", 32, 32, 29.5, None, ISSUE, 4.5))
    return p


def prims_svg(prims, mono=False):
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">']
    f = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")
    for pr in prims:
        if pr[0] == "poly":
            _, pts, fill, stroke, w = pr
            out.append(f'<polygon points="{" ".join(f(x) + "," + f(y) for x, y in pts)}" fill="{fill or "none"}" '
                       f'stroke="{stroke}" stroke-width="{w}" stroke-linejoin="round"/>')
        elif pr[0] == "circle":
            _, cx, cy, r, fill, stroke, w = pr
            out.append(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r)}" fill="{fill or "none"}" stroke="{stroke}" stroke-width="{w}"/>')
        else:
            _, x1, y1, x2, y2, stroke, w = pr
            out.append(f'<path d="M{f(x1)} {f(y1)}L{f(x2)} {f(y2)}" fill="none" stroke="{stroke}" stroke-width="{w}" stroke-linecap="round"/>')
    out.append("</svg>")
    svg = "\n".join(out)
    return svg


def all_icons():
    icons = {}
    for g, (kind, color, _) in GROUPS.items():
        for v in (None, "part", "bad"):
            icons[g + ("_" + v if v else "")] = icon_prims(kind, color, v)
    for v in (None, "part", "bad"):
        icons["neutral" + ("_" + v if v else "")] = icon_prims("circle", NOCYCLE, v)
    for g, (kind, color, *_) in PARTIAL_TYPES.items():
        for v in (None, "part", "bad"):
            icons[g + ("_" + v if v else "")] = icon_prims(kind, color, v)
    icons["stump"] = icon_prims("stump", STUMP)
    return icons


# ---------------------------------------------------------------- plugin.yaml
def chain(conds, last):
    """Regole da soddisfare tutte insieme: conds = [(chiave, [valori ammessi]), ...], poi last = (chiave, [valori]).
    In Every Door le regole allo stesso livello sono alternative, quindi le condizioni vanno annidate con «when»."""
    if not conds:
        return {last[0]: {"only": last[1]}}
    (key, values), nxt = conds[0], chain(conds[1:], last)
    if WHEN_BUG:      # le chiavi di «when» sono lette anche come chiavi di tag: la regola successiva sta accanto ai valori
        return {key: {"when": {**{v: {} for v in values}, **nxt}}}
    return {key: {"when": {v: nxt for v in values}}}


def build_data():
    """Tipi di oggetto = i gruppi della legenda. Un albero è riconosciuto dai tag delle foglie; se mancano, dalla
    specie o dal genere (preset «di riconoscimento», uno per specie e per genere, che portano il nome del gruppo).
    Le scorciatoie di specie e generi si trovano cercando e applicano i loro tag, ma non sono mai il tipo
    riconosciuto di un albero (i loro «tags» contengono un tag che nessun oggetto ha)."""
    presets, micro_markers, tr = {}, {}, {"it": {}, "en": {}}
    NEVER = {"natural": "tree", "dendrosm": "shortcut"}
    by_group, skipped = {g: [] for g in GROUPS}, []
    LABELS = {**TYPE_NAMES, **{g: (it, en) for g, (_k, _c, _t, it, en) in PARTIAL_TYPES.items()}}

    def add_type(pid, g, tags, base=None, add=None):
        it, en = LABELS[g]
        presets[pid] = {"name": base or f"{it} · {en}", "icon": g + ".svg", "area": False, "tags": tags, "fields": ["@natural/tree"]}
        if add:
            presets[pid]["addTags"] = add
        tr["it"][pid], tr["en"][pid] = {"name": it}, {"name": en}
        micro_markers[pid] = {"icon": g + ".svg"}

    # 1. Tipi riconosciuti da tutti e due i tag delle foglie (3 tag: vincono sui preset da 2), palma e ceppo.
    # Tra preset con lo stesso numero di tag Every Door prende il primo: l'ordine di questa funzione conta.
    # Prima di tutti, le palme riconosciute dagli altri tag che usa il sito (PALM_TAGS): valgono più dei tag delle
    # foglie, quindi servono le varianti con leaf_type e leaf_cycle (più tag = precedenza). addTags ha solo
    # natural=tree perché cambiando tipo Every Door toglie i tag di addTags: taxon:family e simili devono restare.
    for i, (k, v) in enumerate(PALM_TAGS):
        for j, extra in enumerate(({"leaf_type": "broadleaved", "leaf_cycle": "evergreen"}, {"leaf_type": "broadleaved"},
                                   {"leaf_cycle": "evergreen"}, {})):
            add_type(f"dendrosm_palmtag_{i}_{j}", "palm", {"natural": "tree", k: v, **extra}, base="DendrOSM", add={"natural": "tree"})
    for g in TYPE_NAMES:
        add_type("dendrosm_" + g, g, {"natural": "tree", "leaf_type": "palm"} if g == "palm" else
                 {"natural": "tree", "leaf_type": LEAF[g.split("_")[0]], "leaf_cycle": LEAF[g.split("_")[1]]})
    presets["dendrosm_stump"] = {"name": "Ceppo · tree stump", "icon": "stump.svg", "area": False,
                                 "tags": {"natural": "tree_stump"}, "fields": ["@natural/tree_stump"]}
    tr["it"]["dendrosm_stump"], tr["en"]["dendrosm_stump"] = {"name": "Ceppo"}, {"name": "Tree stump"}
    micro_markers["dendrosm_stump"] = {"icon": "stump.svg"}
    micro_markers["natural/tree"] = {"icon": "neutral.svg"}    # nessun tag delle foglie, né specie o genere in lista

    # 2. scorciatoie delle specie
    detect = []                                                # preset di riconoscimento, aggiunti dopo le scorciatoie
    for name, ref in SPECIES.items():
        if not NAME_RE.match(name):
            skipped.append(name)
            continue
        g = group_of(name, ref["leaf_type"], ref.get("leaf_cycle"))
        nm = NAMES.get(name, {})
        pid = "dendrosm_sp_" + slug(name)
        assert pid not in presets, pid
        add = {"natural": "tree", "species": name, "species:wikidata": ref["wikidata"], "leaf_type": ref["leaf_type"]}
        if ref.get("leaf_cycle"):
            add["leaf_cycle"] = ref["leaf_cycle"]
        # il nome di base è l'unico che Every Door indicizza per la ricerca: contiene tutti i nomi
        base = [name] + [x for x in (nm.get("it"), nm.get("en")) if x] + nm.get("words", []) + EXTRA_WORDS.get(name, [])
        presets[pid] = {"name": " · ".join(base), "icon": g + ".svg", "area": False, "tags": NEVER, "addTags": add,
                        "fields": ["@natural/tree"]}
        for lang in ("it", "en"):
            tr[lang][pid] = {"name": f"{name} ({nm[lang]})" if nm.get(lang) else name}
        if g in by_group:
            by_group[g].append(name)
            own = {"natural": "tree", "species": name, "species:wikidata": ref["wikidata"]}
            if g == "palm":                                    # palma con i tag della lista (latifoglia sempreverde)
                detect.append(("dendrosm_dp_" + slug(name), g, {"natural": "tree", "species": name, "leaf_type": ref["leaf_type"],
                                                               "leaf_cycle": ref["leaf_cycle"]}, own))
            detect.append(("dendrosm_ds_" + slug(name), g, {"natural": "tree", "species": name}, own))

    # 3. scorciatoie dei generi (non i sinonimi): leaf_cycle solo se è uno solo per tutto il genere
    genus_group, genus_mixed = {g: [] for g in GROUPS}, {}
    for genus, ref in GENERA.items():
        if ref.get("synonym"):
            continue
        add = {"natural": "tree", "genus": genus, "leaf_type": ref["leaf_type"]}
        cycles = ref["leaf_cycle"]
        shape = {"broadleaved": "broad", "needleleaved": "needle"}.get(ref["leaf_type"])
        g = shape or "neutral"
        if len(cycles) == 1:
            add["leaf_cycle"] = cycles[0]
            g = group_of(genus, ref["leaf_type"], cycles[0])
        elif genus.lower() in PALM_GENERA:
            g = "palm"
        pid = "dendrosm_ge_" + slug(genus)
        en = [n for n in ref.get("names", {}).get("en", [])]
        presets[pid] = {"name": " · ".join([genus, "genere", "genus"] + en), "icon": g + ".svg", "area": False,
                        "tags": NEVER, "addTags": add, "fields": ["@natural/tree"]}
        tr["it"][pid] = {"name": f"{genus} (genere)"}
        tr["en"][pid] = {"name": f"{genus} (genus{', ' + en[0] if en else ''})"}
        if g in genus_group and len(cycles) == 1:
            genus_group[g].append(genus)
        else:
            genus_mixed.setdefault(ref["leaf_type"], []).append(genus)
        if g in LABELS:
            # addTags senza genus: cambiando tipo il genus già mappato non va tolto
            detect.append(("dendrosm_dg_" + slug(genus), g, {"natural": "tree", "genus": genus}, {"natural": "tree"}))

    # 3b. Preset di riconoscimento: l'albero senza tutti e due i tag delle foglie prende il gruppo della sua specie
    # (o del genere). Il nome di base non è un nome cercabile; in italiano e in inglese mostrano il nome del gruppo.
    # Prima le specie, poi i generi, poi i tipi da un solo tag delle foglie (a parità di tag vale il primo).
    for pid, g, tags, add in sorted(detect, key=lambda d: d[0][9:11] != "dp" and (d[0][9:11] == "dg") + 1):
        add_type(pid, g, tags, base="DendrOSM", add=add)
    for g, (_k, _c, tag, _it, _en) in PARTIAL_TYPES.items():
        add_type("dendrosm_" + g, g, {"natural": "tree", tag[0]: tag[1]})

    # 4. Element kinds e segnaposti della modalità DendrOSM. L'ordine conta: vale il primo kind che corrisponde.
    # Anello rosso = leaf_type e leaf_cycle ci sono tutti e due ma non sono quelli della specie (o del genere).
    # Punto bianco = manca la specie E manca almeno uno dei due tag delle foglie (basta species=*, qualunque valore,
    # oppure i due tag delle foglie, per toglierlo). Senza i tag delle foglie la chioma è quella della specie.
    kinds, markers, order = {}, {}, {n: [] for n in ("sp_ok", "sp_bad", "sp", "fam", "ge_ok", "ge_bad", "ge", "leaf", "leaf2")}
    ALL_LT = ["broadleaved", "needleleaved", "palm", "leafless", "mixed"]
    ALL_LC = ["deciduous", "evergreen", "semi_deciduous", "semi_evergreen", "mixed"]
    BOTH = [("leaf_type", ALL_LT), ("leaf_cycle", ALL_LC)]
    TREE = [("natural", ["tree"])]                          # solo alberi: non gli arbusti con la stessa specie
    NO_SPECIES = lambda g: {"icon": g + ".svg", "iconPartial": g + "_part.svg", "requiredKeys": ["species"]}

    def kind(slot, name, conds, last, marker):
        kinds[name] = {"matcher": chain(conds, last)}
        markers[name] = marker
        order[slot].append(name)

    def leaf_conds(g):
        lt, lc = LEAF["broad" if g == "palm" else g.split("_")[0]], LEAF["ev" if g == "palm" else g.split("_")[1]]
        return [("leaf_type", [lt, "palm"] if g == "palm" else [lt]), ("leaf_cycle", [lc])]

    for key, s, groups in (("species", "sp", by_group), ("genus", "ge", genus_group)):
        for g, names in groups.items():
            if not names:
                continue
            kind(s + "_ok", f"dendro_{key}_{g}_ok", TREE + leaf_conds(g), (key, names), {"icon": g + ".svg"})
            kind(s + "_bad", f"dendro_{key}_{g}_bad", TREE + BOTH, (key, names), {"icon": g + "_bad.svg"})
            # senza tutti e due i tag delle foglie: la specie basta, il genere no
            kind(s, f"dendro_{key}_{g}", TREE, (key, names), {"icon": g + ".svg"} if key == "species" else NO_SPECIES(g))
    for i, (k, v) in enumerate(PALM_TAGS):                 # palme riconosciute da taxon:family e simili
        kind("fam", f"dendro_palmtag_{i}_full", TREE + BOTH, (k, [v]), {"icon": "palm.svg"})
    for i, (k, v) in enumerate(PALM_TAGS):
        kind("fam", f"dendro_palmtag_{i}", TREE, (k, [v]), NO_SPECIES("palm"))
    for lt, names in genus_mixed.items():                  # generi con cicli diversi tra le specie
        shape = {"broadleaved": "broad", "needleleaved": "needle"}.get(lt, "neutral")
        for g in GROUPS:                                   # con tutti e due i tag delle foglie: icona del gruppo
            if g != "palm" and leaf_conds(g)[0][1] == [lt]:
                kind("ge_ok", f"dendro_genus_mixed_{g}", TREE + leaf_conds(g), ("genus", names), {"icon": g + ".svg"})
        kind("ge_bad", f"dendro_genus_mixed_{lt}_ok", TREE + [("leaf_type", [lt])], ("genus", names), NO_SPECIES(shape))
        kind("ge", f"dendro_genus_mixed_{lt}_bad", TREE + [("leaf_type", ALL_LT)], ("genus", names), {"icon": shape + "_bad.svg"})
        kind("leaf", f"dendro_genus_mixed_{lt}", TREE, ("genus", names), NO_SPECIES(shape))
    for g in GROUPS:                                       # specie e generi non in lista: dai tag delle foglie
        if g == "palm":
            kind("leaf", "dendro_leaf_palm_full", TREE + [("leaf_type", ["palm"])], ("leaf_cycle", ALL_LC), {"icon": "palm.svg"})
            kind("leaf2", "dendro_leaf_palm", TREE, ("leaf_type", ["palm"]), NO_SPECIES("palm"))
        else:
            kind("leaf", f"dendro_leaf_{g}", TREE + leaf_conds(g)[:1], leaf_conds(g)[1], {"icon": g + ".svg"})
    for g, (_k, _c, tag, _it, _en) in PARTIAL_TYPES.items():
        other = ("leaf_cycle", ALL_LC) if tag[0] == "leaf_type" else ("leaf_type", ALL_LT)
        kind("leaf", f"dendro_leaf_{g}_full", TREE + [(tag[0], [tag[1]])], other, {"icon": g + ".svg"})   # due tag: niente punto
        kind("leaf2", f"dendro_leaf_{g}", TREE, (tag[0], [tag[1]]), NO_SPECIES(g))
    kinds["dendro_stump"] = {"matcher": {"natural": {"only": ["tree_stump"]}}}
    kinds["dendro_tree"] = {"matcher": {"natural": {"only": ["tree"]}}}
    markers["dendro_stump"] = {"icon": "stump.svg"}
    markers["dendro_tree"] = NO_SPECIES("neutral")
    kind_list = [k for slot in order.values() for k in slot] + ["dendro_stump", "dendro_tree"]
    assert set(kind_list) == set(kinds) == set(markers) and len(kind_list) == len(kinds)

    data = {
        "id": "dendrosm",
        "name": "DendrOSM",
        "version": 0,                     # lo mette write_plugin (everydoor/version.json)
        "author": "DendrOSM",
        "icon": "mode.svg",
        "experimental": True,             # conta solo nell'archivio pubblico dei plugin: lì resta fuori dagli elenchi
        "homepage": "https://github.com/ivanbranco/tree-osm-qa/blob/main/docs/everydoor.md",
        "description": "Tree types by leaf type and leaf cycle, drawn with the crown legend of the "
                       "DendrOSM map. Species and genera from the OSM wiki lists are shortcuts that set species, "
                       "species:wikidata, leaf_type and leaf_cycle. The DendrOSM mode marks trees with missing "
                       "tags and trees whose leaf tags do not match the species.",
        "kinds": kinds,
        "presets": presets,
        "modes": {
            # vista di controllo: senza pulsanti per aggiungere (gli alberi si aggiungono dal micromapping)
            "dendrosm": {"type": "entrances", "name": "DendrOSM", "icon": "mode.svg", "kinds": kind_list, "markers": markers},
            "micro": {"markers": micro_markers},
        },
    }
    langs = {"it": {"presets": tr["it"]}, "en": {"presets": tr["en"]}}
    print("specie:", sum(1 for p in presets if p.startswith("dendrosm_sp_")), "| generi:",
          sum(1 for p in presets if p.startswith("dendrosm_ge_")), "| voci saltate:", skipped)
    return data, langs


def write_plugin():
    data, langs = build_data()
    icons = all_icons()
    (PLUGIN / "icons").mkdir(parents=True, exist_ok=True)
    (PLUGIN / "langs").mkdir(parents=True, exist_ok=True)
    dump = lambda d: json.dumps(d, ensure_ascii=False, indent=2) + "\n"
    head = "# Generato da everydoor/build_plugin.py (JSON, che è YAML valido): non modificare a mano\n"
    files = {"plugin.yaml": head + dump(data)}             # con version 0: serve solo per l'impronta
    for lang, d in langs.items():
        files[f"langs/{lang}.yaml"] = dump(d)
    for name, prims in icons.items():
        files[f"icons/{name}.svg"] = prims_svg(prims) + "\n"
    blob = " ".join(f"{x:.1f},{y:.1f}" for x, y in shape_pts("blob", 28))   # icona della modalità: si ricolora
    files["icons/mode.svg"] = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">\n'
                               f'<polygon points="{blob}" fill="currentColor"/>\n</svg>\n')
    # Versione: Every Door aggiorna un plugin già installato solo se il numero è più alto. Sale da sola quando cambia
    # il contenuto (impronta di tutti i file); se non cambia nulla resta quella di everydoor/version.json.
    digest = hashlib.sha256("\0".join(f"{rel}\0{files[rel]}" for rel in sorted(files)).encode("utf-8")).hexdigest()
    vfile = OUT / "version.json"
    ver = json.loads(vfile.read_text(encoding="utf-8")) if vfile.exists() else {"version": FIRST_VERSION - 1, "hash": ""}
    if ver["hash"] != digest:
        ver = {"version": ver["version"] + 1, "hash": digest}
        vfile.write_text(json.dumps(ver, indent=1) + "\n", encoding="utf-8", newline="\n")
    data["version"] = ver["version"]
    files["plugin.yaml"] = head + dump(data)
    for old in list((PLUGIN / "icons").glob("*.svg")):
        old.unlink()
    for rel, text in files.items():
        (PLUGIN / rel).write_text(text, encoding="utf-8", newline="\n")
    edp = OUT / "dendrosm.edp"
    with zipfile.ZipFile(edp, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(files):                          # data fissa: lo stesso contenuto dà lo stesso file
            z.writestr(zipfile.ZipInfo(rel, date_time=(2025, 1, 1, 0, 0, 0)), files[rel].encode("utf-8"), zipfile.ZIP_DEFLATED)
    print("versione:", ver["version"])
    return data, icons, edp


# ---------------------------------------------------------------- anteprima (Pillow)
def hexrgb(h, a=255):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)) + (a,)


def draw_prims(img, prims, ox, oy, size, ss):
    """Disegna le primitive (riquadro 64) in un quadrato di lato size con angolo in (ox, oy); ss = sovracampionamento."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    k = size / 64 * ss
    X = lambda v: ox * ss + v * k
    Y = lambda v: oy * ss + v * k
    for pr in prims:
        if pr[0] == "poly":
            _, pts, fill, stroke, w = pr
            P = [(X(x), Y(y)) for x, y in pts]
            if fill:
                d.polygon(P, fill=hexrgb(fill))
            ww = max(1, round(w * k))
            d.line(P + P[:2], fill=hexrgb(stroke), width=ww, joint="curve")
        elif pr[0] == "circle":
            _, cx, cy, r, fill, stroke, w = pr
            ro, ri = (r + w / 2) * k, (r - w / 2) * k
            d.ellipse([X(cx) - ro, Y(cy) - ro, X(cx) + ro, Y(cy) + ro], fill=hexrgb(stroke))
            if fill and ri > 0:                        # gli anelli (senza riempimento) li disegna render_icon
                d.ellipse([X(cx) - ri, Y(cy) - ri, X(cx) + ri, Y(cy) + ri], fill=hexrgb(fill))
        else:
            _, x1, y1, x2, y2, stroke, w = pr
            ww = max(1, round(w * k))
            d.line([(X(x1), Y(y1)), (X(x2), Y(y2))], fill=hexrgb(stroke), width=ww)
            for x, y in ((x1, y1), (x2, y2)):
                d.ellipse([X(x) - ww / 2, Y(y) - ww / 2, X(x) + ww / 2, Y(y) + ww / 2], fill=hexrgb(stroke))


def render_icon(prims, size, bg, ss=6):
    """Icona su un quadratino col colore di sfondo bg. L'anello (cerchio senza riempimento) va disegnato per primo
    sotto il resto, perché Pillow non ha i cerchi vuoti con bordo spesso antialias: si disegna disco rosso + disco bg."""
    from PIL import Image
    pad = 6
    W = size + 2 * pad
    img = Image.new("RGBA", (W * ss, W * ss), hexrgb(bg))
    rings = [p for p in prims if p[0] == "circle" and p[4] is None]
    rest = [p for p in prims if not (p[0] == "circle" and p[4] is None)]
    for _, cx, cy, r, _f, stroke, w in rings:
        draw_prims(img, [("circle", cx, cy, r + w / 4, stroke, stroke, w / 2), ("circle", cx, cy, r - w / 2 - 0.01, bg, bg, 0.02)], pad, pad, size, ss)
    draw_prims(img, rest, pad, pad, size, ss)
    return img.resize((W, W), Image.LANCZOS)


def site_legend_prims():
    """Le voci della legenda del sito, lette da index.html (riquadro 22×22 portato a 64)."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    shapes = {}
    for key, kind in (("legBroad", "blob"), ("legNeedle", "spiky"), ("legPalm", "palm")):
        m = re.search(r'<polygon points="([^"]+)"[^>]*></svg> <span data-i18n="' + key + '"', html)
        shapes[kind] = [tuple(float(v) * 64 / 22 for v in p.split(",")) for p in m.group(1).split()]
    return shapes


def blend(h, a):   # colore con opacità a su bianco
    r, g, b, _ = hexrgb(h)
    return "#%02x%02x%02x" % tuple(round(255 + (c - 255) * a) for c in (r, g, b))


def write_preview(icons):
    from PIL import Image, ImageDraw, ImageFont
    font = lambda s, b=False: ImageFont.truetype(r"C:\Windows\Fonts\segoeuib.ttf" if b else r"C:\Windows\Fonts\segoeui.ttf", s)
    site = site_legend_prims()
    sw = 64 / 22
    # i testi sono in inglese: l'immagine va nella documentazione (docs/everydoor.md)
    rows = [(g, TYPE_NAMES[g][1], [("poly", site[kind], blend(color, .75), OUTLINE, sw)]) for g, (kind, color, _lab) in GROUPS.items()]
    for g, (kind, color, _tag, _it, en) in PARTIAL_TYPES.items():
        prims = [("circle", 32, 32, 8 * sw, blend(color, .75), OUTLINE, sw)] if kind == "circle" else \
                [("poly", site[kind], blend(color, .75), OUTLINE, sw)]
        rows.append((g, en + (" (no leaf cycle)" if kind != "circle" else " (no leaf type)"), prims))
    rows.append(("neutral", "Tree (no leaf tags)", [("circle", 32, 32, 8 * sw, blend(NOCYCLE, .75), OUTLINE, sw)]))
    rows.append(("stump", "Stump", [("line", 5 * sw, 5 * sw, 17 * sw, 17 * sw, STUMP, 3 * sw), ("line", 17 * sw, 5 * sw, 5 * sw, 17 * sw, STUMP, 3 * sw)]))
    BGS = [("map", "#f2efe9"), ("green", "#cdebb0"), ("aerial", "#4b5a3a")]
    cols = [("DendrOSM map legend\n(22 px, and enlarged)", 170), ("Plugin icon\n(enlarged)", 120),
            ("DendrOSM mode, 30 px\nmap · green · aerial imagery", 190), ("Micromapping, 24 px", 170),
            ("Missing tags\n(white dot)", 150), ("Leaf tags not matching\n(red ring)", 170)]
    x0, y0, rh = 250, 96, 96
    W, H = x0 + sum(c[1] for c in cols) + 20, y0 + rh * len(rows) + 50
    img = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((20, 14), "DendrOSM plugin for Every Door: icons", font=font(22, True), fill="#222")
    x = x0
    for title, w in cols:
        d.multiline_text((x + 6, 50), title, font=font(13, True), fill="#333", spacing=2)
        x += w
    for i, (g, lab, site_prims) in enumerate(rows):
        y = y0 + i * rh
        d.line([(15, y), (W - 15, y)], fill="#ddd", width=1)
        d.multiline_text((20, y + 30), lab, font=font(15), fill="#222", spacing=2)
        cy = y + rh // 2
        put = lambda im, cx: img.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))
        x = x0
        put(render_icon(site_prims, 22, "#ffffff"), x + 30); put(render_icon(site_prims, 66, "#ffffff"), x + 105)
        x += cols[0][1]
        put(render_icon(icons[g], 80, "#ffffff"), x + 50)
        x += cols[1][1]
        for j, (_, bg) in enumerate(BGS):
            put(render_icon(icons[g], 30, bg), x + 32 + j * 52)
        x += cols[2][1]
        for j, (_, bg) in enumerate(BGS):
            put(render_icon(icons[g], 24, bg), x + 30 + j * 46)
        x += cols[3][1]
        for v, w in (("part", cols[4][1]), ("bad", cols[5][1])):
            if g + "_" + v in icons:
                put(render_icon(icons[g + "_" + v], 30, BGS[0][1]), x + 32)
                put(render_icon(icons[g + "_" + v], 30, BGS[2][1]), x + 84)
            x += w
    d.text((20, H - 34), "The plugin icons are opaque, with a thicker outline and a white halo, to stay readable "
           "at 24-30 px and in full sun.", font=font(13), fill="#555")
    out = ROOT / "docs/img/everydoor-icons.png"
    img.convert("RGB").save(out)
    return out


if __name__ == "__main__":
    import sys
    data, icons, edp = write_plugin()
    print("plugin:", edp, edp.stat().st_size, "byte")
    if "--preview" in sys.argv:           # immagine della documentazione: serve Pillow e i caratteri di Windows
        print("anteprima:", write_preview(icons))
