# DendrOSM

Mappa degli alberi di OpenStreetMap (`natural=tree`, e i ceppi `natural=tree_stump`), divisa per regioni e pensata per il controllo di qualità dei dati. Per ora le regioni attive sono quelle italiane e la Bulgaria. Il sito è statico (GitHub Pages, MapLibre GL JS) e i dati si aggiornano con una GitHub Action.

Sito: https://ivanbranco.github.io/tree-osm-qa/

## Check QA

Gli alberi con almeno un problema hanno il contorno rosso. I check sono:

* Circonferenza maggiore di 43 m (record mondiale)
* Altezza maggiore di 115,92 m (record mondiale)
* Circonferenza e altezza maggiori del record della singola specie. I limiti sono in `scripts/species_limits.json`; il confronto con `species=*` è esatto (nessuna variante o cultivar) e scatta solo se non è già scattato il check generico
* `species=*` composto da una sola parola
* `genus=*` composto da più di una parola
* `species=*` o `genus=*` che iniziano con una lettera minuscola
* `circumference=*`, `height=*`, `diameter_crown=*` o `diameter=*` con la virgola come separatore decimale (`2,9` invece di `2.9`)
* `circumference=*`, `height=*`, `diameter_crown=*` o `diameter=*` con testo, intervalli, elenchi o valori a 0 (o negativi); i valori con unità (`150 cm`, `12 ft`) non sono segnalati
* `genus=*` diverso dalla prima parola di `species=*` (il segno di ibrido `×` iniziale è ignorato)
* Collegamenti con formato non valido: `wikidata` diverso da `Q…`, `wikimedia_commons` senza `File:` o `Category:`, `panoramax` che non è un UUID, `image` che non inizia con `http`
* `ref:masaf`, `ref:rami` o `website`/`url` verso monumentaltrees.com senza `denotation=natural_monument` (un albero monumentale senza fonte non è segnalato)
* `species:wikidata=*` incoerente con `species=*` secondo la [lista di specie della wiki OSM](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Species): QID diverso da quello atteso per la specie, oppure QID che appartiene a un'altra specie. I QID accettati in più (per esempio i sinonimi) si aggiungono, per specie, in `scripts/whitelist.json`
* `leaf_type=*` o `leaf_cycle=*` (validi) diversi da quelli della specie nella lista delle specie o, se la specie non c'è, del genere nella [lista dei generi](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Genus,_Leaf_cycle,_Leaf_type). `deciduous` e `semi_deciduous` sono considerati equivalenti, come `evergreen` e `semi_evergreen`; `palm` vale come `broadleaved` e `leafless` non viene confrontato.

I check basati sulle liste richiedono `scripts/species_list.json` e `scripts/genus_list.json`, che si generano dall'Action "Aggiorna lista specie e generi"; se i file mancano, non vengono eseguiti.
* `leaf_type=*` diverso da `broadleaved`, `needleleaved`, `leafless`, `palm`
* `leaf_cycle=*` diverso da `deciduous`, `semi_deciduous`, `evergreen`, `semi_evergreen`

Se `circumference=*` manca ma c'è `diameter=*` (in metri), la circonferenza è ricavata come π × diametro e sottoposta agli stessi check e alle stesse stime; se ci sono entrambi i tag, `diameter` è ignorato (ma i controlli sul formato dei valori numerici valgono comunque anche per `diameter`).

I check sono calcolati da `scripts/fetch_trees.py` e salvati nel campo `issues` di `data/trees.geojson`.

## Rendering

