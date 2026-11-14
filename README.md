# OV vanaf Den Haag Centraal

Interactieve kaart met alle rechtstreekse OV-lijnen (trein, metro, tram, bus, nachtbus,
seizoenslijnen en Flixbus) die Den Haag Centraal aandoen. Opgezet als basis voor een
live webapp.

## In drie stappen

```bash
python3 build_data.py            # 1. data ophalen en verwerken (alleen Python 3.8+ nodig)
python3 -m http.server 8000      # 2. map serveren
# 3. open http://localhost:8000
```

Stap 1 downloadt de landelijke dienstregeling van OVapi (ongeveer 250 MB) en de
Flixbus-feed naar `cache/` en schrijft de resultaten naar `data/`. Op een laptop duurt
dat een paar minuten, op een Raspberry Pi eerder tien tot twintig. De kaart werkt ook
door `index.html` gewoon te openen.

Het script print welke haltes het als "Den Haag Centraal" beschouwt (alles binnen
300 meter). Staat daar een halte tussen die er niet bij hoort, of mist er een:

```bash
python3 build_data.py --radius 250
python3 build_data.py --exclude "Schedeldoekshaven|Malieveld"
```

Tram-, metro- en buslijnen worden over de echte sporen en wegen uit OpenStreetMap gelegd
(routes opgehaald via Overpass, gekoppeld op lijnnummer en haltes). Past een variant niet,
dan blijft de lijn uit de dienstregeling staan; het log noemt welke. Uitzetten: `--no-osm`.

Lukt een download niet, haal de zip dan zelf op en geef hem mee:
`python3 build_data.py --nl gtfs-nl.zip --flix flix.zip` (of `--no-flix`).
Andere vervoerders met een eigen GTFS-feed voeg je toe met `--extra naam=bestand.zip`.

## Bestanden

| Bestand | Wat |
| --- | --- |
| `index.html` | de kaart (Leaflet, geen build-stap) |
| `config.js` | instellingen: live-adres, achtergrondkaart |
| `build_data.py` | maakt de data uit GTFS |
| `live_proxy.py` | tussenstation voor live vertrektijden |
| `data/lines.geojson` | per lijn de route en alle eigenschappen |
| `data/stops.geojson` | alle haltes |
| `data/meta.json` | bronnen, peildagen, haltecodes van het knooppunt |
| `data/ov-data.js` | dezelfde drie in één bestand, voor de kaart |
| `vendor/` | Leaflet en een eenvoudige achtergrondkaart voor als er geen kaarttegels laden |

### Eigenschappen per lijn (`lines.geojson`)

`nummer`, `naam`, `vervoerder`, `cat` (trein, metro, tram, bus, nachtbus, langeafstand, veer),
`kleur` (officiële lijnkleur als de feed die heeft), `richtingen`, `seizoen`,
`freq.{wk,za,zo}` met `perUur` (vertrekken per uur per richting, 7 tot 19 uur),
`uren` (24 getallen, vertrekken per uur) en `ritten`, `patronen` (routevarianten met
haltevolgorde), `reistijd` (per halte `[minuten vanaf Centraal, minuten naar Centraal]`)
en `live` (`route_id` en de KV78-sleutel vervoerder:lijn, om live-ritten te koppelen).

## Live vertrektijden

Het tabblad Vertrektijden leest een eenvoudig JSON-formaat van een eigen adres.
`live_proxy.py` levert dat formaat:

```bash
python3 live_proxy.py --data data/meta.json            # bus, tram, metro via OVapi
python3 live_proxy.py --data data/meta.json --ns-key … # plus treinen via de NS API
```

Zet daarna in `config.js` het adres, bijvoorbeeld
`liveEndpoint: "http://raspberrypi.local:8787/departures"`.

Het tussenstation zoekt bij de start zelf de OVapi-haltegebieden rond het knooppunt op.
Test het los met `python3 live_proxy.py --test`: dat toont de gevonden gebieden en de
eerste vertrekken. De veldnamen van de vertrekken en het NS-deel zijn nog niet tegen de
echte bron gecontroleerd.

Live voertuigen: hetzelfde tussenstation levert op `/vehicles` de posities van bus, tram
en metro uit `vehiclePositions.pb` (GTFS-realtime van OVapi). De kaart laat ze tussen twee
metingen langs de lijn schuiven. Aan en uit met het vinkje Live voertuigen.

Vervolgstappen: vertragingen per lijn uit `tripUpdates.pb`, treinposities via de NS API,
en `build_data.py` wekelijks laten draaien met cron.

## Vaste diensten en bijwerken

`./install.sh` (eenmalig) installeert `python3-zmq` en maakt twee diensten aan die vanzelf
starten: `ov-kaart-web` (poort 8000) en `ov-kaart-live` (poort 8787). De NS-sleutel staat in
`ns.key`, de instellingen in `config.js` (kopie van `config.example.js`); beide blijven lokaal.
`./update.sh` haalt de nieuwste versie op en herstart het tussenstation.

## Live datastroom

Het tussenstation luistert mee met de open stroom van NDOV Loket (ZeroMQ): KV6 voor bus, tram
en metro, en de treinposities van NS. Daardoor komen posities binnen zodra ze er zijn, in
plaats van eens per minuut. Valt de stroom weg, dan schakelt het terug op opvragen bij OVapi
en de NS API. Testen: `python3 live_proxy.py --test`.

## Ritpaneel en schaduwvoertuigen

`build_data.py` schrijft ook `data/trips.json`: per rit de haltes en tijden. Het tussenstation
gebruikt dat voor twee dingen. Tik je een voertuig aan, dan levert `/trip?id=...` de haltes met
geplande en verwachte tijden (voor treinen aangevuld met de actuele NS-ritinformatie). En ritten
die volgens de dienstregeling nu rijden maar geen positie delen, zoals Flixbus, rijden als
schaduwvoertuig mee: gestippeld, stilstaand bij haltes en ertussen optrekkend en afremmend.
De dienstkalender in `trips.json` loopt twee weken vooruit; draai `build_data.py` dus wekelijks.

## Bronnen

Dienstregeling: OVapi / OpenOV (CC0). Flixbus: publieke GTFS-feed van Flix.
Achtergrond: Natural Earth; kaarttegels van OpenStreetMap.
