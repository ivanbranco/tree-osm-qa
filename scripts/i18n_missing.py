"""Frasi da tradurre per le lingue della comunità (issue #103).

Confronta i testi inglesi dell'oggetto I18N di index.html con il file di ogni lingua in i18n/ (i18n/fr.js...) e
scrive i18n/<lingua>-missing.md: le frasi che mancano, già nel formato del file della lingua (con il testo inglese
da sostituire e quello italiano come confronto), quelle uguali all'inglese e quelle che il sito non usa più.
Le frasi uguali in italiano e in inglese (DendrOSM, JOSM...) non si contano: senza traduzione resta lo stesso testo.
Scrive anche la percentuale di frasi tradotte nella riga «const LANG_DONE» di index.html: sotto LANG_MIN (70%) la
lingua non ha il link in alto sul sito.

Uso: python -I scripts/i18n_missing.py          (tutte le lingue di i18n/)
     python -I scripts/i18n_missing.py fr       (una sola)
"""
import datetime
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEP = "/"   # separatore dei nomi delle frasi annidate ("josmMulti/zoom"): alcuni nomi contengono il punto ("a2.1")
IDENT = re.compile(r"[A-Za-z0-9_$]")
# dopo questi caratteri una "/" apre un'espressione regolare, non una divisione
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


class JSError(Exception):
    pass


class Parser:
    """Lettore minimo di un oggetto letterale JavaScript: dei valori tiene il testo sorgente, non li valuta."""

    def __init__(self, src, pos):
        self.s, self.i = src, pos

    def ws(self):
        s = self.s
        while self.i < len(s):
            if s[self.i].isspace():
                self.i += 1
            elif s.startswith("//", self.i):
                self.i = s.find("\n", self.i) if "\n" in s[self.i:] else len(s)
            elif s.startswith("/*", self.i):
                self.i = s.index("*/", self.i) + 2
            else:
                break

    def line(self, i=None):
        return self.s.count("\n", 0, self.i if i is None else i) + 1

    def string(self):
        """Salta una stringa '...' o "..." che inizia in self.i."""
        q, s = self.s[self.i], self.s
        self.i += 1
        while s[self.i] != q:
            if s[self.i] == "\\":
                self.i += 1
            elif s[self.i] == "\n":
                raise JSError(f"stringa non chiusa alla riga {self.line()}")
            self.i += 1
        self.i += 1

    def template(self):
        """Salta un testo `...` con le parti ${...}, che possono contenere altri testi."""
        s = self.s
        self.i += 1
        while s[self.i] != "`":
            if s[self.i] == "\\":
                self.i += 2
                continue
            if s.startswith("${", self.i):
                self.i += 2
                self.raw("}")
                self.i += 1
                continue
            self.i += 1
        self.i += 1

    def regex(self):
        s = self.s
        self.i += 1
        cls = False
        while cls or s[self.i] != "/":
            if s[self.i] == "\\":
                self.i += 1
            elif s[self.i] == "[":
                cls = True
            elif s[self.i] == "]":
                cls = False
            elif s[self.i] == "\n":
                raise JSError(f"espressione regolare non chiusa alla riga {self.line()}")
            self.i += 1
        self.i += 1
        while self.i < len(s) and IDENT.match(s[self.i]):
            self.i += 1

    def raw(self, stops):
        """Avanza fino a uno dei caratteri `stops` fuori da parentesi, stringhe e commenti; restituisce il testo."""
        s, start, depth, prev = self.s, self.i, 0, ""
        while True:
            if self.i >= len(s):
                raise JSError(f"valore non chiuso (iniziato alla riga {self.line(start)})")
            c = s[self.i]
            if depth == 0 and c in stops:
                return s[start:self.i].strip()
            if c in "'\"":
                self.string()
            elif c == "`":
                self.template()
            elif s.startswith("//", self.i) or s.startswith("/*", self.i):
                self.ws()
                continue
            elif c == "/" and (prev in REGEX_BEFORE or s[:self.i].rstrip().endswith("return")):
                self.regex()
            else:
                if c in "([{":
                    depth += 1
                elif c in ")]}":
                    depth -= 1
                    if depth < 0:
                        raise JSError(f"parentesi chiusa in più alla riga {self.line()}")
                self.i += 1
                if not c.isspace():
                    prev = c
                continue
            prev = "a"   # dopo una stringa o un'espressione regolare viene un operatore

    def key(self):
        s = self.s
        if s[self.i] in "'\"":
            start = self.i
            self.string()
            return eval(s[start:self.i], {}, {})   # solo una stringa letterale
        start = self.i
        while self.i < len(s) and IDENT.match(s[self.i]):
            self.i += 1
        if start == self.i:
            raise JSError(f"nome di una frase non valido alla riga {self.line()}: {s[self.i:self.i + 30]!r}")
        return s[start:self.i]

    def obj(self, prefix, out):
        """Legge {...} da self.i; mette in `out` nome -> (testo sorgente, riga). I sotto-oggetti si appiattiscono."""
        if self.s[self.i] != "{":
            raise JSError(f"atteso '{{' alla riga {self.line()}")
        self.i += 1
        while True:
            self.ws()
            if self.s[self.i] == "}":
                self.i += 1
                return out
            if self.s.startswith("...", self.i):
                raise JSError(f"'...' non gestito alla riga {self.line()}")
            line = self.line()
            k = self.key()
            self.ws()
            name = prefix + k
            if self.s[self.i] == ":":
                self.i += 1
                self.ws()
                if self.s[self.i] == "{":
                    self.obj(name + SEP, out)
                else:
                    out[name] = (self.raw(",}"), line)
            elif self.s[self.i] in ",}":   # forma breve { nome }
                out[name] = (k, line)
            else:
                raise JSError(f"atteso ':' dopo {k!r} alla riga {line}")
            self.ws()
            if self.s[self.i] == ",":
                self.i += 1


