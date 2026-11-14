#!/usr/bin/env python3
"""
live_proxy.py - klein tussenstation voor live vertrektijden bij Den Haag Centraal.

De kaart (index.html) vraagt elke 30 seconden  GET /departures  op en verwacht:

  {"updated": "2026-11-13T08:00:12",
   "departures": [{"line": "6", "mode": "tram", "destination": "Leidschendam Noord",
                   "planned": "2026-11-13T08:03:00", "expected": "2026-11-13T08:04:10",
                   "platform": "", "status": "DRIVING", "kv78": "HTM:6"}]}

Bronnen:
  - bus, tram, metro: OVapi  http://v0.ovapi.nl/stopareacode/<codes>   (geen sleutel nodig)
    De codes zoekt het script bij de start zelf op: alle OVapi-haltegebieden binnen de
    straal van het knooppunt (bij Den Haag Centraal o.a. GvCS, 2601, 2605, 2609).
  - trein (optioneel): NS Reisinformatie API, gratis sleutel via apiportal.ns.nl

LET OP: de lijst met haltegebieden is op de Pi gecontroleerd; de veldnamen van de
vertrekken zelf en het NS-deel zijn nog niet tegen de echte bron getest. Controleer bij
de eerste start de uitvoer en pas zo nodig ovapi() of ns() aan.

Daarnaast  GET /vehicles : actuele voertuigposities (bus, tram, metro) uit het
GTFS-realtime-bestand vehiclePositions.pb van OVapi, alleen voor lijnen op de kaart:

  {"updated": "...", "vehicles": [{"id": "...", "route_id": "104312", "lat": 52.08,
                                   "lon": 4.32, "bearing": 90.0, "ts": 1794567890, "label": "3115"}]}

Gebruik:  python3 live_proxy.py [--ns-key SLEUTEL] [--port 8787] [--test]
"""
import argparse, csv, gzip, io, json, math, os, struct, threading, time, urllib.request, zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "ov-kaart-denhaag/1.0 (persoonlijk project)"
MODES = {"TRAM": "tram", "BUS": "bus", "METRO": "metro", "BOAT": "veer", "TRAIN": "trein"}


def get(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


OVAPI = "http://v0.ovapi.nl"


def find_areas(lat, lon, radius):
    """Alle OVapi-haltegebieden binnen radius meter van het knooppunt."""
    out = []
    for code, v in get(OVAPI + "/stopareacode/").items():
        try:
            dy = (float(v["Latitude"]) - lat) * 111320.0
            dx = (float(v["Longitude"]) - lon) * 111320.0 * math.cos(math.radians(lat))
        except (KeyError, TypeError, ValueError):
            continue
        if math.hypot(dx, dy) <= radius:
            out.append(code)
            print("  haltegebied %-8s %s, %s" % (code, v.get("TimingPointName", ""), v.get("TimingPointTown", "")))
    return sorted(out)


def passes(node, found):
    """Zoekt in het antwoord alle 'Passes'-blokken, hoe diep ze ook genest zijn."""
    if isinstance(node, dict):
        p = node.get("Passes")
        if isinstance(p, dict):
            found.update(p)
        for k, v in node.items():
            if k != "Passes":
                passes(v, found)
    return found


def ovapi(codes):
    out, now = [], time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 60))
    if not codes:
        return out
    found = passes(get(OVAPI + "/stopareacode/" + ",".join(codes)), {})
    for p in found.values():
        if not isinstance(p, dict) or p.get("TripStopStatus") in ("PASSED", "ARRIVED"):
            continue
        if p.get("JourneyStopType") == "LAST":      # eindpunt: aankomst, geen vertrek
            continue
        exp = p.get("ExpectedDepartureTime") or p.get("TargetDepartureTime") or ""
        if not exp or exp < now:
            continue
        out.append({
            "line": p.get("LinePublicNumber", ""),
            "mode": MODES.get(p.get("TransportType", ""), "bus"),
            "destination": p.get("DestinationName50", ""),
            "planned": p.get("TargetDepartureTime", ""),
            "expected": exp,
            "platform": p.get("SideCode") or "",
            "status": p.get("TripStopStatus", ""),
            "kv78": "%s:%s" % (p.get("DataOwnerCode", ""), p.get("LinePlanningNumber", "")),
        })
    return out


# ------------------------------------------------------------ voertuigposities (GTFS-realtime)
VEH_URLS = ["https://gtfs.ovapi.nl/nl/vehiclePositions.pb", "http://gtfs.ovapi.nl/nl/vehiclePositions.pb",
            "https://gtfs.openov.nl/gtfs-rt/vehiclePositions.pb"]


