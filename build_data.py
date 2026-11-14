#!/usr/bin/env python3
"""
build_data.py - maakt de kaartdata voor "OV vanaf Den Haag Centraal".

Haalt de landelijke GTFS-dienstregeling (OVapi) en de Flixbus-feed op, zoekt
alle ritten die het knooppunt Den Haag Centraal aandoen en schrijft:

  data/lines.geojson   routes per lijn (MultiLineString + eigenschappen)
  data/stops.geojson   alle haltes/stations op die lijnen
  data/meta.json       bron, peildata, haltes van het knooppunt (voor live)
  data/ov-data.js      alles samen, direct te laden door index.html

Alleen de standaardbibliotheek van Python 3.8+ is nodig.

Gebruik:
  python3 build_data.py                 # downloadt alles naar ./cache
  python3 build_data.py --nl pad/naar/gtfs-nl.zip --flix pad/naar/flix.zip
  python3 build_data.py --no-flix --radius 250
"""
import argparse, csv, io, json, math, os, re, sys, time, urllib.parse, urllib.request, zipfile
from array import array
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

HUB_LAT, HUB_LON = 52.08085, 4.32470          # Den Haag Centraal
NL_URLS = [
    "https://gtfs.ovapi.nl/nl/gtfs-nl.zip",
    "https://gtfs.openov.nl/gtfs-rt/gtfs-openov-nl.zip",
    "http://gtfs.ovapi.nl/gtfs-nl.zip",
]
FLIX_URLS = [
    "https://gtfs.gis.flix.tech/gtfs_generic_eu.zip",
    "http://gtfs.gis.flix.tech/gtfs_generic_eu.zip",
]
UA = "ov-kaart-denhaag/1.0 (persoonlijk project)"
csv.field_size_limit(10**9)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- helpers
def download(urls, dest, max_age_h):
    if os.path.exists(dest) and time.time() - os.path.getmtime(dest) < max_age_h * 3600:
        log("cache gebruikt:", dest)
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last = None
    for url in urls:
        try:
            log("download", url)
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done = 0
                nxt = 0
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
                    done += len(b)
                    if done >= nxt:
                        log("  %d MB%s" % (done >> 20, " / %d MB" % (total >> 20) if total else ""))
                        nxt += 25 << 20
            os.replace(dest + ".part", dest)
            return dest
        except Exception as e:  # noqa
            last = e
            log("  mislukt:", e)
    raise RuntimeError("download mislukt (%s)" % last)


def rows(zf, name):
    """Streamt een GTFS-tabel als (header-index, rij-iterator). Ontbreekt het bestand: (None, [])."""
    cand = [n for n in zf.namelist() if n == name or n.endswith("/" + name)]
    if not cand:
        return None, iter(())
    f = io.TextIOWrapper(zf.open(cand[0]), encoding="utf-8-sig", newline="")
    rd = csv.reader(f)
    try:
        header = next(rd)
    except StopIteration:
        return None, iter(())
    return {h.strip(): i for i, h in enumerate(header)}, rd


def col(ix, name, default=None):
    i = ix.get(name)
    if i is None:
        return lambda r: default
    return lambda r: r[i] if i < len(r) else default


def secs(t):
    if not t:
        return -1
    p = t.split(":")
    try:
        return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)
    except ValueError:
        return -1


def dist_m(lat1, lon1, lat2, lon2):
    k = 111320.0
    dx = (lon2 - lon1) * k * math.cos(math.radians((lat1 + lat2) / 2))
    dy = (lat2 - lat1) * k
    return math.hypot(dx, dy)


