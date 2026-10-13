"""SafeRoute backend: Google Routes API + incident scoring."""
import csv, math, os
from collections import defaultdict
from datetime import date, timedelta

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()
API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
POLICE_NUMBER = os.getenv("POLICE_NUMBER", "112")
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

RADIUS_M = 150          # an incident counts if it is within this distance of the route
MAX_AGE_DAYS = 365      # ignore older incidents
WEIGHTS = {"accident": 1, "harassment": 3, "kidnap": 8, "teasing": 1}  # tune these
KEYS = {"accident": "accidents", "harassment": "harassments", "kidnap": "kidnaps", "teasing": "teasing"}
CELL = 0.003            # grid size in degrees (~330 m) for fast lookups
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "incidents.csv")

app = Flask(__name__, static_folder="static", static_url_path="")
# ---------------------------------------------------------------- incident data
GRID = defaultdict(list)
_loaded_mtime = None


def cell(lat, lng):
    return (int(lat // CELL), int(lng // CELL))


def load_incidents():
    """Load the CSV into a grid index. Re-runs automatically when the file changes,
    so a nightly job can simply overwrite incidents.csv."""
    global _loaded_mtime
    mtime = os.path.getmtime(CSV_PATH)
    if mtime == _loaded_mtime:
        return
    GRID.clear()
    cutoff = date.today() - timedelta(days=MAX_AGE_DAYS)
    with open(CSV_PATH, newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            kind = row["type"].strip().lower()
            if kind not in WEIGHTS:
                continue
            try:
                if date.fromisoformat(row["date"]) < cutoff:
                    continue
            except (KeyError, ValueError):
                pass  # no/invalid date: keep the row
            inc = {"id": i, "lat": float(row["lat"]), "lng": float(row["lng"]), "type": kind}
            GRID[cell(inc["lat"], inc["lng"])].append(inc)
    _loaded_mtime = mtime
    # ---------------------------------------------------------------- geometry
def haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(a))


def decode_polyline(s):
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(s):
        for axis in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else result >> 1
            if axis == 0:
                lat += d
            else:
                lng += d
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def densify(points, max_m=50):
    """Insert points so no gap exceeds max_m (long straight roads have few vertices)."""
    out = []
    for a, b in zip(points, points[1:]):
        n = max(1, math.ceil(haversine(*a, *b) / max_m))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n)]
    return out + points[-1:]


def incidents_near(points):
    found = {}
    for lat, lng in densify(points):
        cy, cx = cell(lat, lng)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for inc in GRID.get((cy + dy, cx + dx), ()):
                    if inc["id"] not in found and haversine(lat, lng, inc["lat"], inc["lng"]) <= RADIUS_M:
                        found[inc["id"]] = inc
    return found
# ---------------------------------------------------------------- scoring
def count(incs):
    c = {v: 0 for v in KEYS.values()}
    for inc in incs:
        c[KEYS[inc["type"]]] += 1
    return c


def risk(incs):
    return sum(WEIGHTS[i["type"]] for i in incs)


def summarize(route):
    """Turn one Google route into our JSON shape, scoring each step (road segment)."""
    seen, steps = {}, []
    for leg in route.get("legs", []):
        for st in leg.get("steps", []):
            pts = decode_polyline(st["polyline"]["encodedPolyline"])
            new = {k: v for k, v in incidents_near(pts).items() if k not in seen}  # count each incident once
            seen.update(new)
            steps.append({
                "instruction": st.get("navigationInstruction", {}).get("instructions", "Continue"),
                "stats": count(new.values()), "score": risk(new.values()),
            })
    return {
        "duration_s": int(float(route["duration"].rstrip("s"))),
        "distance_m": route.get("distanceMeters", 0),
        "path": decode_polyline(route["polyline"]["encodedPolyline"]),
        "score": risk(seen.values()),
        "stats": count(seen.values()),
        "steps": steps,
    }
# ---------------------------------------------------------------- Google Routes API
class RouteError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def fetch_routes(start, end, mode="DRIVE"):
    if not API_KEY:
        raise RouteError("GOOGLE_MAPS_API_KEY is not set", 500)
    body = {"origin": {"address": start}, "destination": {"address": end},
            "travelMode": mode, "computeAlternativeRoutes": True}
    if mode == "DRIVE":
        body["routingPreference"] = "TRAFFIC_AWARE"
    headers = {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,"
                            "routes.legs.steps.polyline.encodedPolyline,routes.legs.steps.navigationInstruction.instructions",
    }
    r = requests.post(ROUTES_URL, json=body, headers=headers, timeout=15)
    if not r.ok:
        raise RouteError(f"Google Routes API error {r.status_code}: {r.text[:300]}")
    return r.json().get("routes", [])


# ---------------------------------------------------------------- API
@app.route("/getRoutes", methods=["GET", "POST"])
def get_routes():
    data = request.get_json(silent=True) or request.args
    start, end = (data.get("start") or "").strip(), (data.get("end") or "").strip()
    mode = data.get("mode", "DRIVE").upper()
    if not start or not end:
        return jsonify(error="Both 'start' and 'end' are required"), 400
    if mode not in ("DRIVE", "WALK"):
        return jsonify(error="mode must be DRIVE or WALK"), 400
    try:
        load_incidents()
        routes = [summarize(r) for r in fetch_routes(start, end, mode)]
    except RouteError as e:
        return jsonify(error=str(e)), e.status
    if not routes:
        return jsonify(error="No route found between those locations"), 404

    fastest = min(routes, key=lambda r: r["duration_s"])
    safest = min(routes, key=lambda r: (r["score"], r["duration_s"]))
    return jsonify({
        "fastest_route": fastest,
        "safest_route": safest,
        "stats": {"fastest_route": fastest["stats"], "safest_route": safest["stats"]},
        "same_route": fastest is safest,
        "routes_considered": len(routes),
        "police_number": POLICE_NUMBER,
    })


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))