"""Step 3: test the backend.
  python test_backend.py            -> live test against the running server (needs API key)
  python test_backend.py offline    -> no Google call; checks the scoring with fake routes
"""
import sys


def encode(points):
    out, plat, plng = [], 0, 0
    for lat, lng in points:
        for v, p in ((round(lat * 1e5), plat), (round(lng * 1e5), plng)):
            d = v - p
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plng = round(lat * 1e5), round(lng * 1e5)
    return "".join(out)


def fake_route(pts, secs):
    mid = len(pts) // 2
    step = lambda p, t: {"polyline": {"encodedPolyline": encode(p)}, "navigationInstruction": {"instructions": t}}
    return {"duration": f"{secs}s", "distanceMeters": secs * 10, "polyline": {"encodedPolyline": encode(pts)},
            "legs": [{"steps": [step(pts[:mid + 1], "Head east"), step(pts[mid:], "Turn right")]}]}


def check(body):
    for key in ("fastest_route", "safest_route", "stats"):
        assert key in body, f"missing {key}"
    f, s = body["fastest_route"], body["safest_route"]
    print(f"fastest: {f['duration_s']//60} min, score {f['score']}, {f['stats']}")
    print(f"safest : {s['duration_s']//60} min, score {s['score']}, {s['stats']}")
    assert s["score"] <= f["score"]


if sys.argv[1:] == ["offline"]:
    import app as backend
    A = [(28.6280, 77.2090), (28.6305, 77.2145), (28.6330, 77.2200)]   # short, passes many incidents
    B = [(28.6280, 77.2090), (28.6250, 77.2140), (28.6330, 77.2200)]   # detour, quieter
    backend.fetch_routes = lambda s, e, m: [fake_route(A, 600), fake_route(B, 780)]
    res = backend.app.test_client().get("/getRoutes?start=a&end=b")
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    check(body)
    assert "fastest_route" in body and "safest_route" in body
    assert body["safest_route"]["score"] <= body["fastest_route"]["score"]
    print("offline test passed")

else:
    import requests
    r = requests.get("http://localhost:5000/getRoutes",
                     params={"start": "Connaught Place, New Delhi", "end": "India Gate, New Delhi"}, timeout=30)
    print(r.status_code)
    r.raise_for_status()
    check(r.json())
    print("live test passed")