def simplify(pts, tol):
    """Douglas-Peucker (iteratief). pts: [(lon, lat)], tol in graden."""
    n = len(pts)
    if n < 3:
        return pts
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        ax, ay = pts[a]
        bx, by = pts[b]
        dx, dy = bx - ax, by - ay
        l2 = dx * dx + dy * dy
        best, bi = -1.0, -1
        for i in range(a + 1, b):
            px, py = pts[i]
            if l2 == 0:
                d = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / l2
                t = 0 if t < 0 else 1 if t > 1 else t
                d = (px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2
            if d > best:
                best, bi = d, i
        if best > tol * tol:
            keep[bi] = True
            stack.append((a, bi))
            stack.append((bi, b))
    return [p for p, k in zip(pts, keep) if k]


_road_cache, _road_state = {}, {"fails": 0, "path": None}


def road_route(coords, base):
    """Route over de weg langs de haltes via OSRM. coords: [(lon, lat)]. Geeft None bij een fout."""
    key = ";".join("%.5f,%.5f" % c for c in coords)
    if key in _road_cache:
        return [tuple(p) for p in _road_cache[key]]
    if _road_state["fails"] >= 3:
        return None
    out = []
    try:
        for i in range(0, len(coords) - 1, 24):
            part = coords[i:i + 25]
            url = "%s/route/v1/driving/%s?overview=full&geometries=geojson&continue_straight=false" % (
                base.rstrip("/"), ";".join("%.5f,%.5f" % c for c in part))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                j = json.loads(r.read().decode("utf-8"))
            if j.get("code") != "Ok" or not j.get("routes"):
                raise RuntimeError(j.get("code") or "geen route")
            g = j["routes"][0]["geometry"]["coordinates"]
            out.extend((float(x), float(y)) for x, y in (g[1:] if out else g))
            time.sleep(1.1)
    except Exception as e:  # noqa
        _road_state["fails"] += 1
        log("   wegroute mislukt (%s)%s" % (e, "; ik stop met proberen" if _road_state["fails"] >= 3 else ""))
        return None
    _road_cache[key] = out
    if _road_state["path"]:
        with open(_road_state["path"], "w") as f:
            json.dump(_road_cache, f)
    return out


# ---------------------------------------------------------------- echte sporen en busroutes uit OpenStreetMap
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
OSM_GROUP = {"tram": "rail", "light_rail": "rail", "subway": "rail", "bus": "bus", "trolleybus": "bus"}


def fetch_osm(cache_dir, radius, max_age_h):
    """Haalt alle tram-, metro- en busroutes (OSM-relaties) op die bij het knooppunt langskomen."""
    path = os.path.join(cache_dir, "osm-routes.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
        log("cache gebruikt:", path)
        return json.load(open(path, encoding="utf-8"))
    q = ('[out:json][timeout:180];(node(around:%d,%f,%f);way(around:%d,%f,%f);)->.a;'
         '(rel(bn.a)[type=route][route~"^(tram|bus|subway|light_rail|trolleybus)$"];'
         'rel(bw.a)[type=route][route~"^(tram|bus|subway|light_rail|trolleybus)$"];);out geom;'
         % (radius, HUB_LAT, HUB_LON, radius, HUB_LAT, HUB_LON))
    last = None
    for url in OVERPASS:
        try:
            log("OpenStreetMap-routes ophalen via", url.split("/")[2])
            req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=240) as r:
                raw = r.read()
            j = json.loads(raw.decode("utf-8"))
            if not j.get("elements"):
                raise RuntimeError("leeg antwoord")
            with open(path, "wb") as f:
                f.write(raw)
            return j
        except Exception as e:  # noqa
            last = e
            log("   mislukt:", e)
    log("LET OP: OpenStreetMap niet bereikbaar (%s); ik gebruik de lijnen uit de dienstregeling." % last)
    return None


KX = 111320.0 * math.cos(math.radians(HUB_LAT))
KY = 111320.0


class OsmGraph:
    """Netwerk van de wegdelen (sporen/straten) van een of meer OSM-routes. De volgorde van de
    wegdelen in OSM doet er niet toe: we zoeken zelf de weg van halte naar halte."""

    def __init__(self):
        self.ids, self.xy, self.adj, self.edges = {}, [], [], []
        self._seen = set()

    def node(self, lon, lat):
        k = (round(lon, 7), round(lat, 7))
        i = self.ids.get(k)
        if i is None:
            i = self.ids[k] = len(self.xy)
            self.xy.append(((lon - HUB_LON) * KX, (lat - HUB_LAT) * KY))
            self.adj.append([])
        return i

    def add_way(self, pts):
        prev = None
        for lon, lat in pts:
            i = self.node(lon, lat)
            if prev is not None and prev != i and (prev, i) not in self._seen:
                self._seen.add((prev, i)); self._seen.add((i, prev))
                (x1, y1), (x2, y2) = self.xy[prev], self.xy[i]
                w = math.hypot(x2 - x1, y2 - y1)
                self.adj[prev].append((i, w)); self.adj[i].append((prev, w))
                self.edges.append((prev, i, min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)))
            prev = i

    def snap(self, px, py, limit):
        """Dichtstbijzijnde punt op het netwerk: (afstand, u, v, qx, qy) of None."""
        best, bd = None, limit * limit
        xy = self.xy
        for u, v, x0, x1, y0, y1 in self.edges:
            if px < x0 - limit or px > x1 + limit or py < y0 - limit or py > y1 + limit:
                continue
            ax, ay = xy[u]
            dx, dy = xy[v][0] - ax, xy[v][1] - ay
            l2 = dx * dx + dy * dy
            t = ((px - ax) * dx + (py - ay) * dy) / l2 if l2 else 0.0
            t = 0.0 if t < 0 else 1.0 if t > 1 else t
            qx, qy = ax + t * dx, ay + t * dy
            d = (px - qx) ** 2 + (py - qy) ** 2
            if d < bd:
                bd, best = d, (u, v, qx, qy)
        return None if best is None else (math.sqrt(bd),) + best

    def route(self, a, b, max_len):
        """Kortste weg over het netwerk tussen twee gesnapte punten. Geeft [(x, y)] of None."""
        _, au, av, ax, ay = a
        _, bu, bv, bx, by = b
        if {au, av} == {bu, bv}:
            return [(ax, ay), (bx, by)]
        import heapq
        xy = self.xy
        dist, prev, heap = {}, {}, []
        for n in (au, av):
            d0 = math.hypot(xy[n][0] - ax, xy[n][1] - ay)
            dist[n] = d0
            heapq.heappush(heap, (d0, n))
        targets = {bu: math.hypot(xy[bu][0] - bx, xy[bu][1] - by), bv: math.hypot(xy[bv][0] - bx, xy[bv][1] - by)}
        best, best_n, done = float("inf"), None, set()
        while heap:
            d, n = heapq.heappop(heap)
            if n in done:
                continue
            done.add(n)
            if d > max_len or d >= best:
                break
            if n in targets and d + targets[n] < best:
                best, best_n = d + targets[n], n
            for m, w in self.adj[n]:
                nd = d + w
                if nd < dist.get(m, float("inf")):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd, m))
        if best_n is None or best > max_len:
            return None
        path, n = [], best_n
        while n is not None:
            path.append(xy[n])
            n = prev.get(n)
        path.reverse()
        return [(ax, ay)] + path + [(bx, by)]