def pb(buf):
    """Minimale protobuf-lezer: geeft (veldnummer, type, waarde) voor elk veld in buf."""
    i, n = 0, len(buf)
    while i < n:
        key = shift = 0
        while True:
            b = buf[i]; i += 1
            key |= (b & 0x7F) << shift; shift += 7
            if b < 0x80:
                break
        field, wt = key >> 3, key & 7
        if wt == 0:
            v = shift = 0
            while True:
                b = buf[i]; i += 1
                v |= (b & 0x7F) << shift; shift += 7
                if b < 0x80:
                    break
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        elif wt == 2:
            ln = shift = 0
            while True:
                b = buf[i]; i += 1
                ln |= (b & 0x7F) << shift; shift += 7
                if b < 0x80:
                    break
            v = buf[i:i + ln]; i += ln
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        else:
            return
        yield field, wt, v


def parse_vehicles(buf):
    """FeedMessage -> lijst van dicts (id, trip_id, route_id, lat, lon, bearing, ts, label)."""
    out = []
    for f, wt, ent in pb(buf):
        if f != 2 or wt != 2:
            continue
        v = {"id": "", "trip_id": "", "route_id": "", "label": ""}
        for f2, wt2, x in pb(ent):
            if f2 == 1 and wt2 == 2:
                v["id"] = x.decode("utf-8", "replace")
            elif f2 == 4 and wt2 == 2:                       # VehiclePosition
                for f3, wt3, y in pb(x):
                    if f3 == 1 and wt3 == 2:                 # TripDescriptor
                        for f4, wt4, z in pb(y):
                            if f4 == 1 and wt4 == 2:
                                v["trip_id"] = z.decode("utf-8", "replace")
                            elif f4 == 5 and wt4 == 2:
                                v["route_id"] = z.decode("utf-8", "replace")
                    elif f3 == 2 and wt3 == 2:               # Position
                        for f4, wt4, z in pb(y):
                            if wt4 == 5 and f4 in (1, 2, 3):
                                v[{1: "lat", 2: "lon", 3: "bearing"}[f4]] = round(struct.unpack("<f", z)[0], 6)
                    elif f3 == 5 and wt3 == 0:
                        v["ts"] = y
                    elif f3 == 8 and wt3 == 2:               # VehicleDescriptor
                        for f4, wt4, z in pb(y):
                            if wt4 == 2 and (f4 == 2 or (f4 == 1 and not v["label"])):
                                v["label"] = z.decode("utf-8", "replace")
        if "lat" in v and "lon" in v:
            out.append(v)
    return out


def load_trip_map(gtfs_path, route_ids):
    """trip_id en realtime_trip_id -> route_id, en treinnummer -> route_id, voor de lijnen op de kaart."""
    m, short = {}, {}
    if not os.path.exists(gtfs_path):
        print("  %s niet gevonden: voertuigen worden alleen op route_id gekoppeld" % gtfs_path)
        return m, short
    with zipfile.ZipFile(gtfs_path) as z:
        rd = csv.reader(io.TextIOWrapper(z.open("trips.txt"), encoding="utf-8-sig", newline=""))
        h = next(rd)
        ir, it = h.index("route_id"), h.index("trip_id")
        irt = h.index("realtime_trip_id") if "realtime_trip_id" in h else None
        isn = h.index("trip_short_name") if "trip_short_name" in h else None
        for r in rd:
            if r[ir] in route_ids:
                m[r[it]] = r[ir]
                if irt is not None and r[irt]:
                    m[r[irt]] = r[ir]
                if isn is not None and r[isn]:
                    short[r[isn].strip()] = r[ir]
    return m, short


# ------------------------------------------------------------ treinposities (NS Virtual Train API)
TRAIN_URLS = ["https://gateway.apiportal.ns.nl/virtual-train-api/vehicle",
              "https://gateway.apiportal.ns.nl/virtual-train-api/api/vehicle",
              "https://gateway.apiportal.ns.nl/virtual-train-api/v1/vehicle"]


def find_positions(node, out):
    """Zoekt in het antwoord alle objecten met een positie (lat + lng/lon), hoe diep ook."""
    if isinstance(node, dict):
        if "lat" in node and ("lng" in node or "lon" in node):
            out.append(node)
        else:
            for v in node.values():
                find_positions(v, out)
    elif isinstance(node, list):
        for v in node:
            find_positions(v, out)
    return out


def trains(key, short_map, state):
    data, errs = None, []
    for url in ([state["train_url"]] if state.get("train_url") else TRAIN_URLS):
        try:
            data = get(url, {"Ocp-Apim-Subscription-Key": key})
            state["train_url"] = url
            break
        except Exception as e:  # noqa
            errs.append("%s: %s" % (url.split("ns.nl")[1], e))
            if "429" in str(e) or "401" in str(e) or "403" in str(e):
                break                      # geweigerd: andere adressen proberen heeft geen zin
    if data is None:
        raise RuntimeError("treinposities niet bereikbaar (%s)" % "; ".join(errs))
    found = find_positions(data, [])
    if not found:
        raise RuntimeError("treinposities: antwoord niet herkend: " + json.dumps(data)[:300])
    state["train_sample"] = found[0]
    state["train_total"] = len(found)
    now, out = int(time.time()), []
    for t in found:
        nr = str(t.get("treinNummer") or t.get("ritId") or t.get("trainNumber") or "").strip()
        rid = short_map.get(nr) or short_map.get(str(t.get("ritId") or "").strip())
        if not rid:
            continue
        try:
            out.append({"id": "ns:" + nr, "route_id": rid, "lat": float(t["lat"]), "lon": float(t.get("lng", t.get("lon"))),
                        "bearing": t.get("richting"), "ts": now, "label": "trein " + nr, "kmh": t.get("snelheid")})
        except (TypeError, ValueError):
            pass
    return out


