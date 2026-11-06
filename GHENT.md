# Ghent in voxels

The default viewer now opens a 2 × 2 km map of central Ghent, built from actual municipal building and terrain geometry plus the city's tree inventory. It reuses the original voxel renderer, streamed tiles, orbit and walking controls, place search, sunlight and weather controls. Warsaw is available at `?city=warsaw`.

## Preview

Use Node 22.12+ (or Node 24) and the repository's pnpm version:

```sh
cd web
pnpm install
pnpm dev
```

Open http://localhost:5173/. Generated Ghent assets are included in `web/public/ghent`; no API access or DWG converter is needed to run the viewer.

Navigation uses AZERTY: **Z/S** move forward/back, **Q/D** turn left/right, **A/E** tilt up/down, and **Shift** moves faster. Arrow keys also work. The on-screen hints use the same layout.

## Sources

- [Stad Gent — Gent in 3D](https://data.stad.gent/explore/dataset/gent-in-3d/): four building and four terrain tiles, named 2013, in Belgian Lambert 72 (EPSG:31370). Bounds: easting 104000–106000, northing 193000–195000. Converted STL files come from [Mateo Van Damme's Fly Over Ghent](https://github.com/MateoVanDamme/fly-over-gent), hosted at `https://storage.googleapis.com/fly-over-ghent/stl/`. This project uses the data; it does not copy the other viewer's implementation.
- [Groendienst Gent — Locaties bomen Gent](https://data.stad.gent/explore/dataset/locaties-bomen-gent/): downloaded 2026-11-06, 68,240 inventory records. The map contains 3,345 trees; 18 trees inside the historical building footprint are omitted. Coordinates, species, source height classes and provenance are retained in `web/public/ghent/trees.json`.
- [OpenStreetMap](https://www.openstreetmap.org/copyright): roads, water, vegetation areas, named places and building footprints for illustrative facade variation. The export timestamp is recorded in `meta.json`.

City data uses the [Modellicentie Gratis Hergebruik Vlaanderen](https://www.vlaanderen.be/digitaal-vlaanderen/onze-oplossingen/open-data/voorwaarden-voor-het-hergebruik-van-overheidsinformatie/modellicentie-gratis-hergebruik). Contains government information obtained under the free reuse model license Flanders v1.0. © OpenStreetMap contributors, [ODbL](https://opendatacommons.org/licenses/odbl/). Original viewer code remains under the repository's MIT license.

## Rebuild

The existing Python project includes the required geospatial dependencies. Downloads are cached under ignored `data/raw/ghent`; the normalized full tree inventory goes to ignored `data/ghent`.

```sh
python3 scripts/fetch_ghent.py
python3 scripts/ghent_trees.py
uv run python scripts/build_ghent.py
uv run python -m unittest discover -s scripts -p 'test_ghent.py' -v
```

`ghent_trees.py --refresh` updates the tree inventory and its metadata. It also supports `--bbox WEST SOUTH EAST NORTH` in WGS84 and `--output PATH`. The importer uses JSON from the same API as the supplied CSV export so field keys remain stable across display languages.

The builder rasterizes triangle elevations at 1 m cell centres, fills buildings below their roofs and exports the existing 1/2/4/8 m tile format. Terrain covers 99.6% of the region; missing edge samples use the nearest valid terrain elevation. It writes a build report and separate terrain, building-region, foliage, search and camera data. A cached rebuild needs about a minute and roughly 2 GB of RAM.

For browser verification, install Chromium with `pnpm exec playwright-core install chromium`, start the dev server and run `node tools/ghent-smoke.mjs` from `web`. The test checks loading, map asset requests, time, weather, place search and camera movement, and saves `/tmp/ghent-overview.png`.

## De Lijn transit

### Timetable animation without a key

Without a live API key, the viewer automatically animates scheduled bus and tram journeys from the public [De Lijn GTFS archive provided by iRail / FlatTurtle](https://gtfs.flatturtle.cloud/delijn/). This also works on static hosting where the live proxy is unavailable. The bundled feed is version `20261106_20261231_003000`, valid 6 November–31 December 2026. The browser downloads only a 1.35 MB compressed extract containing 37,349 trip definitions, 483 route shapes and 99 route identifiers that cross this map; it does not download the regional GTFS ZIP.

Vehicles follow published route shapes, pause for scheduled dwell times and interpolate distance at constant speed between timed stops. Weekday calendars, date exceptions, after-midnight service and Brussels daylight saving time determine which journeys appear. Transit uses the current clock in Europe/Brussels; the scene date/hour controls affect lighting independently. The panel says **scheduled estimates · not live GPS**, estimated vehicles use blue models with `≈` route badges, and vehicle details state that actual delays and cancellations are unknown. These are planned journeys, not confirmed vehicle locations or assignments. Valid live GPS data takes priority when a key is configured.

Expired feeds show an expiry message and stop running vehicles beyond their valid service period. To refresh the public data and rebuild the local extract:

```sh
uv run python scripts/build_delijn_schedule.py --download
```

Without `--download`, the script uses cached `data/raw/ghent/delijn-gtfs.zip`. It streams the regional files, projects route shapes into the same Lambert 72 grid as the city and matches timed stops to distance along each route. Cropping preserves separate visits to the map rather than connecting them across gaps. Mismatched shapes and invalid timing sequences are skipped. Frequency-based templates are explicitly rejected by this importer. Changes to the map bounds require rebuilding both the city and the timetable extract.

`pnpm test:transit` tests timetable calendars, dwell, route geometry, after-midnight service, daylight saving time, feed expiry and a sample of the actual extract. `node tools/schedule-smoke.mjs` checks the key-free fallback in Chromium using the actual bundled timetable, including movement, selection, estimate labels and pausing. It saves `ghent-scheduled-preview.png`.

### Live GPS with an optional key

The Ghent viewer includes an optional live transit layer using De Lijn GTFS Realtime v3. Vehicle positions are projected into the same Lambert 72 grid as the city. The layer refreshes every 30 seconds and has a show/hide control. Click a vehicle or its yellow badge for its reported delay, cancellation status and GPS update time. Live positions always use the current real time, independently of the scene's date and hour controls. They do not follow the historical scene clock or predict motion between GPS fixes.

The API requires a subscription key. From `web`, copy `.env.example` to `.env.local` and set:

```dotenv
DELIJN_API_KEY=your-subscription-key
```

Restart `pnpm dev`. The server requests `https://api.delijn.be/gtfs/v3/realtime?json=true&position=true&delay=true&canceled=true`, sending the key in the `Ocp-Apim-Subscription-Key` header. Keys stay on the server, outside the Vite bundle. `.env` and `.env.local` are ignored by Git. The browser receives only filtered Ghent vehicle records through `/api/delijn`, not the original region-wide feed. All viewers share one cached upstream request per 30 seconds. Upstream requests time out after 15 seconds; failure responses never forward upstream error bodies.

The UI distinguishes live, stale, scheduled, unavailable, unconfigured and empty feeds. GPS positions older than 90 seconds are labelled as last known; positions older than three minutes are removed. A stale or failed feed cannot silently appear live. Without a key, or when live access is unavailable, the viewer uses the clearly labelled timetable estimates described above. If the timetable asset is also missing, it shows a connection message. Hidden tabs and a paused layer stop API polling. Vehicle shapes are illustrative, and GPS fixes may disagree with streets or bridges in the 2013 geometry.

Realtime route identifiers are not necessarily public line numbers and do not reliably identify bus versus tram. Without matching static route metadata, vehicles use a neutral model and their reported route identifier in the details. To add public line names and bus/tram models, obtain the matching De Lijn **GTFS Static v3** ZIP and extract its `routes.txt`:

```sh
python3 scripts/delijn_routes.py /path/to/gtfs_transit.zip
```

Then add `DELIJN_ROUTES_FILE=../data/ghent/delijn-routes.json` to `web/.env.local` and restart the server. The importer supports standard and extended bus/tram route types. A cancellation or stop delay is shown only when it can be joined to a positioned vehicle's trip and service instance; unlocated canceled trips cannot be placed on this map. Stop-specific delays require a current stop sequence. The first version expects full GTFS snapshots; differential feeds are rejected rather than partially displayed.

For production, run `pnpm build` then `pnpm start` from `web` (Node 22.12+). This serves `dist` and `/api/delijn` together on `127.0.0.1:3000`; `HOST` and `PORT` are configurable. Use the hosting environment's secrets for `DELIJN_API_KEY`. Static GitHub Pages hosting cannot run this proxy: deploy the Node service separately, set the frontend's public `VITE_DELIJN_PROXY_URL` to its HTTPS `/api/delijn` URL before building, and set the Node server's `DELIJN_ALLOWED_ORIGIN` to the exact frontend origin. This URL contains no key.

`pnpm test:transit` checks parsing, projection, trip joins, freshness, caching, concurrency, credential handling and HTTP behavior using mock responses. `node tools/transit-smoke.mjs` checks vehicle markers, toggling, stale/empty feeds, connection failures and recovery in Chromium, also with explicitly mocked responses. An actual authenticated De Lijn response still needs to be verified with a valid subscription key; fixtures are not presented to users as live data.

References: [De Lijn developer portal](https://portal.delijn.be/apis), [GTFS Realtime reference](https://gtfs.org/documentation/realtime/reference/) and [freshness best practices](https://gtfs.org/documentation/realtime/realtime-best-practices/).

## Limits of this edition

- Geometry dates from 2013 and does not represent subsequent construction. The newer tree inventory can disagree with that geometry.
- Roof shapes and terrain elevations come from the real model. Vertical column filling approximates tunnels, arches and overhangs; thin details smaller than a voxel can disappear.
- STL has no colours. Facades, roofs and ground use illustrative colours and OSM surface classifications; these are not sampled from aerial photographs.
- Most trees have no recorded height. Bounded height classes use their midpoint; open classes and missing heights get explicit rendering estimates. Crowns are schematic. Diameter and trunk circumference are preserved without conversion because their units are not documented in the API metadata. The publisher says the inventory is incomplete and some circumferences are street/species averages.
- Weather is manually selected. Warsaw's measured climate, seasonal phenology, window statistics and Vistula flood scenarios are disabled for Ghent. General street traffic is not yet generated; De Lijn transit uses timetable estimates by default and reported GPS positions when available.