def build_osm_index(j):
    """(groep, lijnnummer) -> lijst netwerken: eerst elke OSM-route apart, als laatste alle samen."""
    idx, per_ref, n = defaultdict(list), defaultdict(list), 0
    for el in j.get("elements", []):
        if el.get("type") != "relation":
            continue
        tags = el.get("tags", {})
        grp, ref = OSM_GROUP.get(tags.get("route", "")), (tags.get("ref") or "").strip().lower()
        if not grp or not ref:
            continue
        ways = []
        for m in el.get("members", []):
            role = m.get("role") or ""
            if m.get("type") == "way" and m.get("geometry") and not role.startswith(("platform", "stop")):
                ways.append([(g["lon"], g["lat"]) for g in m["geometry"] if g])
        if not ways:
            continue
        g = OsmGraph()
        for w in ways:
            g.add_way(w)
        idx[(grp, ref)].append(g)
        per_ref[(grp, ref)].extend(ways)
        n += 1
    for key, ways in per_ref.items():
        if len(idx[key]) > 1:
            g = OsmGraph()
            for w in ways:
                g.add_way(w)
            idx[key].append(g)
    log("OpenStreetMap: %d routes voor %d lijnnummers" % (n, len(idx)))
    return idx


def osm_geometry(idx, grp, ref, stops, hub_flags):
    """Legt een haltereeks over het echte spoor of de echte weg. Geeft [(lon, lat)] of None."""
    cands = idx.get((grp, ref.strip().lower()))
    if not cands or len(stops) < 2:
        return None
    sxy = [((x - HUB_LON) * KX, (y - HUB_LAT) * KY) for x, y in stops]
    last = len(sxy) - 1
    # het knooppunt zelf is het midden van het station; als tussenhalte slaan we het over
    use = [k for k in range(len(sxy)) if not hub_flags[k] or k in (0, last)]
    if len(use) < 2:
        return None
    best = None
    for g in cands:
        snaps, ok = [], 0
        for k in use:
            sn = g.snap(sxy[k][0], sxy[k][1], 400 if hub_flags[k] else 120)
            snaps.append(sn)
            ok += sn is not None
        if ok / len(use) < 0.85:
            continue
        pts_xy, fails, legs, cur = [], 0, 0, None
        for k, sn in zip(use, snaps):
            if sn is None:
                continue
            if cur is not None:
                (pk, psn) = cur
                straight = math.hypot(sxy[k][0] - sxy[pk][0], sxy[k][1] - sxy[pk][1])
                leg = g.route(psn, sn, straight * 3 + 500)
                legs += 1
                if leg is None:
                    fails += 1
                    leg = [(psn[3], psn[4]), (sn[3], sn[4])]
                pts_xy.extend(leg if not pts_xy else leg[1:])
            cur = (k, sn)
        if not legs or fails / legs > 0.15:
            continue
        score = (-fails, ok)
        if best is None or score > best[0]:
            best = (score, pts_xy)
        if fails == 0 and ok == len(use):
            break
    if best is None:
        return None
    return [(x / KX + HUB_LON, y / KY + HUB_LAT) for x, y in best[1]]


