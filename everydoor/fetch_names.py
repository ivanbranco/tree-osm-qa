# Nomi comuni delle specie della lista di riferimento, da Wikidata, per il plugin di Every Door (issue #52).
# Scrive everydoor/names.json: {specie: {"it": nome mostrato o null, "en": ..., "words": [altri nomi per la ricerca]}}.
# Uso (dalla cartella del repository):  python -I everydoor/fetch_names.py
# Per ogni lingua il nome mostrato è, nell'ordine: il primo «nome comune del taxon» (P1843), l'etichetta se non è il
# nome scientifico, il primo alias che non sembra un nome scientifico. I nomi sono presi così come sono: non rivisti.
import json, re, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECIES = json.loads((ROOT / "scripts/species_list.json").read_text(encoding="utf-8"))["species"]
GENERA = set(json.loads((ROOT / "scripts/genus_list.json").read_text(encoding="utf-8"))["genera"])
GENERA |= {re.sub(r"^× ", "", n).split()[0] for n in SPECIES}
LANGS = ("it", "en")
MAX_WORDS = 10      # nomi in più per la ricerca, per specie
LATIN_END = ("a", "us", "um", "is", "ii", "i", "ae", "ens", "ensis", "or", "x", "es", "on", "os")


def looks_scientific(name):
    w = name.replace("×", " ").split()
    if not w or w[0][:1].upper() + w[0][1:] not in GENERA:
        return False
    return len(w) == 1 or w[1].lower().endswith(LATIN_END) or w[1] in ("x", "var.", "subsp.")


def fetch(ids):
    url = "https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(ids), "props": "labels|aliases|claims",
        "languages": "|".join(LANGS), "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "tree-species-names/0.1 (python urllib)"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))["entities"]
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            wait = int(e.headers.get("Retry-After") or 0) or 20 * (attempt + 1)
            print("429, attendo", wait, "s", flush=True)
            time.sleep(min(wait, 120))


def names_of(sci, e):
    out, words = {}, []
    common = {l: [] for l in LANGS}
    for c in e.get("claims", {}).get("P1843", []):
        v = c.get("mainsnak", {}).get("datavalue", {}).get("value")
        if v and v.get("language") in common:
            common[v["language"]].append(v["text"].strip())
    for lang in LANGS:
        label = e.get("labels", {}).get(lang, {}).get("value", "").strip()
        aliases = [a["value"].strip() for a in e.get("aliases", {}).get(lang, [])]
        cands = common[lang] + ([label] if label else []) + aliases
        good = [c for c in cands if c and c.lower() != sci.lower() and not looks_scientific(c)]
        out[lang] = good[0] if good else None
        words += [c for c in cands if c and c.lower() != sci.lower()]
    seen, uniq = {(out[l] or "").lower() for l in LANGS} | {""}, []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            uniq.append(w)
    out["words"] = uniq[:MAX_WORDS]
    return out


if __name__ == "__main__":
    by_q = {}
    for k, v in SPECIES.items():
        by_q.setdefault(v["wikidata"], []).append(k)
    print("codici Wikidata usati da più voci della lista:", {q: n for q, n in by_q.items() if len(n) > 1})
    qs, result = sorted(by_q, key=lambda q: int(q[1:])), {}
    for i in range(0, len(qs), 50):
        ents = fetch(qs[i:i + 50])
        for q in qs[i:i + 50]:
            for sci in by_q[q]:
                result[sci] = names_of(sci, ents.get(q, {}))
        print(f"{min(i + 50, len(qs))}/{len(qs)}", flush=True)
        time.sleep(1)
    out = ROOT / "everydoor/names.json"
    out.write_text(json.dumps(dict(sorted(result.items())), ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    for lang in LANGS:
        print(lang, "con nome comune:", sum(1 for v in result.values() if v[lang]), "su", len(result))
