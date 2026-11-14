# DendrOSM <img src="favicon.svg" alt="" width="32" height="32">

Map of OpenStreetMap trees (`natural=tree`, and stumps `natural=tree_stump`), divided by region and designed for data quality control.

**Site**: https://ivanbranco.github.io/tree-osm-qa/

**World map of monumental trees**: https://ivanbranco.github.io/tree-osm-qa/monumental-trees/

![Trees on DendrOSM: lobed and polygonal crowns, trunks, monumental trees with a yellow outline, stumps](docs/img/map-example.png)

*OpenStreetMap and OSM are trademarks of the OpenStreetMap Foundation. DendrOSM is not endorsed by or affiliated with the OpenStreetMap Foundation.*

## What it does

* **Checks the data**: 48 checks on measurements, species and genus names, Wikidata, links and monumental trees. Trees with at least one error have a red outline, and the card explains each error
* **Draws the trees** with their real measurements: crown, trunk, shape according to leaf type, colour according to leaf cycle, plus monumental trees, sacred trees, multiple trunks, ivy and stumps
* **Helps fix the data**: filters by species and by error type, a card with photo and links, opening the tree in iD, JOSM or MapComplete, a one-click fix in JOSM for errors and hints with only one possible fix, downloading the errors as GeoJSON
* **Shows what is missing**: the Completeness tab counts how many trees in view have species, leaf type, leaf cycle, height, circumference, crown diameter, a photo and, for dioecious trees, the sex, and highlights on the map those that lack each one
* **Compares regions**: error ranking, by number or by percentage, and trend over time
* **Finds the record trees**: the Record trees tab shows the tallest tree, the thickest trunk, the most slender and the stockiest tree, the widest crown and the oldest tree, for all the loaded regions, for a country, for a region or for a species, each with its top ten; the three tallest can be compared to scale, and the oldest come with what happened in the world that year
* **World map of monumental trees**: all the monumental trees in the world, without the QA part, to share; it also counts how many trees of the Italian MASAF list are already on OpenStreetMap, by region
* In Italian and English

## Regions

Italy, Poland, metropolitan France, Spain (in the autonomous communities and cities), the United Kingdom and Ireland (divided into regions), Austria (in the nine Länder), the states of Washington and Pennsylvania in the United States; Portugal, Luxembourg, the Baltic states, Slovenia, Croatia, Bosnia and Herzegovina, Serbia, Montenegro, Kosovo, Albania, North Macedonia, Greece, Bulgaria, Romania, the European microstates (Andorra, Liechtenstein, Malta, Monaco, San Marino, Vatican City), Japan and Venezuela (one region each).

A new region can be requested from the menu ("+ Request a new region…") or by clicking an uncovered country on the map. You can also ask for a new region, or for an update of an existing one, by opening a [GitHub issue](https://github.com/ivanbranco/tree-osm-qa/issues/new?template=region.yml).

## Documentation

* [QA checks](docs/check.md): all the checks, with their thresholds and where they come from
* [The map](docs/mappa.md): how trees are drawn, filters, card, completeness, ranking, trend and language
* [Data and updates](docs/dati.md): data files, the update Action, the world map and the repository structure

The site is static (GitHub Pages, MapLibre GL JS); data is downloaded from Overpass by GitHub Actions, which are started manually.

## License and attribution

Data and boundaries © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available under the ODbL. The background of the image above is also © OpenStreetMap contributors.

The reference lists (`scripts/species_list.json` and `scripts/genus_list.json`) are derived from the [List of Species](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Species) and [List of Genus, Leaf cycle, Leaf type](https://wiki.openstreetmap.org/wiki/Tag:natural%3Dtree/List_of_Genus,_Leaf_cycle,_Leaf_type) pages of the OpenStreetMap Wiki, licensed under [CC BY-SA 2.0](https://wiki.openstreetmap.org/wiki/Wiki_content_license).

The list of codes and the data of the monumental trees of Italy (`scripts/masaf_list.json`, `scripts/masaf_trees.json`), used for a check and to show on the monumental trees map the trees not yet on OpenStreetMap, are derived from the MASAF list, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): Masaf - Direzione generale dell'economia montana e delle foreste - Difor4 - "dataset AMI - Censimento alberi monumentali d'Italia vs. 11_2024".