def read_index():
    src = (ROOT / "index.html").read_text(encoding="utf-8")
    start = src.index("const I18N = {") + len("const I18N = ")
    p = Parser(src, start + 1)
    langs = {}
    while True:
        p.ws()
        if src[p.i] == "}":
            break
        lang = p.key()
        p.ws()
        p.i += 1   # ":"
        p.ws()
        langs[lang] = p.obj("", {})
        p.ws()
        if src[p.i] == ",":
            p.i += 1
    return langs


def read_lang(path):
    """i18n/<lingua>.js: window.DENDROSM_I18N = { "nome": valore, ... }; i nomi sono già appiattiti."""
    src = path.read_text(encoding="utf-8")
    m = re.search(r"DENDROSM_I18N\s*=\s*\{", src)
    if not m:
        raise JSError("manca «window.DENDROSM_I18N = {»")
    # un blocco annidato nel file ("cmp: { ... }") vale come i nomi con "/", come in index.html (vedi withFallback)
    return Parser(src, m.end() - 1).obj("", {})


def block(names, en, it, fr=None):
    """Righe da incollare nel file della lingua: nome, valore (inglese da sostituire), italiano come confronto."""
    out = []
    for n in names:
        out.append(f"  // IT: {it[n][0]}" if n in it else "  // (solo in inglese)")
        val = fr[n][0] if fr else en[n][0]
        out.append(f"  {quote(n)}: {val},")
    return out


def quote(n):
    return '"' + n.replace("\\", "\\\\").replace('"', '\\"') + '"'


def report(lang, idx):
    path = ROOT / "i18n" / f"{lang}.js"
    en, it = idx["en"], idx.get("it", {})
    tr = read_lang(path)
    same_it_en = {n for n in en if n in it and it[n][0] == en[n][0]}
    needed = [n for n in en if n not in same_it_en]
    missing = [n for n in needed if n not in tr]
    same = [n for n in needed if n in tr and tr[n][0] == en[n][0]]
    stale = [n for n in tr if n not in en]
    done = len(needed) - len(missing) - len(same)
    today = datetime.date.today().isoformat()
    md = [f"# Texts to translate: `{lang}`", "",
          f"Generated on {today} by `scripts/i18n_missing.py`, comparing `i18n/{lang}.js` with the English texts of "
          f"`index.html`. **{done} of {len(needed)}** texts are translated"
          f" ({100 * done // max(len(needed), 1)}%). Texts that are missing appear in English on the site.", "",
          "How to use this file:", "",
          f"1. Copy the lines of a block below into `i18n/{lang}.js`, before the last `}};` (or send them back in the "
          "issue, filled in, and they will be copied there).",
          "2. Replace the English text with the translation. Keep everything that is not text: the quotes, "
          "`${…}`, the parts before `=>`, HTML tags such as `<b>` or `<a href=…>`, and the comma at the end of the line.",
          "3. The `// IT:` line is the Italian text, only for comparison: it can stay or be deleted.", ""]
    if missing:
        md += [f"## Missing ({len(missing)})", "", "```js", *block(missing, en, it), "```", ""]
    if same:
        md += [f"## Same as English ({len(same)})", "",
               "These texts are in the file but identical to English. If they are meant to stay so, nothing to do.",
               "", "```js", *block(same, en, it, tr), "```", ""]
    if stale:
        md += [f"## No longer used ({len(stale)})", "",
               f"The site no longer has these texts (or they were renamed): they can be deleted from `i18n/{lang}.js`.",
               "", *[f"* `{n}` (line {tr[n][1]})" for n in stale], ""]
    if not (missing or same or stale):
        md += ["Everything is translated.", ""]
    out = ROOT / "i18n" / f"{lang}-missing.md"
    out.write_text("\n".join(md), encoding="utf-8", newline="\n")
    print(f"{lang}: {done}/{len(needed)} tradotte, {len(missing)} mancanti, {len(same)} uguali all'inglese, "
          f"{len(stale)} non più usate -> {out.relative_to(ROOT)}")
    return 100 * done // max(len(needed), 1)


LANG_DONE = re.compile(r"^const LANG_DONE = \{(.*)\};(?=\r?$)", re.M)


def write_done(pct):
    """Riscrive in index.html la riga const LANG_DONE = { fr: 93 }: la pagina mostra la lingua da LANG_MIN in su."""
    path = ROOT / "index.html"
    with path.open(encoding="utf-8", newline="") as f:   # così gli a capo del file restano quelli che sono
        src = f.read()
    m = LANG_DONE.search(src)
    if not m:
        raise SystemExit("index.html: manca la riga «const LANG_DONE = { … };»")
    old = dict(re.findall(r"(\w+): (\d+)", m.group(1)))
    old.update({k: str(v) for k, v in pct.items()})
    line = "const LANG_DONE = { " + ", ".join(f"{k}: {v}" for k, v in sorted(old.items())) + " };"
    if line != m.group(0):
        path.write_text(src[:m.start()] + line + src[m.end():], encoding="utf-8", newline="")
        print(f"index.html: {line}")
    lim = re.search(r"const LANG_MIN = (\d+)", src)
    if lim:
        low = [k for k, v in old.items() if int(v) < int(lim.group(1))]
        if low:
            print(f"sotto il {lim.group(1)}%, senza link sul sito: {', '.join(low)}")


def main():
    idx = read_index()
    langs = sys.argv[1:] or sorted(p.stem for p in (ROOT / "i18n").glob("*.js"))
    write_done({lang: report(lang, idx) for lang in langs})


if __name__ == "__main__":
    main()
