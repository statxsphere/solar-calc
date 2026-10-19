# solar-calc
A repository for solar energy methods in tandem with EO and GIS data.

Right now it works out the rooftop solar potential for every building in a neighbourhood, from open LiDAR, building footprints and weather data.

I started this as a notebook (`eda.ipynb`) that read a DSM and footprints, got slope and aspect with a Sobel filter and ran pvlib on each roof pixel. This turns that into a pipeline and a web map for the Outer Sunset in San Francisco.

**Map:** https://statxsphere.github.io/solar-calc/


### Area

Outer Sunset, San Francisco, roughly Irving St to Pacheco St and 32nd to 42nd Ave (`-122.5011, 37.7500, -122.4897, 37.7635`). About 1.0 x 1.5 km and 3,735 buildings, mostly two storey row houses with flat or low roofs.


### Data

Everything is open and gets downloaded by `download.py`.

| What | Source | Licence |
| --- | --- | --- |
| LiDAR point cloud | USGS 3DEP project `CA_SanFrancisco_B23` (flown 2023), read from the public Entwine copy on AWS (`s3://usgs-lidar-public/CA_SanFrancisco_1_B23`) | Public domain (US Government) |
| Building footprints | DataSF [Building Footprints](https://data.sfgov.org/Geographic-Locations-and-Boundaries/Building-Footprints/ynuv-fyni) | ODC PDDL 1.0 |
| Typical weather year | [PVGIS](https://re.jrc.ec.europa.eu/pvg_tools/en/) 5.2 TMY, PVGIS-NSRDB database | © European Union, reuse with attribution |
| Weather for one year (optional) | [Open-Meteo](https://open-meteo.com/) historical API | CC BY 4.0 |
| Basemap on the web map | [OpenFreeMap](https://openfreemap.org/) / OpenMapTiles, OpenStreetMap data | ODbL |
| Checks only | PVGIS PVcalc, [Project Sunroof](https://sunroof.withgoogle.com/data-explorer/) public CSV, EPA [eGRID2023](https://www.epa.gov/egrid/summary-data) | Published figures, quoted not redistributed |

The raw LiDAR (about 760 MB of LAZ) stays in `data/raw/` and is not committed. Only the small files the map needs are in `docs/data/`.


### Run it

```
python -m venv .venv
.venv\Scripts\activate      # or source .venv/bin/activate
pip install -r requirements.txt

python download.py   # footprints + lidar, around 15 min depending on the connection
python main.py       # under a minute
```

Tested on Python 3.12 with the pinned versions in `requirements.txt`. They all have wheels for 3.10 to 3.12 except `timezonefinder`, which installs from source as plain python.

To look at the map locally: `cd docs` then `python -m http.server 8621`.


### How it works

1. **DSM.** `download.py` reads the EPT nodes over the area plus 100 m around it (down to depth 9, about 0.25 m point spacing), drops noise points (class 7 and 18), reprojects to UTM 10N and keeps the highest point in each 0.5 m cell. Empty cells (0.04%) get the nearest value.
2. **Clip to footprints.** Footprints are shrunk by 0.5 m so wall pixels on the edges drop out, then burned into a raster of building ids.
3. **Slope and aspect.** 3x3 median filter on the DSM, then Sobel. Aspect is 0 = north, clockwise, same as pvlib.
4. **Shading.** For every roof pixel I find the horizon angle in 16 directions out to 100 m on the DSM. Direct sun is set to zero for the hours when the sun is lower than the horizon in its direction, and sky diffuse is scaled by a sky view factor from the same horizon angles (relative to an open roof with the same tilt). This is a horizon approximation, not ray tracing, and trees count as obstacles.
5. **Weather.** PVGIS TMY, a typical year put together from 2005-2015 NSRDB satellite data. Timezone from `timezonefinder`.
6. **Irradiance.** pvlib Perez model for every bin of 5° tilt x 15° aspect, summed over the year to kWh/m², then looked up for each pixel together with its shading.
7. **Per building.**

| Setting | Value |
| --- | --- |
| Max tilt for usable roof | 45° |
| North facing (aspect 315° to 45°) | left out if tilt > 10° |
| Min sunlight for a usable pixel | 70% of an open flat roof = 1,117 kWh/m²/yr |
| Min usable roof for a building to count | 10 m² |
| Roof area | sloped area (pixel area / cos tilt) |
| Panel packing | 65% of usable roof |
| Panel efficiency | 20% (0.2 kWp per m² of panel) |
| Performance ratio | 0.80 |
| Grid CO₂ factor | 0.1944 kg/kWh (eGRID2023 CAMX, 428.5 lb/MWh) |

Outputs go to `data/preds/sunset/`: buildings as GeoParquet and GeoJSON (roof m², usable m², kWh/m², kWp, kWh/yr, CO₂ kg/yr) and the per-pixel kWh/m² raster as a COG. `docs/data/` gets a simplified GeoJSON, two PNGs of the raster for the map and `stats.json`.


### Results

| | |
| --- | --- |
| Buildings | 3,735 |
| Worth a system (≥ 10 m² usable roof) | 3,444 (92%) |
| Usable roof | 302,727 m² |
| Capacity | 39.4 MWp |
| Generation | 48.7 GWh/yr |
| CO₂ avoided | 9,460 t/yr |
| Median roof | 1,558 kWh/m²/yr, 11.1 kWp, 13,689 kWh/yr |
| Shading loss on roof pixels | 4.7% |


### Validation

| Check | This model | Reference |
| --- | --- | --- |
| Sunlight on an open flat roof | 1,596 kWh/m² | PVGIS PVcalc (NSRDB): 1,592 kWh/m² |
| South roof at 36° | 1,863 kWh/m² | PVGIS optimal angle (36°, 10° west of south): 1,852 kWh/m² |
| Yield of a flat system | 1,277 kWh/kWp | PVGIS, 14% system loss: 1,262 kWh/kWp |
| Yield across all suitable roofs | 1,237 kWh/kWp | Project Sunroof, ZIP 94122: 1,317 kWh/kW |
| Share of buildings suitable | 92% | Project Sunroof, ZIP 94122: 78.8% |
| Median system size | 11.1 kWp | Project Sunroof, ZIP 94122: 5.25 kW |
| Roof height vs the 2010 LiDAR heights in DataSF | median -0.18 m, 0.8% of buildings off by > 2 m | 0 |

What I take from this:
- The irradiance part lines up with PVGIS within 1%. It is the same weather data, so this checks the pvlib and solar position side, not the weather.
- The weather source matters more than anything else here. The Outer Sunset is foggy and the reanalysis datasets miss it: PVGIS-ERA5 gives 1,844 kWh/m² of yearly GHI and Open-Meteo for 2023 gives 1,780, against 1,595 from NSRDB. So I use NSRDB.
- Yield per kW is 6% below Sunroof. Sunroof's ZIP 94122 covers the whole Sunset north of about Quintara, not just this area.
- My system sizes are about twice Sunroof's. Sunroof fits 250 W panels onto roof segments (median 21 panels per roof), while mine covers 65% of the usable roof (median about 54 m² of panels) with 20% panels, and Sunroof qualifies fewer roofs. So my kWp and kWh are the upper end, with no setbacks or panel layout.
- The DSM roofs sit within 20 cm of the heights DataSF ships with the footprints, so the DSM and the footprints line up.


### Limits

- One weather point for the whole area.
- Horizon in 16 directions out to 100 m only. Hills further away only come in through PVGIS's own terrain horizon.
- Overhead wires are in the LiDAR as unclassified points. The median filter removes most of them.
- Footprints are older than the LiDAR, so a few rebuilt buildings don't match.
- No temperature model or inverter clipping, just a flat performance ratio.


### Changes from the notebook

- `tzwhere` is replaced with `timezonefinder` (tzwhere doesn't install on new Python).
- Sobel output is now divided by 8 x pixel size so slopes come out in real degrees. Before this most pixels looked very steep.
- Aspect is `arctan2(-dx, dy) % 360`. The old conversion turned east into south (checked on synthetic planes).
- Clear sky + cloud cover is replaced by measured irradiance from a TMY.
- Irradiance is worked out once per tilt/aspect bin instead of once per pixel, which is why a few thousand buildings take under a minute.


### Folder Structure:

```
├── data
│   ├── raw                     # lidar, tmy, sunroof csv (not committed)
│   ├── {input_id}
│   │   ├── {input_id}.geojson  # footprints
│   │   ├── {input_id}.tif      # dsm
│   ├── preds/{input_id}        # outputs
│
├── docs                        # web map (github pages)
├── download.py
├── utils.py
├── main.py
├── eda.ipynb
├── requirements.txt
└── .gitignore
```
