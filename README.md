# tree-osm-qa

Mappa degli alberi di OpenStreetMap (`natural=tree`) per il Piemonte, pensata per il controllo di qualità dei dati. Il sito è statico (GitHub Pages, MapLibre GL JS) e i dati si aggiornano con una GitHub Action.

Sito: https://ivanbranco.github.io/tree-osm-qa/

## Check QA

Gli alberi con almeno un problema hanno il contorno rosso. I check sono:

* Circonferenza maggiore di 43 m (record mondiale)
* Altezza maggiore di 115,92 m (record mondiale)
* Circonferenza e altezza maggiori del record della singola specie. I limiti sono in `scripts/species_limits.json`; il confronto con `species=*` è esatto (nessuna variante o cultivar) e scatta solo se non è già scattato il check generico
* `species=*` composto da una sola parola
* `genus=*` composto da più di una parola
* `species=*` o `genus=*` che iniziano con una lettera minuscola
* `circumference=*`, `height=*` o `diameter_crown=*` con la virgola come separatore decimale (`2,9` invece di `2.9`)
* `leaf_type=*` diverso da `broadleaved`, `needleleaved`, `leafless`, `palm`
* `leaf_cycle=*` diverso da `deciduous`, `semi_deciduous`, `evergreen`, `semi_evergreen`

I check sono calcolati da `scripts/fetch_trees.py` e salvati nel campo `issues` di `data/trees.geojson`.

## Rendering

* **Chioma**: cerchio con dimensione reale da `diameter_crown=*`; se manca, raggio di 3 m e riempimento più trasparente. La forma dipende da `leaf_type=*`:
  * rotonda se `leaf_type` è mancante (o ha un altro valore, come `leafless`)
  * bombata, a lobi, se `broadleaved` o `palm`
  * poligonale (esagono o ottagono, ruotato in modo diverso per ogni albero) se `needleleaved`
* **Tronco**: cerchio marrone con raggio ricavato da `circumference=*`. Se il tag manca ma c'è `height=*` e la specie ha un rapporto altezza/circonferenza (`height_circ_ratio`) in `scripts/species_limits.json`, la circonferenza è stimata e il tronco è disegnato più chiaro. Lo stesso vale per il rapporto diametro chioma/circonferenza (`crown_circ_ratio`), in entrambe le direzioni. Le stime servono solo al rendering e non entrano nei check QA.
* **Contorno della chioma**: giallo per `denotation=natural_monument`, viola per `denotation=landmark`, rosso per gli alberi con problemi (il rosso ha la precedenza).
* **Zoom**: da zoom 16 compaiono le forme e il tronco; tra zoom 9 e 16 gli alberi sono cerchi; sotto zoom 9 si vedono solo gli alberi con problemi.

## Filtri e scheda

* Filtri per `denotation`, per genus/specie (l'elenco è costruito dagli alberi in vista) e per tipo di errore.
* Cliccando un albero si apre la scheda con i dati principali, i problemi rilevati e i link a OSM, Wikidata, Commons e Panoramax.
* L'immagine viene da `wikimedia_commons`, poi da `panoramax`, poi da `image`.
* Se presenti, la scheda indica `ref:masaf` (elenco degli alberi monumentali d'Italia del MASAF), `ref:rami` (registro RAMI) e `website`/`url` verso monumentaltrees.com.

## Dati e aggiornamento

I dati arrivano da Overpass (`natural=tree` nell'area del Piemonte) e vengono salvati in `data/trees.geojson`. L'aggiornamento è periodico e si può avviare a mano da Actions → "Aggiorna dati alberi" → Run workflow. Se il download fallisce restano i dati precedenti.

## Struttura

* `scripts/fetch_trees.py` – scarica da Overpass, calcola i check, scrive `data/trees.geojson`
* `scripts/species_limits.json` – per specie: limiti di altezza e circonferenza, rapporti usati per stimare le dimensioni mancanti
* `.github/workflows/update.yml` – aggiornamento dei dati
* `index.html` – il sito

Per cambiare area modifica `BBOX` e `AREA_ID` in `scripts/fetch_trees.py`. Per pubblicare, attiva GitHub Pages dal branch `main`, cartella root.

## Licenza e attribuzione

Dati © contributori di [OpenStreetMap](https://www.openstreetmap.org/copyright), disponibili con licenza ODbL.
