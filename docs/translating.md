# Translating DendrOSM

[← DendrOSM](../README.md)

DendrOSM is written in Italian and English. Other languages are added and kept up to date by volunteers who speak
them: they are not translated automatically, and the site maintainer does not translate them. A language therefore
needs someone who takes care of it, also later, when new features bring new texts.

Texts that are not translated yet appear in **English**: the site always works, even with a translation that is not
complete.

## Languages

| Language | File | Translated | Status |
|---|---|---|---|
| French | [`i18n/fr.js`](../i18n/fr.js) | see [`i18n/fr-missing.md`](../i18n/fr-missing.md) | being proofread ([issue #103](https://github.com/ivanbranco/tree-osm-qa/issues/103)) |

A language appears among the choices at the top of the panel (IT · EN · …) only when at least **70%** of its texts are
translated. If it drops below 70% (because new texts were added and not translated), it is removed from the choices
until the translation catches up; people who had chosen it see the site in the language of their browser, or in
English. Any language can still be opened from the address, for example
[`#lang=fr`](https://ivanbranco.github.io/tree-osm-qa/#lang=fr): this is how a translator checks their work.

## How to translate

Each language has a file of its own in the `i18n` folder (`i18n/fr.js`), and next to it a list of what is missing
(`i18n/fr-missing.md`). The list is updated from time to time by the maintainer; each entry is already written in
the format of the language file, with the English text to replace and the Italian one for comparison:

```js
  // IT: "Vai alla mia posizione"
  "locate": "Go to my location",
```

becomes, in `i18n/fr.js`:

```js
  "locate": "Aller à ma position",
```

* Only the text between the quotes changes. The name before the colon stays as it is.
* Some texts have variable parts: keep `${…}`, the letters before `=>` and everything around them exactly as they
  are, and move them where they fit in the sentence. For example
  `` agoDays: n => `${n} days ago` `` becomes `` agoDays: n => `il y a ${n} jours` ``.
* Keep the HTML tags (`<a href=…>`, `<b>`) and the OSM tags (`species=*`, `leaf_type=broadleaved`): they are not
  translated.
* Every line ends with a comma.
* Names with `/` are texts inside a group: `"josmMulti/zoom"` is the text `zoom` of the group `josmMulti`. In the file
  they can be written either way, as a single line with `/` or inside a block `josmMulti: { zoom: … }`.

The section "Same as English" of the list shows texts that are in the file but identical to English: if they are
meant to stay so (a brand name, a word that is the same in both languages), there is nothing to do. The section
"No longer used" shows texts the site does not have any more: they can be deleted.

## How to send a translation

There are two ways, whichever is easier:

1. **On GitHub, without installing anything.** Open [`i18n/fr.js`](../i18n/fr.js), press the pencil ("Edit this
   file"), make the changes and press "Propose changes". GitHub makes a copy of the project for you and sends the
   change as a *pull request*: the maintainer sees what changed, line by line, and adds it to the site.
2. **In an issue.** Copy the filled-in lines of `i18n/fr-missing.md` (or the whole file) into a comment of the
   language's issue, or open a [new issue](https://github.com/ivanbranco/tree-osm-qa/issues/new). The maintainer
   copies them into the language file.

## A new language

Open an [issue](https://github.com/ivanbranco/tree-osm-qa/issues/new) saying which language you would like to
translate. The maintainer creates `i18n/<code>.js` and its list of missing texts; from then on it works as above.

## What is not translated

* The pages of the species (`species/`) and this documentation, which are only in English.
* The names of regions and countries, the events of the "Record trees" tab and the common names of species, which
  come from the data: in a new language they appear in English (or, for common names, only the scientific name).
* OSM tags and values.

## For the maintainer

`python -I scripts/i18n_missing.py` compares every `i18n/<code>.js` with the English texts in `index.html`, rewrites
`i18n/<code>-missing.md` and writes the percentage of each language in the line `const LANG_DONE` of `index.html`,
which decides whether the language is shown (`LANG_MIN`, 70). `LANGS_HIDDEN` keeps a language hidden even above the
threshold, for example while it is being proofread. Texts that are the same in Italian and English (DendrOSM, JOSM…)
are not counted. A new language needs only its file in `i18n/` and a run of the script; for dates and numbers,
`LOCALES` in `index.html` gives its locale (`fr-FR`).