def median(v):
    v = sorted(v)
    return v[len(v) // 2]


def mode_of(route_type):
    """GTFS route_type (basis en uitgebreid) -> modaliteit."""
    try:
        t = int(route_type)
    except (TypeError, ValueError):
        return "bus"
    if t in (0, 5, 12) or 900 <= t < 1000:
        return "tram"
    if t == 1 or 400 <= t < 500 or 500 <= t < 700:
        return "metro"
    if t == 2 or 100 <= t < 200:
        return "trein"
    if t == 4 or t == 1000 or t == 1200:
        return "veer"
    return "bus"  # 3, 200-299 (touringcar), 700-799


# ---------------------------------------------------------------- per feed
class Feed:
    def __init__(self, key, path):
        self.key, self.path = key, path


def process_feed(feed, radius, exclude_re, today):
    zf = zipfile.ZipFile(feed.path)
    out = {"feed": feed.key}

    # -- stops
    ix, rd = rows(zf, "stops.txt")
    if ix is None:
        raise RuntimeError("stops.txt ontbreekt in " + feed.path)
    g = {k: col(ix, k, "") for k in ("stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon",
                                     "location_type", "parent_station", "platform_code")}
    stops, hub_ids = {}, set()
    for r in rd:
        try:
            lat, lon = float(g["stop_lat"](r)), float(g["stop_lon"](r))
        except ValueError:
            continue
        sid = g["stop_id"](r)
        stops[sid] = (g["stop_name"](r).strip(), lat, lon, g["parent_station"](r), g["stop_code"](r),
                      g["platform_code"](r), g["location_type"](r) or "0")
        if g["location_type"](r) in ("", "0") and dist_m(HUB_LAT, HUB_LON, lat, lon) <= radius:
            if exclude_re and exclude_re.search(g["stop_name"](r)):
                continue
            hub_ids.add(sid)
    log("[%s] %d haltes, waarvan %d binnen %d m van Den Haag Centraal" % (feed.key, len(stops), len(hub_ids), radius))
    for n, c in Counter(stops[s][0] for s in hub_ids).most_common():
        log("     knooppunthalte: %-45s (%d perrons/haltepalen)" % (n, c))
    if not hub_ids:
        return None

    # -- stop_times, ronde 1: welke ritten doen het knooppunt aan
    ix, rd = rows(zf, "stop_times.txt")
    i_trip, i_stop = ix["trip_id"], ix["stop_id"]
    hub_trips = set()
    n = 0
    for r in rd:
        n += 1
        if r[i_stop] in hub_ids:
            hub_trips.add(r[i_trip])
    log("[%s] stop_times ronde 1: %d regels, %d ritten via het knooppunt" % (feed.key, n, len(hub_trips)))

    # -- stop_times, ronde 2: volledige ritten
    ix, rd = rows(zf, "stop_times.txt")
    i_seq, i_arr, i_dep = ix["stop_sequence"], ix.get("arrival_time"), ix.get("departure_time")
    stop_idx, stop_list = {}, []
    tt = {}  # trip_id -> array('i') [seq, stopidx, arr, dep, ...]
    for r in rd:
        tid = r[i_trip]
        if tid not in hub_trips:
            continue
        s = r[i_stop]
        si = stop_idx.get(s)
        if si is None:
            si = stop_idx[s] = len(stop_list)
            stop_list.append(s)
        a = secs(r[i_arr]) if i_arr is not None else -1
        d = secs(r[i_dep]) if i_dep is not None else -1
        if a < 0:
            a = d
        if d < 0:
            d = a
        arr = tt.get(tid)
        if arr is None:
            arr = tt[tid] = array("i")
        arr.extend((int(r[i_seq]), si, a, d))
    log("[%s] stop_times ronde 2 klaar" % feed.key)

    # -- trips
    ix, rd = rows(zf, "trips.txt")
    g = {k: col(ix, k, "") for k in ("route_id", "service_id", "trip_id", "trip_headsign", "direction_id",
                                     "shape_id", "realtime_trip_id", "trip_short_name")}
    trips = {}
    for r in rd:
        tid = g["trip_id"](r)
        if tid in tt:
            trips[tid] = (g["route_id"](r), g["service_id"](r), g["trip_headsign"](r).strip(),
                          g["direction_id"](r) or "0", g["shape_id"](r), g["realtime_trip_id"](r),
                          g["trip_short_name"](r).strip())
    services = {t[1] for t in trips.values()}

    # -- routes, agency
    ix, rd = rows(zf, "agency.txt")
    agencies = {}
    if ix is not None:
        ga, gn = col(ix, "agency_id", ""), col(ix, "agency_name", "")
        for r in rd:
            agencies[ga(r)] = gn(r).strip()
    ix, rd = rows(zf, "routes.txt")
    g = {k: col(ix, k, "") for k in ("route_id", "agency_id", "route_short_name", "route_long_name",
                                     "route_type", "route_color", "route_text_color")}
    routes = {}
    used_routes = {t[0] for t in trips.values()}
    for r in rd:
        rid = g["route_id"](r)
        if rid in used_routes:
            routes[rid] = {k: g[k](r).strip() for k in g}

    # -- kalender
    svc_dates = defaultdict(set)
    ix, rd = rows(zf, "calendar.txt")
    if ix is not None:
        gs = col(ix, "service_id")
        days = [col(ix, d, "0") for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
        g0, g1 = col(ix, "start_date"), col(ix, "end_date")
        for r in rd:
            if gs(r) not in services:
                continue
            try:
                d0 = datetime.strptime(g0(r), "%Y%m%d").date()
                d1 = datetime.strptime(g1(r), "%Y%m%d").date()
            except (TypeError, ValueError):
                continue
            d = d0
            while d <= d1 and (d - d0).days < 800:
                if days[d.weekday()](r) == "1":
                    svc_dates[gs(r)].add(d.toordinal())
                d += timedelta(days=1)
    ix, rd = rows(zf, "calendar_dates.txt")
    if ix is not None:
        gs, gd, ge = col(ix, "service_id"), col(ix, "date"), col(ix, "exception_type", "1")
        for r in rd:
            s = gs(r)
            if s not in services:
                continue
            try:
                o = datetime.strptime(gd(r), "%Y%m%d").date().toordinal()
            except (TypeError, ValueError):
                continue
            if ge(r) == "2":
                svc_dates[s].discard(o)
            else:
                svc_dates[s].add(o)

    # -- feed_info
    info = {}
    ix, rd = rows(zf, "feed_info.txt")
    if ix is not None:
        for r in rd:
            info = {k: (r[i] if i < len(r) else "") for k, i in ix.items()}
            break

    out.update(stops=stops, hub_ids=hub_ids, stop_list=stop_list, tt=tt, trips=trips, routes=routes,
               agencies=agencies, svc_dates=svc_dates, info=info, zf=zf)
    return out


def read_shapes(zf, wanted):
    ix, rd = rows(zf, "shapes.txt")
    res = defaultdict(list)
    if ix is None or not wanted:
        return res
    i_id, i_seq, i_lat, i_lon = ix["shape_id"], ix["shape_pt_sequence"], ix["shape_pt_lat"], ix["shape_pt_lon"]
    for r in rd:
        if r[i_id] in wanted:
            try:
                res[r[i_id]].append((int(r[i_seq]), float(r[i_lon]), float(r[i_lat])))
            except ValueError:
                pass
    return {k: [(x, y) for _, x, y in sorted(v)] for k, v in res.items()}


# ---------------------------------------------------------------- hoofdprogramma
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nl", help="pad naar gtfs-nl.zip (anders downloaden)")
    ap.add_argument("--flix", help="pad naar Flixbus GTFS-zip (anders downloaden)")
    ap.add_argument("--no-flix", action="store_true", help="Flixbus overslaan")
    ap.add_argument("--extra", action="append", default=[], metavar="NAAM=PAD",
                    help="extra GTFS-feed toevoegen, bv. --extra blablacar=blabla.zip")
    ap.add_argument("--radius", type=float, default=300, help="straal knooppunt in meters (standaard 300)")
    ap.add_argument("--exclude", help="regex: haltenamen binnen de straal die niet meetellen")
    ap.add_argument("--out", default="data")
    ap.add_argument("--cache", default="cache")
    ap.add_argument("--max-age", type=float, default=20, help="cache hergebruiken tot zoveel uur oud")
    ap.add_argument("--tolerance", type=float, default=8, help="vereenvoudiging van routes in meters")
    ap.add_argument("--date", help="peildatum JJJJ-MM-DD (standaard vandaag)")
    ap.add_argument("--no-osm", action="store_true", help="tram-, metro- en busroutes niet over de echte sporen/wegen uit OpenStreetMap leggen")
    ap.add_argument("--no-roads", action="store_true", help="rechte lijnen van langeafstandsbussen niet over de weg leggen")
    ap.add_argument("--osrm", default="https://router.project-osrm.org", help="adres van een OSRM-routeserver")
    a = ap.parse_args()

    today = datetime.strptime(a.date, "%Y-%m-%d").date() if a.date else date.today()
    excl = re.compile(a.exclude) if a.exclude else None
    feeds = [Feed("nl", a.nl or download(NL_URLS, os.path.join(a.cache, "gtfs-nl.zip"), a.max_age))]
    if not a.no_flix:
        try:
            feeds.append(Feed("flix", a.flix or download(FLIX_URLS, os.path.join(a.cache, "flix.zip"), a.max_age)))
        except Exception as e:  # noqa
            log("LET OP: Flixbus-feed niet beschikbaar (%s). Ga verder zonder; geef hem mee met --flix." % e)
    for x in a.extra:
        k, p = x.split("=", 1)
        feeds.append(Feed(k, p))

    data = [d for d in (process_feed(f, a.radius, excl, today) for f in feeds) if d]
    if not data:
        sys.exit("Geen enkele halte gevonden bij Den Haag Centraal; klopt de feed?")

    # ---- peildagen kiezen: doordeweeks (di/do), zaterdag, zondag met een 'gewone' hoeveelheid ritten
    per_date = Counter()
    for d in data:
        per_svc = Counter(t[1] for t in d["trips"].values())
        own = set()
        for s, n in per_svc.items():
            for o in d["svc_dates"].get(s, ()):
                per_date[o] += n
                own.add(o)
        d["n_weeks"] = max(1, len({date.fromordinal(o).isocalendar()[:2] for o in own}))
    if not per_date:
        sys.exit("Geen kalendergegevens gevonden.")
    feed_start, feed_end = min(per_date), max(per_date)

    def pick(weekdays):
        c = [o for o in per_date if date.fromordinal(o).weekday() in weekdays and today.toordinal() <= o <= today.toordinal() + 42]
        if len(c) < 2:
            c = [o for o in per_date if date.fromordinal(o).weekday() in weekdays]
        if not c:
            return None
        c.sort(key=lambda o: (per_date[o], o))
        return c[len(c) // 2]

    ref = {"wk": pick((1, 3)), "za": pick((5,)), "zo": pick((6,))}
    log("peildagen:", {k: (date.fromordinal(v).isoformat() if v else None) for k, v in ref.items()})

    # ---- stations samenvoegen over feeds heen
    stations = []            # {name, lat, lon, n, hub}
    by_name = defaultdict(list)
    hub_station = {"name": "Den Haag Centraal", "lat": HUB_LAT, "lon": HUB_LON, "n": 1, "hub": True, "sum": None}
    stations.append(hub_station)

    def station_for(d, sid, memo):
        if sid in memo:
            return memo[sid]
        if sid in d["hub_ids"]:
            memo[sid] = 0
            return 0
        name, lat, lon, parent = d["stops"][sid][:4]
        if parent and parent in d["stops"]:
            if d["stops"][parent][0]:
                name = d["stops"][parent][0]
        key = re.sub(r"\s+", " ", name.lower())
        for idx in by_name[key]:
            st = stations[idx]
            if dist_m(st["lat"], st["lon"], lat, lon) < 500:
                st["sum"][0] += lat
                st["sum"][1] += lon
                st["n"] += 1
                memo[sid] = idx
                return idx
        stations.append({"name": name, "lat": lat, "lon": lon, "n": 1, "hub": False, "sum": [lat, lon]})
        by_name[key].append(len(stations) - 1)
        memo[sid] = len(stations) - 1
        return memo[sid]

    # ---- lijnen opbouwen
    lines = {}
    hub_stops_meta = []
    plan_pat, plan_tim, plan_svc, plan_trips = {}, {}, {}, []
    for d in data:
        memo = {}
        stop_list, stops = d["stop_list"], d["stops"]
        used_hub = Counter()
        for tid, arr in d["tt"].items():
            tr = d["trips"].get(tid)
            if not tr:
                continue
            rid, svc, headsign, direction, shape_id, rt_id, short_name = tr
            n = len(arr) // 4
            seq = sorted(range(n), key=lambda i: arr[i * 4])
            sts = [station_for(d, stop_list[arr[i * 4 + 1]], memo) for i in seq]
            if 0 not in sts:
                continue
            h = sts.index(0)
            hub_i = seq[h]
            used_hub[stop_list[arr[hub_i * 4 + 1]]] += 1
            hub_dep, hub_arr = arr[hub_i * 4 + 3], arr[hub_i * 4 + 2]
            lid = d["feed"] + ":" + rid
            L = lines.get(lid)
            if L is None:
                L = lines[lid] = {"feed": d["feed"], "route": d["routes"].get(rid, {"route_id": rid}), "d": d,
                                  "pat": Counter(), "pat_shape": defaultdict(Counter), "pat_head": defaultdict(Counter),
                                  "tf": defaultdict(list), "tto": defaultdict(list), "dates": set(),
                                  "hourly": {k: [[0] * 24, [0] * 24] for k in ref}, "night": [0, 0], "rt": Counter(),
                                  "heads": [Counter(), Counter()], "ntrips": 0}
            # opeenvolgende dubbele stations (zelfde station, ander perron) samenvoegen
            pat = []
            for s in sts:
                if not pat or pat[-1] != s:
                    pat.append(s)
            pat = tuple(pat)
            # ritplan voor het live-deel: per station aankomst (eerste perron) en vertrek (laatste perron)
            tm, last_s = [], None
            for j, i in enumerate(seq):
                if sts[j] != last_s:
                    tm.append([arr[i * 4 + 2], arr[i * 4 + 3]])
                    last_s = sts[j]
                else:
                    tm[-1][1] = arr[i * 4 + 3]
            t0 = tm[0][1]
            rel = tuple(x - t0 for ad in tm for x in ad)
            pi = plan_pat.setdefault(pat, len(plan_pat))
            ti = plan_tim.setdefault(rel, len(plan_tim))
            sk = (d["feed"], svc)
            if sk not in plan_svc:
                lo, hi = today.toordinal() - 1, today.toordinal() + 14
                plan_svc[sk] = (len(plan_svc), sorted(o for o in d["svc_dates"].get(svc, ()) if lo <= o <= hi))
            plan_trips.append([lid, rt_id, short_name, headsign, pi, ti, t0, plan_svc[sk][0]])
            L["pat"][pat] += 1
            L["pat_shape"][pat][shape_id] += 1
            L["pat_head"][pat][headsign] += 1
            L["ntrips"] += 1
            for j, i in enumerate(seq):
                s = sts[j]
                if s == 0:
                    continue
                if j > h:
                    v = L["tf"][s]
                    if len(v) < 60:
                        v.append(arr[i * 4 + 2] - hub_dep)
                else:
                    v = L["tto"][s]
                    if len(v) < 60:
                        v.append(hub_arr - arr[i * 4 + 3])
            dts = d["svc_dates"].get(svc, ())
            L["dates"].update(dts)
            departs = h < len(sts) - 1
            di = 1 if direction == "1" else 0
            if departs:
                L["heads"][di][headsign or stations[sts[-1]]["name"]] += 1
                hr = (hub_dep // 3600) % 24
                L["night"][0] += 1
                if hr < 5:
                    L["night"][1] += 1
                for k, o in ref.items():
                    if o in dts:
                        L["hourly"][k][di][hr] += 1
            if rt_id:
                L["rt"][":".join(rt_id.split(":")[:2])] += 1
        for sid, n in used_hub.most_common():
            s = stops[sid]
            hub_stops_meta.append({"feed": d["feed"], "stop_id": sid, "stop_code": s[4], "name": s[0],
                                   "platform": s[5], "lat": s[1], "lon": s[2], "ritten": n})

    for st in stations:
        if st["sum"]:
            st["lat"], st["lon"] = st["sum"][0] / st["n"], st["sum"][1] / st["n"]

    # ---- patronen kiezen: elk traject (paar opeenvolgende stations) minstens een keer
    for L in lines.values():
        covered, chosen = set(), []
        for pat, cnt in sorted(L["pat"].items(), key=lambda kv: (-kv[1], -len(kv[0]))):
            edges = {frozenset(e) for e in zip(pat, pat[1:])}
            if edges - covered:
                covered |= edges
                chosen.append(pat)
        L["chosen"] = chosen
    for d in data:
        wanted = set()
        for L in lines.values():
            if L["d"] is d:
                for pat in L["chosen"]:
                    sh = L["pat_shape"][pat].most_common(1)[0][0]
                    if sh:
                        wanted.add(sh)
        log("[%s] shapes lezen (%d nodig)" % (d["feed"], len(wanted)))
        d["shapes"] = read_shapes(d["zf"], wanted)

    tol = a.tolerance / 111320.0
    os.makedirs(a.cache, exist_ok=True)
    _road_state["path"] = os.path.join(a.cache, "wegroutes.json")
    if os.path.exists(_road_state["path"]):
        try:
            _road_cache.update(json.load(open(_road_state["path"])))
        except ValueError:
            pass
    n_road = n_dubbel = 0
    osm_idx = None
    if not a.no_osm:
        oj = fetch_osm(a.cache, int(a.radius + 50), 24 * 7)
        if oj:
            osm_idx = build_osm_index(oj)
    osm_stats, osm_miss = [0, 0], Counter()
    has_flix = any(d["feed"] == "flix" for d in data)
    used_stations = set()
    feats = []
    for lid, L in lines.items():
        r = L["route"]
        mode = mode_of(r.get("route_type"))
        short = r.get("route_short_name", "") or ""
        longn = r.get("route_long_name", "") or ""
        night_share = L["night"][1] / L["night"][0] if L["night"][0] else 0
        is_night = mode == "bus" and (night_share >= 0.6 or re.match(r"^N\s?\d", short) is not None)
        agency = L["d"]["agencies"].get(r.get("agency_id", ""), r.get("agency_id", "")) or ""
        coach = re.search(r"flix|blablacar|eurolines|regiojet", agency, re.I) is not None
        if L["feed"] == "nl" and re.search(r"flix", agency, re.I) and has_flix:
            n_dubbel += 1          # staat ook (vollediger) in de Flixbus-feed
            continue
        if mode == "bus" and (L["feed"] != "nl" or coach):
            cat = "langeafstand"
        elif is_night:
            cat = "nachtbus"
        else:
            cat = mode
        weeks = len({date.fromordinal(o).isocalendar()[:2] for o in L["dates"]})
        seasonal = weeks / L["d"]["n_weeks"] < 0.35 and not is_night and cat != "langeafstand"
        geoms, pats = [], []
        for pat in L["chosen"]:
            sh = L["pat_shape"][pat].most_common(1)[0][0]
            pts = L["d"]["shapes"].get(sh) if sh else None
            from_shape = bool(pts) and len(pts) > 1
            if not from_shape:
                pts = [(stations[s]["lon"], stations[s]["lat"]) for s in pat]
            vorm = "shape" if from_shape else "haltes"
            if osm_idx is not None and cat in ("tram", "metro", "bus", "nachtbus"):
                osm_stats[1] += 1
                og = osm_geometry(osm_idx, "bus" if mode == "bus" else "rail", short,
                                  [(stations[s]["lon"], stations[s]["lat"]) for s in pat], [s == 0 for s in pat])
                if og:
                    pts, vorm = og, "osm"
                    osm_stats[0] += 1
                else:
                    osm_miss[short] += 1
            # langeafstandsbussen hebben vaak alleen rechte lijnen tussen haltes: leg ze over de weg
            gap = max((dist_m(y1, x1, y2, x2) for (x1, y1), (x2, y2) in zip(pts, pts[1:])), default=0)
            straight = (not from_shape) or len(pts) <= 3 * len(pat) or gap > 4000
            if cat == "langeafstand" and not a.no_roads and straight and len(pat) > 1:
                if n_road == 0:
                    log("langeafstandsroutes over de weg leggen (ongeveer een seconde per stuk)")
                rr = road_route([(stations[s]["lon"], stations[s]["lat"]) for s in pat], a.osrm)
                n_road += 1
                if rr and len(rr) > 1:
                    pts, vorm = rr, "weg"
            pts = simplify(pts, tol * 3 if cat == "langeafstand" else tol * 0.35 if vorm == "osm" else tol)
            geoms.append([[round(x, 5), round(y, 5)] for x, y in pts])
            pats.append({"kop": L["pat_head"][pat].most_common(1)[0][0], "ritten": L["pat"][pat],
                         "haltes": ["s%d" % s for s in pat], "vorm": vorm})
            used_stations.update(pat)
        times = {}
        for s in set(L["tf"]) | set(L["tto"]):
            f = round(median(L["tf"][s]) / 60) if L["tf"].get(s) else None
            t = round(median(L["tto"][s]) / 60) if L["tto"].get(s) else None
            times["s%d" % s] = [f, t]
        freq = {}
        for k in ref:
            h0, h1 = L["hourly"][k]
            tot = [x + y for x, y in zip(h0, h1)]
            per_dir = max(sum(h0[7:19]), sum(h1[7:19])) / 12.0
            if cat == "nachtbus":
                per_dir = max(sum(h0[0:5]), sum(h1[0:5])) / 5.0
            freq[k] = {"perUur": round(per_dir, 1), "uren": tot, "ritten": sum(tot)}
        heads = []
        for c in L["heads"]:
            if c:
                heads.append(c.most_common(1)[0][0])
        color = (r.get("route_color") or "").strip().lstrip("#")
        feats.append({
            "type": "Feature",
            "geometry": {"type": "MultiLineString", "coordinates": geoms},
            "properties": {
                "id": lid, "feed": L["feed"], "vervoerder": L["d"]["agencies"].get(r.get("agency_id", ""), r.get("agency_id", "")),
                "nummer": short, "naam": longn, "cat": cat, "modaliteit": mode, "route_type": r.get("route_type", ""),
                "kleur": ("#" + color) if re.fullmatch(r"[0-9A-Fa-f]{6}", color) else None,
                "richtingen": heads, "freq": freq, "seizoen": seasonal, "weken": weeks, "rittenTotaal": L["ntrips"],
                "patronen": pats, "reistijd": times,
                "live": {"route_id": r.get("route_id", ""), "kv78": L["rt"].most_common(1)[0][0] if L["rt"] else None},
            },
        })

    cat_order = {"trein": 0, "metro": 1, "tram": 2, "bus": 3, "nachtbus": 4, "langeafstand": 5, "veer": 6}

    def sortkey(f):
        p = f["properties"]
        m = re.search(r"\d+", p["nummer"])
        return (cat_order.get(p["cat"], 9), int(m.group()) if m else 9999, p["nummer"], p["naam"])

    feats.sort(key=sortkey)
    stop_feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(stations[s]["lon"], 5), round(stations[s]["lat"], 5)]},
                   "properties": {"id": "s%d" % s, "naam": stations[s]["name"], "knooppunt": s == 0}}
                  for s in sorted(used_stations | {0})]
    meta = {
        "gemaakt": datetime.now().isoformat(timespec="seconds"),
        "knooppunt": {"naam": "Den Haag Centraal", "lat": HUB_LAT, "lon": HUB_LON, "straal_m": a.radius, "haltes": hub_stops_meta},
        "peildagen": {k: (date.fromordinal(v).isoformat() if v else None) for k, v in ref.items()},
        "feedperiode": [date.fromordinal(feed_start).isoformat(), date.fromordinal(feed_end).isoformat()],
        "bronnen": [{"feed": d["feed"], "bestand": os.path.basename(d["zf"].filename or ""),
                     "versie": d["info"].get("feed_version", ""), "uitgever": d["info"].get("feed_publisher_name", "")} for d in data],
        "aantallen": {"lijnen": len(feats), "haltes": len(stop_feats), "per_categorie": dict(Counter(f["properties"]["cat"] for f in feats))},
    }
    lines_fc = {"type": "FeatureCollection", "features": feats}
    stops_fc = {"type": "FeatureCollection", "features": stop_feats}
    os.makedirs(a.out, exist_ok=True)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    for name, obj in (("lines.geojson", lines_fc), ("stops.geojson", stops_fc), ("meta.json", meta)):
        with open(os.path.join(a.out, name), "w", encoding="utf-8") as f:
            f.write(dump(obj))
    with open(os.path.join(a.out, "ov-data.js"), "w", encoding="utf-8") as f:
        f.write("window.OV_DATA=" + dump({"meta": meta, "lines": lines_fc, "stops": stops_fc}) + ";\n")
    if n_dubbel:
        log("%d Flixbus-lijnen uit de Nederlandse feed overgeslagen (zitten al in de Flixbus-feed)" % n_dubbel)
    if osm_idx is not None:
        log("echte routes uit OpenStreetMap: %d van %d routevarianten" % tuple(osm_stats))
        if osm_miss:
            log("   niet gevonden (lijn uit dienstregeling gebruikt): " + ", ".join("%s (%dx)" % kv for kv in sorted(osm_miss.items())))
    if n_road:
        log("%d langeafstandsroutes aangeboden aan de routeserver" % n_road)
    # ritplannen (alleen voor het tussenstation; de kaart laadt dit niet)
    kept = {f["properties"]["id"] for f in feats}
    plan = {
        "gemaakt": meta["gemaakt"],
        "patronen": [["s%d" % x for x in ptn] for ptn, _ in sorted(plan_pat.items(), key=lambda kv: kv[1])],
        "tijden": [list(t) for t, _ in sorted(plan_tim.items(), key=lambda kv: kv[1])],
        "diensten": [[date.fromordinal(o).strftime("%Y%m%d") for o in days] for _, days in sorted(plan_svc.values())],
        "ritten": [t for t in plan_trips if t[0] in kept],
    }
    with open(os.path.join(a.out, "trips.json"), "w", encoding="utf-8") as f:
        f.write(dump(plan))
    log("ritplannen: %d ritten, %d haltepatronen, %d tijdprofielen (trips.json, %.1f MB)"
        % (len(plan["ritten"]), len(plan["patronen"]), len(plan["tijden"]), os.path.getsize(os.path.join(a.out, "trips.json")) / 1e6))
    log("klaar: %d lijnen, %d haltes -> %s/" % (len(feats), len(stop_feats), a.out))
    for c, n in sorted(meta["aantallen"]["per_categorie"].items(), key=lambda kv: cat_order.get(kv[0], 9)):
        log("   %-13s %d" % (c, n))
    log("ov-data.js is %.1f MB" % (os.path.getsize(os.path.join(a.out, "ov-data.js")) / 1e6))


if __name__ == "__main__":
    main()
