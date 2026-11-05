# alberi-osm
Mappa degli alberi OSM del Piemonte (`natural=tree`), con rendering chioma/tronco, filtri `denotation` e controlli di qualità.

- `scripts/fetch_trees.py` – scarica da Overpass e calcola i problemi; scrive `data/trees.geojson`
- `.github/workflows/update.yml` – aggiornamento settimanale
- `index.html` – sito (MapLibre GL JS)

Per cambiare regione modifica `ISO` in `fetch_trees.py`. Pubblica con GitHub Pages dal branch `main`, cartella root.
Primo avvio: Actions → "Aggiorna dati alberi" → Run workflow.