* **Chioma**: cerchio con dimensione reale da `diameter_crown=*`; se manca, raggio di ripiego (3 m; 6 m per `denotation=natural_monument` e `landmark`, 2 m per `parking`) e riempimento più trasparente. La chioma non viene stimata dalla circonferenza: usando il rapporto mediano chioma/circonferenza risultava sovrastimata. La forma dipende da `leaf_type=*` (se manca o non è valido, viene stimato dalle liste della wiki OSM: dalla specie o, se non c'è, dal genere):
  * rotonda se `leaf_type` è mancante (o ha un altro valore, come `leafless`)
  * bombata, a lobi, se `broadleaved` o `palm`
  * poligonale (esagono o ottagono, ruotato in modo diverso per ogni albero) se `needleleaved`
* **Tronco**: cerchio marrone con raggio ricavato da `circumference=*` (o da `diameter=*`). Se il tag manca, la circonferenza è stimata (tronco disegnato più chiaro) da `height=*` oppure da `diameter_crown=*`, usando i rapporti della specie in `scripts/species_limits.json` (`height_circ_ratio`, `crown_circ_ratio`) o, per le specie senza rapporto, le mediane generali di `_default` (altezza/circonferenza 12, chioma/circonferenza 7,03). Le stime servono solo al rendering e non entrano nei check QA.
* **Ceppi** (`natural=tree_stump`): una X marrone, grande quanto la circonferenza (`circumference=*` o `diameter=*`); se manca, raggio di 0,25 m e X più chiara. Compaiono da zoom 10, con la X rossa se il ceppo ha errori; seguono gli stessi check degli alberi.
* **Contorno della chioma**: giallo per `denotation=natural_monument` (gli alberi monumentali sono disegnati sopra agli altri), viola per `denotation=landmark`, rosso per gli alberi con problemi (il rosso ha la precedenza).
* **Zoom**: gli alberi con problemi si vedono a ogni zoom; da zoom 9 si aggiungono i monumentali; da zoom 10 tutti gli alberi (come cerchi); da zoom 16 compaiono le forme e il tronco.

## Filtri e scheda

* Filtri per genus/specie e per tipo di errore: entrambi gli elenchi sono costruiti dagli alberi in vista, con il conteggio. Nel filtro genus/specie, "Sconosciuto" mostra gli alberi senza `genus` né `species`, "Con species" quelli con `species=*` (qualsiasi valore). Per vedere solo i monumentali basta restare tra zoom 9 e 10.
* Riquadro "Classifica errori" (chiudibile): le nazioni in ordine di alberi con almeno un errore e, dentro ogni nazione, le regioni; toccando una regione la si apre. I conteggi sono nel campo `errors` di `data/index.json` e si riempiono all'aggiornamento dei dati.
* Cliccando un albero si apre la scheda con i dati principali, i problemi rilevati e i link a OSM, Wikidata, Commons e Panoramax.
* L'immagine viene da `wikimedia_commons`, poi da `panoramax`, poi da `image`.
* Se presenti, la scheda indica `ref:masaf` (elenco degli alberi monumentali d'Italia del MASAF), `ref:rami` (registro RAMI) e `website`/`url` verso monumentaltrees.com.

## Lingua

Il sito è in italiano se la lingua del browser è l'italiano, altrimenti in inglese. Si cambia con IT · EN in alto nel pannello: la scelta resta salvata nel browser. Si può anche forzare dall'indirizzo, per esempio `#lang=en` oppure `#region=lombardia&lang=en`.

Tutti i testi sono nell'oggetto `I18N` all'inizio dello script in `index.html`: per aggiungere una lingua si copia uno dei due blocchi, lo si traduce e si aggiunge il codice a `LANGS`. Nomi delle regioni, tag e valori OSM non vengono tradotti.

## Regioni e dati

I dati arrivano da Overpass (`natural=tree` e `natural=tree_stump`) e sono divisi per regione. All'apertura non è caricata nessuna regione: si sceglie toccando i confini sulla mappa (le regioni senza dati sono grigie) o dal menu, dove le regioni italiane sono raggruppate sotto "Italia". "← Tutte le regioni" riporta alla vista d'insieme. Indirizzo diretto: `#region=lombardia`.

* `data/regions/<regione>.geojson.gz` – gli alberi di una regione, con i check già calcolati (compressi: il browser li decomprime)
* `data/index.json` – elenco delle regioni disponibili, numero di alberi, numero di alberi con errori (`errors`) e data dell'ultimo cambiamento dei dati
* `scripts/regions.json` – regioni: nome, `group` (es. "Italia": le regioni con lo stesso gruppo compaiono insieme nel menu), codice ISO 3166-2, id della relation OSM (`rel`), bbox approssimativa e, se serve, `split` (livelli iniziali di suddivisione in tile: 0 per i paesi con pochi alberi)
* `data/regions_boundaries.geojson` – confini semplificati delle regioni, per sceglierle sulla mappa; si genera dall'Action "Aggiorna confini regioni" (da Nominatim)

**Aggiornamento**: da GitHub, Actions → "Aggiorna dati alberi" → Run workflow, scegliendo `all` oppure una singola regione. Non ci sono esecuzioni automatiche: l'aggiornamento parte solo a mano. Se il download di una regione fallisce restano i suoi dati precedenti; se i dati non cambiano, i file non vengono modificati.

In locale: `python scripts/fetch_trees.py lombardia` (o `all`).

Le regioni grandi vengono scaricate a pezzi (tile) e unite. Il confine esatto lo decide l'area amministrativa OSM; la bbox serve solo a dividere il lavoro, quindi se mancano alberi ai margini di una regione basta allargarla in `regions.json`.

## Struttura

* `scripts/fetch_trees.py` – scarica da Overpass, calcola i check, scrive i dati
* `scripts/regions.json` – elenco delle regioni
* `scripts/update_boundaries.py` – scarica i confini delle regioni in `data/regions_boundaries.geojson`
* `scripts/update_species_list.py` – scarica le liste di specie e generi dalla wiki OSM in `scripts/species_list.json` e `scripts/genus_list.json`
* `scripts/species_limits.json` – per specie: limiti di altezza e circonferenza, rapporti usati per stimare le dimensioni mancanti
* `scripts/whitelist.json` – eccezioni ai check (per ora: `species:wikidata` accettati per specie)
* `.github/workflows/update.yml` – aggiornamento dei dati
* `.github/workflows/update-boundaries.yml` – aggiornamento dei confini (solo a mano)
* `.github/workflows/update-species-list.yml` – aggiornamento delle liste di specie e generi (solo a mano)
* `index.html` – il sito
* `taginfo.json` – i tag OSM usati dal progetto, nel [formato di Taginfo](https://wiki.openstreetmap.org/wiki/Taginfo/Projects); se si aggiunge o si toglie un tag in `fetch_trees.py`, va aggiornato anche qui

Per pubblicare, attiva GitHub Pages dal branch `main`, cartella root.

## Licenza e attribuzione

Dati e confini © contributori di [OpenStreetMap](https://www.openstreetmap.org/copyright), disponibili con licenza ODbL.

Le liste di riferimento (`scripts/species_list.json` e `scripts/genus_list.json`) derivano dalle pagine [List of Species](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Species) e [List of Genus, Leaf cycle, Leaf type](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Genus,_Leaf_cycle,_Leaf_type) dell'OpenStreetMap Wiki, sotto licenza [CC BY-SA 2.0](https://wiki.openstreetmap.org/wiki/Wiki_content_license).