def vehicles(route_ids, trip_map, state):
    buf, last = None, None
    for url in ([state["url"]] if state.get("url") else VEH_URLS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                buf = r.read()
            state["url"] = url
            break
        except Exception as e:  # noqa
            last = e
    if buf is None:
        raise RuntimeError("vehiclePositions niet bereikbaar (%s)" % last)
    allv = parse_vehicles(buf)
    out = []
    for v in allv:
        rid = v["route_id"] if v["route_id"] in route_ids else None
        if rid is None:
            for k in (v["trip_id"], v["id"], v["id"].split(":", 1)[-1], v["trip_id"].split(":", 1)[-1]):
                if k in trip_map:
                    rid = trip_map[k]
                    break
        if rid is None:
            continue
        out.append({"id": v["id"] or v["trip_id"], "route_id": rid, "lat": v["lat"], "lon": v["lon"],
                    "bearing": v.get("bearing"), "ts": v.get("ts"), "label": v["label"]})
    state["seen"] = len(allv)
    if allv and not state.get("sample"):
        state["sample"] = {k: allv[0][k] for k in ("id", "trip_id", "route_id")}
    return out


# ------------------------------------------------------------ live datastroom (NDOV Loket, ZeroMQ)
# In plaats van elke minuut een bestand op te vragen luisteren we mee met de ruwe stroom:
#   KV6 (bus, tram, metro)  tcp://pubsub.besteffort.ndovloket.nl:7658   /<vervoerder>/KV6posinfo
#   NS treinposities        tcp://pubsub.besteffort.ndovloket.nl:7664   /RIG/NStreinpositiesInterface5
KV6_ADDR = "tcp://pubsub.besteffort.ndovloket.nl:7658"
NS_ADDR = "tcp://pubsub.besteffort.ndovloket.nl:7664"
NS_ENVELOPE = "/RIG/NStreinpositiesInterface5"
OWNER_ENVELOPE = {"HTM": "RIG", "RET": "RIG", "EBS": "EBS", "ARR": "ARR", "CXX": "CXX", "QBUZZ": "QBUZZ",
                  "KEOLIS": "KEOLIS", "SYNTUS": "KEOLIS", "GVB": "GVB", "TEC": "OPENOV", "DELIJN": "OPENOV"}
KV6_EVENTS = {"INIT", "DELAY", "ARRIVAL", "ONSTOP", "DEPARTURE", "ONROUTE", "OFFROUTE", "END"}


def rd_to_wgs(x, y):
    """Rijksdriehoek -> breedte/lengte (benadering, nauwkeurig tot ongeveer een meter)."""
    dx, dy = (x - 155000.0) * 1e-5, (y - 463000.0) * 1e-5
    lat = (3235.65389 * dy - 32.58297 * dx ** 2 - 0.2475 * dy ** 2 - 0.84978 * dx ** 2 * dy - 0.0655 * dy ** 3
           - 0.01709 * dx ** 2 * dy ** 2 - 0.00738 * dx + 0.0053 * dx ** 4 - 0.00039 * dx ** 2 * dy ** 3
           + 0.00033 * dx ** 4 * dy - 0.00012 * dx * dy)
    lon = (5260.52916 * dx + 105.94684 * dx * dy + 2.45656 * dx * dy ** 2 - 0.81885 * dx ** 3 + 0.05594 * dx * dy ** 3
           - 0.05607 * dx ** 3 * dy + 0.01199 * dy - 0.00256 * dx ** 3 * dy ** 2 + 0.00128 * dx * dy ** 4
           + 0.00022 * dy ** 2 - 0.00022 * dx ** 2 + 0.00026 * dx ** 5)
    return round(52.15517440 + lat / 3600.0, 6), round(5.38720621 + lon / 3600.0, 6)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def iso_ts(text):
    try:
        return int(datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa
        return int(time.time())


def unpack(payload):
    try:
        return gzip.decompress(payload)
    except Exception:  # noqa
        return payload


def parse_kv6(xml_bytes):
    """KV6posinfo-bericht -> lijst van (soort, velden)."""
    out = []
    for el in ET.fromstring(xml_bytes).iter():
        kind = local(el.tag).upper()
        if kind in KV6_EVENTS:
            out.append((kind, {local(c.tag).lower(): (c.text or "").strip() for c in el}))
    return out


def parse_ns_positions(xml_bytes):
    """NS treinposities -> lijst van dicts (nr, lat, lon, kmh, bearing, ts, materieel)."""
    out = []
    for el in ET.fromstring(xml_bytes).iter():
        if local(el.tag).lower() != "treinlocation":
            continue
        nr, best, mat = "", None, []
        for c in el:
            name = local(c.tag).lower()
            if name == "treinnummer":
                nr = (c.text or "").strip()
            elif name == "treinmaterieeldelen":
                f = {local(x.tag).lower(): (x.text or "").strip() for x in c}
                if f.get("materieeldeelnummer"):
                    mat.append(f["materieeldeelnummer"])
                if best is None and f.get("latitude") and f.get("longitude"):
                    best = f
        if not nr or best is None:
            continue
        try:
            out.append({"nr": nr, "lat": round(float(best["latitude"]), 6), "lon": round(float(best["longitude"]), 6),
                        "kmh": float(best.get("snelheid") or 0), "bearing": float(best.get("richting") or 0),
                        "ts": iso_ts(best["gpsdatumtijd"]) if best.get("gpsdatumtijd") else int(time.time()), "mat": mat})
        except ValueError:
            pass
    return out


class Stream:
    """Luistert in achtergronddraden mee en houdt per voertuig de laatste stand bij."""

    def __init__(self, trip_map, short_map, kv6_addr=KV6_ADDR, ns_addr=NS_ADDR):
        self.trip_map, self.short_map = trip_map, short_map
        self.kv6_addr, self.ns_addr = kv6_addr, ns_addr
        self.lock = threading.Lock()
        self.veh, self.trains = {}, {}
        self.last = {"kv6": 0.0, "ns": 0.0}
        self.count = {"kv6": 0, "ns": 0, "kv6_ours": 0, "ns_ours": 0}
        self.sample = {}
        self.error = {}

    def envelopes(self):
        owners = {k.split(":", 1)[0] for k in self.trip_map if k.count(":") == 2}
        env = {"/%s/KV6posinfo" % OWNER_ENVELOPE[o] for o in owners if o in OWNER_ENVELOPE}
        return sorted(env or {"/RIG/KV6posinfo"})

    def start(self, kv6=True, ns=True):
        if kv6:
            threading.Thread(target=self._run, args=("kv6", self.kv6_addr, self.envelopes(), self._on_kv6), daemon=True).start()
        if ns:
            threading.Thread(target=self._run, args=("ns", self.ns_addr, [NS_ENVELOPE], self._on_ns), daemon=True).start()

    def _run(self, name, addr, envs, handler):
        import zmq
        ctx = zmq.Context.instance()
        while True:
            sock = ctx.socket(zmq.SUB)
            try:
                sock.setsockopt(zmq.RCVTIMEO, 45000)
                sock.setsockopt(zmq.LINGER, 0)
                sock.connect(addr)
                for e in envs:
                    sock.setsockopt_string(zmq.SUBSCRIBE, e)
                print(time.strftime("%H:%M:%S"), "stroom %s: verbonden met %s %s" % (name, addr, envs), flush=True)
                while True:
                    parts = sock.recv_multipart()
                    data = unpack(parts[-1])
                    self.last[name] = time.time()
                    self.count[name] += 1
                    if name not in self.sample:
                        self.sample[name] = data[:900].decode("utf-8", "replace")
                    try:
                        handler(data)
                    except Exception as e:  # noqa
                        self.error[name] = "%s: %s" % (type(e).__name__, e)
            except Exception as e:  # noqa
                self.error[name] = "%s: %s" % (type(e).__name__, e)
                print(time.strftime("%H:%M:%S"), "stroom %s: %s - opnieuw verbinden" % (name, self.error[name]), flush=True)
            finally:
                sock.close()
            time.sleep(5)

    def _on_kv6(self, data):
        for kind, f in parse_kv6(data):
            key = "%s:%s:%s" % (f.get("dataownercode", ""), f.get("lineplanningnumber", ""), f.get("journeynumber", ""))
            rid = self.trip_map.get(key)
            if rid is None:
                continue
            vid = "%s:%s" % (f.get("operatingday", ""), key)
            with self.lock:
                if kind == "END":
                    self.veh.pop(vid, None)
                    continue
                v = self.veh.setdefault(vid, {"id": vid, "route_id": rid})
                self.count["kv6_ours"] += 1
                v["seen"] = time.time()
                v["status"] = kind
                if f.get("vehiclenumber"):
                    v["label"] = f["vehiclenumber"]
                if f.get("punctuality", "") not in ("", None):
                    try:
                        v["delay"] = int(f["punctuality"])
                    except ValueError:
                        pass
                if f.get("userstopcode"):
                    v["stop"] = f["userstopcode"]
                try:
                    x, y = float(f.get("rd-x", "-1")), float(f.get("rd-y", "-1"))
                except ValueError:
                    x = y = -1
                if x > 0 and y > 0:
                    v["lat"], v["lon"] = rd_to_wgs(x, y)
                    v["ts"] = iso_ts(f["timestamp"]) if f.get("timestamp") else int(time.time())

    def _on_ns(self, data):
        for t in parse_ns_positions(data):
            rid = self.short_map.get(t["nr"])
            if rid is None:
                continue
            with self.lock:
                self.count["ns_ours"] += 1
                self.trains["ns:" + t["nr"]] = {"id": "ns:" + t["nr"], "route_id": rid, "lat": t["lat"], "lon": t["lon"],
                                                 "bearing": t["bearing"], "kmh": t["kmh"], "ts": t["ts"], "label": "trein " + t["nr"],
                                                 "mat": t["mat"], "seen": time.time()}

    def alive(self, name):
        return time.time() - self.last[name] < 90

    def snapshot(self, name):
        now = time.time()
        src = self.veh if name == "kv6" else self.trains
        with self.lock:
            for k in [k for k, v in src.items() if now - v["seen"] > (600 if name == "kv6" else 180)]:
                del src[k]
            return [{k: x for k, x in v.items() if k != "seen"} for v in src.values() if "lat" in v]


# ------------------------------------------------------------ ritplannen (uit data/trips.json)
class Plan:
    """Dienstregeling per rit: voor het infopaneel en voor voertuigen zonder gps (schaduwvoertuigen)."""

    def __init__(self, trips_path, stops_path):
        j = json.load(open(trips_path, encoding="utf-8"))
        self.pat, self.tim, self.svc, self.trips = j["patronen"], j["tijden"], [set(x) for x in j["diensten"]], j["ritten"]
        self.st = {}
        for f in json.load(open(stops_path, encoding="utf-8"))["features"]:
            c = f["geometry"]["coordinates"]
            self.st[f["properties"]["id"]] = (f["properties"]["naam"], c[1], c[0])
        self.by_key, self.by_short = {}, {}
        for i, t in enumerate(self.trips):
            if t[1]:
                self.by_key.setdefault(t[1], []).append(i)
            if t[2]:
                self.by_short.setdefault(t[2], []).append(i)
        self.span = [(t[6] + self.tim[t[5]][0], t[6] + self.tim[t[5]][-1]) for t in self.trips]

    @staticmethod
    def midnight(day):                       # 'JJJJMMDD' -> epoch van 00:00 lokale tijd
        return int(time.mktime(time.strptime(day, "%Y%m%d")))

    @staticmethod
    def days(now):
        return [time.strftime("%Y%m%d", time.localtime(now - k * 86400)) for k in (0, 1)]

    def pick(self, idxs, now, day=None):
        """Kiest uit ritten met dezelfde sleutel degene die (vandaag of gisteren) nu rijdt. Geeft (index, dag)."""
        best = None
        for d in ([day] if day else self.days(now)):
            m = self.midnight(d)
            for i in idxs:
                runs = d in self.svc[self.trips[i][7]]
                a, b = self.span[i]
                inside = m + a - 1800 <= now <= m + b + 3600
                score = (runs, inside)
                if best is None or score > best[0]:
                    best = (score, i, d)
        return (best[1], best[2]) if best else (None, None)

    def find(self, vid, now):
        if vid.startswith("ns:"):
            return self.pick(self.by_short.get(vid[3:], []), now)
        if vid.startswith("plan:"):
            _, day, i = vid.split(":", 2)
            return int(i), day
        day, _, key = vid.partition(":")
        return self.pick(self.by_key.get(key, []), now, day.replace("-", "") if len(day) == 10 else None)

    def stops(self, i, day):
        t = self.trips[i]
        m, rel, out = self.midnight(day) + t[6], self.tim[t[5]], []
        for k, sid in enumerate(self.pat[t[4]]):
            name, lat, lon = self.st.get(sid, (sid, None, None))
            out.append({"sid": sid, "name": name, "lat": lat, "lon": lon, "arr": m + rel[2 * k], "dep": m + rel[2 * k + 1]})
        return out

    def ghosts(self, now, live_keys, live_shorts, lines):
        """Ritten die volgens dienstregeling nu rijden maar waarvan geen live positie bekend is."""
        out = []
        for d in self.days(now):
            m = self.midnight(d)
            rel_now = now - m
            for i, t in enumerate(self.trips):
                a, b = self.span[i]
                if not (a <= rel_now <= b) or d not in self.svc[t[7]] or t[0] not in lines:
                    continue
                if (t[1] and t[1] in live_keys) or (t[2] and t[2] in live_shorts):
                    continue
                pat, rel, base = self.pat[t[4]], self.tim[t[5]], m + t[6]
                legs = []
                for k in range(len(pat)):
                    arr, dep = base + rel[2 * k], base + rel[2 * k + 1]
                    if dep > arr and dep >= now - 5:
                        legs.append([pat[k], pat[k], arr, dep])            # stilstand bij halte
                    if k + 1 < len(pat):
                        nxt = base + rel[2 * k + 2]
                        if nxt >= now - 5:
                            legs.append([pat[k], pat[k + 1], dep, max(nxt, dep + 1)])
                    if len(legs) >= 6:
                        break
                if legs:
                    out.append({"id": "plan:%s:%d" % (d, i), "line": t[0], "ghost": True, "legs": legs,
                                "label": t[2] or "", "head": t[3]})
        return out


def ns_journey(key, nr):
    """Ritdetails van een trein bij NS (haltes, werkelijke tijden, sporen, materieel). Ongetest formaat: tolerant lezen."""
    data = get("https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/journey?train=" + nr, {"Ocp-Apim-Subscription-Key": key})
    stops, extra = [], {}
    for st in (data.get("payload") or {}).get("stops") or []:
        if st.get("status") == "PASSING":
            continue
        arr, dep = (st.get("arrivals") or [{}])[0], (st.get("departures") or [{}])[0]
        info = st.get("stop") or {}

        def ts(x):
            return iso_ts(x[:19] + x[19:22] + ":" + x[22:] if len(x) == 24 and x[19] in "+-" else x) if x else None
        stops.append({"name": info.get("name", ""), "lat": info.get("lat"), "lon": info.get("lng"),
                      "arr": ts(arr.get("plannedTime")), "dep": ts(dep.get("plannedTime")),
                      "arr_real": ts(arr.get("actualTime")), "dep_real": ts(dep.get("actualTime")),
                      "track": dep.get("actualTrack") or dep.get("plannedTrack") or arr.get("actualTrack") or arr.get("plannedTrack") or "",
                      "cancelled": bool(arr.get("cancelled") or dep.get("cancelled"))})
        stock = st.get("actualStock") or st.get("plannedStock") or {}
        if stock and not extra:
            parts = stock.get("trainParts") or []
            extra = {"type": stock.get("trainType", ""), "seats": stock.get("numberOfSeats"), "parts": len(parts) or stock.get("numberOfParts"),
                     "image": ((parts[0].get("image") or {}).get("uri") if parts else None)}
        prod = dep.get("product") or arr.get("product") or {}
        if prod and "product" not in extra:
            extra["product"] = prod.get("longCategoryName") or prod.get("categoryCode") or ""
            extra["operator"] = prod.get("operatorName", "")
    if not stops:
        raise RuntimeError("geen haltes in NS-antwoord")
    return stops, extra


def ns(key, station="GVC"):
    data = get("https://gateway.apiportal.ns.nl/reisinformatie-api/api/v2/departures?station=" + station,
               {"Ocp-Apim-Subscription-Key": key})
    out = []
    for d in data.get("payload", {}).get("departures", []):
        prod = d.get("product", {})
        out.append({
            "line": prod.get("longCategoryName", "") or d.get("trainCategory", ""),
            "mode": "trein",
            "destination": d.get("direction", ""),
            "planned": (d.get("plannedDateTime") or "")[:19],
            "expected": (d.get("actualDateTime") or d.get("plannedDateTime") or "")[:19],
            "platform": d.get("actualTrack") or d.get("plannedTrack") or "",
            "status": "CANCEL" if d.get("cancelled") else "",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/meta.json")
    ap.add_argument("--ns-key")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--ttl", type=int, default=20, help="seconden dat een antwoord wordt hergebruikt")
    ap.add_argument("--areas", help="haltegebieden zelf opgeven, bv. GvCS,2601,2605,2609")
    ap.add_argument("--test", action="store_true", help="eenmalig ophalen, tonen en stoppen")
    ap.add_argument("--lines", default="data/lines.geojson")
    ap.add_argument("--gtfs", default="cache/gtfs-nl.zip", help="dienstregeling, om voertuigen aan lijnen te koppelen")
    ap.add_argument("--no-vehicles", action="store_true")
    ap.add_argument("--no-stream", action="store_true", help="niet meeluisteren met de live datastroom (alleen opvragen)")
    ap.add_argument("--train-ttl", type=int, default=60, help="minimaal zoveel seconden tussen twee keer treinposities ophalen")
    ap.add_argument("--vehicle-ttl", type=int, default=62, help="minimaal zoveel seconden tussen twee keer posities ophalen")
    a = ap.parse_args()
    if not a.ns_key and os.path.exists("ns.key"):          # sleutel uit bestand, zodat hij niet op de opdrachtregel staat
        a.ns_key = open("ns.key").read().strip() or None
        print("NS-sleutel gelezen uit ns.key", flush=True)
    meta = json.load(open(a.data, encoding="utf-8"))
    hub = meta["knooppunt"]
    try:
        codes = a.areas.split(",") if a.areas else find_areas(hub["lat"], hub["lon"], hub.get("straal_m", 300))
    except Exception as e:  # noqa
        print("haltegebieden ophalen mislukt (%s); bekende gebieden van Den Haag Centraal gebruikt" % e, flush=True)
        codes = ["2601", "2605", "2609", "GvCS"]
    print("%d haltegebieden: %s" % (len(codes), ",".join(codes)), flush=True)
    cache = {"t": 0, "body": b"{}"}
    vcache, vstate = {"t": 0, "body": b"{}"}, {}
    route_ids, trip_map, short_map = set(), {}, {}
    if not a.no_vehicles:
        try:
            fc = json.load(open(a.lines, encoding="utf-8"))
            route_ids = {f["properties"]["live"]["route_id"] for f in fc["features"] if f["properties"].get("feed") == "nl"}
            print("voertuigen: %d lijnen, ritten inlezen uit %s ..." % (len(route_ids), a.gtfs), flush=True)
            trip_map, short_map = load_trip_map(a.gtfs, route_ids)
            print("  %d ritsleutels en %d treinnummers geladen" % (len(trip_map), len(short_map)), flush=True)
        except Exception as e:  # noqa
            print("voertuigen uitgeschakeld:", e, flush=True)

    plan = None
    try:
        plan = Plan(os.path.join(os.path.dirname(a.lines), "trips.json"), os.path.join(os.path.dirname(a.lines), "stops.geojson"))
        line_ids = {f["properties"]["id"] for f in json.load(open(a.lines, encoding="utf-8"))["features"]}
        print("ritplannen: %d ritten geladen" % len(plan.trips), flush=True)
    except Exception as e:  # noqa
        print("ritplannen niet beschikbaar (%s); draai build_data.py opnieuw voor het infopaneel" % e, flush=True)
    jcache = {}

    def live_of(vid):
        if stream is not None:
            with stream.lock:
                v = stream.veh.get(vid) or stream.trains.get(vid)
                if v:
                    return dict(v)
        for v in vstate.get("good", []) + vstate.get("trains", []):
            if v["id"] == vid:
                return dict(v)
        return {}

    def build_trip(vid):
        now = time.time()
        live = live_of(vid)
        live.pop("seen", None)
        out = {"id": vid, "live": live, "now": int(now), "source": "dienstregeling", "stops": [], "extra": {}}
        if plan is not None:
            i, day = plan.find(vid, now)
            if i is not None:
                t = plan.trips[i]
                out.update(line=t[0], head=t[3], number=t[2], stops=plan.stops(i, day))
        if vid.startswith("ns:") and a.ns_key:
            c = jcache.get(vid)
            if not c or now - c[0] > 30:
                try:
                    c = (now, ns_journey(a.ns_key, vid[3:]), None)
                except Exception as e:  # noqa
                    c = (now, None, str(e))
                jcache[vid] = c
                for k in [k for k, x in jcache.items() if now - x[0] > 600]:
                    del jcache[k]
            if c[1]:
                out["stops"], out["extra"], out["source"] = c[1][0], c[1][1], "NS"
            elif c[2]:
                out["note"] = "NS-ritdetails niet beschikbaar: " + c[2]
        delay = live.get("delay")
        for st in out["stops"]:                      # verwachte tijden: gepland + actuele vertraging
            if "arr_real" not in st:
                st["arr_real"] = st["arr"] + delay if delay is not None and st["arr"] else None
                st["dep_real"] = st["dep"] + delay if delay is not None and st["dep"] else None
        return json.dumps(out).encode()

    def add_ghosts(vs):
        if plan is None:
            return vs
        keys = {v["id"].split(":", 1)[-1] for v in vs if not v["id"].startswith("ns:")}
        shorts = {v["id"][3:] for v in vs if v["id"].startswith("ns:")}
        try:
            return vs + plan.ghosts(time.time(), keys, shorts, line_ids)
        except Exception as e:  # noqa
            print("schaduwvoertuigen:", e, flush=True)
            return vs

    stream = None
    if route_ids and not a.no_stream:
        try:
            import zmq  # noqa
            stream = Stream(trip_map, short_map)
            stream.start()
        except ImportError:
            print("live datastroom uit: pakket python3-zmq ontbreekt (sudo apt install python3-zmq)", flush=True)

    def build_vehicles():
        kv6_live = stream is not None and stream.alive("kv6")
        ns_live = stream is not None and stream.alive("ns")
        if kv6_live and ns_live:
            return json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "errors": [], "stale": False,
                               "stream": {"kv6": True, "ns": True},
                               "vehicles": add_ghosts(stream.snapshot("kv6") + stream.snapshot("ns"))}).encode()
        j = json.loads(build_polled(not kv6_live, not ns_live))
        if kv6_live:
            j["vehicles"] = stream.snapshot("kv6") + [v for v in j["vehicles"] if v["id"].startswith("ns:")]
            j["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if ns_live:
            j["vehicles"] = [v for v in j["vehicles"] if not v["id"].startswith("ns:")] + stream.snapshot("ns")
        j["stream"] = {"kv6": kv6_live, "ns": ns_live}
        j["vehicles"] = add_ghosts(j["vehicles"])
        return json.dumps(j).encode()

    def build_polled(want_ovapi=True, want_ns=True):
        # Bij een fout (bv. OVapi weigert omdat we te vaak vragen) de laatste goede posities
        # blijven doorgeven en de tussenpoos verdubbelen, tot maximaal twee minuten.
        errors = []
        if want_ovapi and time.time() >= vstate.get("next", 0):
            try:
                vs = vehicles(route_ids, trip_map, vstate)
                if vs or not vstate.get("good"):
                    vstate["good"], vstate["good_at"] = vs, time.strftime("%Y-%m-%dT%H:%M:%S")
                vstate["wait"] = a.vehicle_ttl
            except Exception as e:  # noqa
                errors.append(str(e))
                vstate["wait"] = min(120, max(a.vehicle_ttl, vstate.get("wait", a.vehicle_ttl)) * 2)
                print(time.strftime("%H:%M:%S"), "voertuigen:", e, "- volgende poging over %d s" % vstate["wait"], flush=True)
            vstate["next"] = time.time() + vstate["wait"] - 1
        if want_ns and a.ns_key and time.time() >= vstate.get("train_next", 0):
            try:
                vstate["trains"] = trains(a.ns_key, short_map, vstate)
                vstate["train_wait"] = a.train_ttl
                vstate["train_err"] = None
            except Exception as e:  # noqa
                # laatste goede treinposities blijven staan; daarna steeds langer wachten (max. 10 min)
                vstate["train_wait"] = min(600, max(a.train_ttl, vstate.get("train_wait", a.train_ttl)) * 2)
                vstate["train_err"] = str(e)
                print(time.strftime("%H:%M:%S"), e, "- volgende poging over %d s" % vstate["train_wait"], flush=True)
            vstate["train_next"] = time.time() + vstate["train_wait"] - 2
        if vstate.get("train_err"):
            errors.append(vstate["train_err"])
        if not vstate.get("good_at"):
            vstate["good_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return json.dumps({"updated": vstate.get("good_at", ""), "vehicles": vstate.get("good", []) + vstate.get("trains", []), "errors": errors,
                           "stale": bool(errors) and not vstate.get("good"), "feed_total": vstate.get("seen", 0)}).encode()

    def build():
        deps, errors = [], []
        for name, fn in (("ovapi", lambda: ovapi(codes)), ("ns", (lambda: ns(a.ns_key)) if a.ns_key else None)):
            if fn is None:
                continue
            try:
                deps += fn()
            except Exception as e:  # noqa
                errors.append("%s: %s" % (name, e))
        deps.sort(key=lambda d: d.get("expected") or d.get("planned") or "")
        return json.dumps({"updated": time.strftime("%Y-%m-%dT%H:%M:%S"), "departures": deps[:80], "errors": errors}).encode()

    if a.test:
        j = json.loads(build())
        print("fouten:", j["errors"])
        print("%d vertrekken" % len(j["departures"]))
        for d in j["departures"][:12]:
            print("  %s  %-5s %-9s %-28s %s" % (d["expected"][11:16], d["mode"], d["line"], d["destination"][:28], d["kv78"] if "kv78" in d else ""))
        if stream is not None:
            print("live datastroom: 25 seconden meeluisteren ...", flush=True)
            time.sleep(25)
            for name in ("kv6", "ns"):
                print("  %s: %d berichten, %d meldingen op onze lijnen, %d voertuigen met positie, fout: %s"
                      % (name, stream.count[name], stream.count[name + "_ours"], len(stream.snapshot(name)), stream.error.get(name)))
                print("    begin eerste bericht:", (stream.sample.get(name) or "(niets ontvangen)")[:420].replace("\n", " "))
                for v in stream.snapshot(name)[:2]:
                    print("    ", v)
        if plan is not None:
            g = plan.ghosts(time.time(), set(), set(), line_ids)
            print("ritplannen: volgens dienstregeling rijden nu %d ritten op onze lijnen" % len(g))
            if stream is not None:
                vs = json.loads(build_vehicles())["vehicles"]
                gh = [v for v in vs if v.get("ghost")]
                print("  waarvan %d zonder live positie (schaduwvoertuigen); voorbeeld: %s" % (len(gh), json.dumps(gh[0])[:260] if gh else "-"))
                real = [v for v in vs if not v.get("ghost")]
                if real:
                    print("  ritdetails voorbeeld:", build_trip(real[0]["id"]).decode()[:420])
        if route_ids:
            j = json.loads(build_polled())
            print("voertuigen: %d op onze lijnen, %d in het hele bestand, fouten: %s" % (len(j["vehicles"]), j["feed_total"], j["errors"]))
            print("  bron:", vstate.get("url"), " voorbeeld uit bestand:", vstate.get("sample"))
            for v in j["vehicles"][:4]:
                print("  ", v)
            if a.ns_key:
                tr = [v for v in j["vehicles"] if v["id"].startswith("ns:")]
                print("treinen: %d op onze lijnen, %d in het hele land; bron: %s" % (len(tr), vstate.get("train_total", 0), vstate.get("train_url")))
                print("  voorbeeld uit antwoord:", vstate.get("train_sample"))
                for v in tr[:4]:
                    print("  ", v)
        return

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/vehicles"):
                if time.time() - vcache["t"] > 2:
                    vcache["body"], vcache["t"] = build_vehicles(), time.time()
                body = vcache["body"]
            elif self.path.startswith("/trip"):
                from urllib.parse import urlparse, parse_qs
                vid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
                body = build_trip(vid)
            elif self.path.startswith("/departures"):
                if time.time() - cache["t"] > a.ttl:
                    cache["body"], cache["t"] = build(), time.time()
                body = cache["body"]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    print("luistert op http://0.0.0.0:%d  (/departures en /vehicles)" % a.port, flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
