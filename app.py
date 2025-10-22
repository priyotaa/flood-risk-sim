# app.py
from flask import Flask, render_template, jsonify, request
import requests, datetime, statistics, threading

app = Flask(__name__)

# --------------------------
# CONFIG: Stations & venues
# --------------------------
STATIONS = [
    # site_id, name, lat, lon, city
    {"site":"01103500","name":"Charles River at Dover","lat":42.256209,"lon":-71.260055,"city":"Dover"},
    {"site":"01104500","name":"Charles River at Waltham","lat":42.372319,"lon":-71.233667,"city":"Waltham"},  # USGS page
    {"site":"01104705","name":"Charles River at First St (Cambridge)","lat":42.362222,"lon":-71.078611,"city":"Cambridge"}, # USGS
    {"site":"01104710","name":"Charles River @ Museum of Science (Boston)","lat":42.365931,"lon":-71.070051,"city":"Boston"}, # USGS
    {"site":"01104715","name":"Charles River at New Charles River Dam (Boston)","lat":42.368889,"lon":-71.061667,"city":"Boston"} # USGS
]

# Venues we care about (Community Boating, MIT Sailing Pavilion, etc.)
VENUES = [
    {"id":"cbi","name":"Community Boating, Inc. (CBI)","lat":42.3599,"lon":-71.0731,"address":"21 David G Mugar Way, Boston, MA"}, # CBI site
    {"id":"mit_sailing","name":"MIT Sailing Pavilion","lat":42.3573,"lon":-71.0953,"address":"134 Memorial Dr, Cambridge, MA"} # approximate
]

# subscriptions stored in-memory (demo): {venue_id: [{"name":..., "email":...}, ...]}
SUBSCRIPTIONS = {v['id']: [] for v in VENUES}

# USGS water services endpoints
USGS_IV_API = "https://waterservices.usgs.gov/nwis/iv/?format=json&sites={site}&parameterCd=00065"
USGS_DV_API = "https://waterservices.usgs.gov/nwis/dv/?format=json&sites={site}&parameterCd=00065&startDT={start}&endDT={end}"

# --------------------------
# Helpers: fetch current and 30-day daily values
# --------------------------
def fetch_current_gage(site):
    url = USGS_IV_API.format(site=site)
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    j = r.json().get("value", {})
    g = None
    t = None
    for series in j.get("timeSeries", []):
        var = series.get("variable", {}).get("variableCode", [])
        code = var[0].get("value") if var else None
        if code == "00065":
            vals = series.get("values", [])
            if vals and vals[0].get("value"):
                last = vals[0]["value"][-1]
                try:
                    g = float(last.get("value"))
                    t = last.get("dateTime")
                except:
                    g = None
    return g, t

def fetch_30d_daily(site, days=30):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = USGS_DV_API.format(site=site, start=start.isoformat(), end=end.isoformat())
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    j = r.json()

    history = []

    try:
        time_series = j.get("value", {}).get("timeSeries", [])
        for series in time_series:
            # Only consider discharge variable 00065
            code = series.get("variable", {}).get("variableCode", [{}])[0].get("value")
            if code != "00065":
                continue
            values_blocks = series.get("values", [])
            for vb in values_blocks:
                entries = vb.get("value", [])
                for entry in entries:
                    val = entry.get("value")
                    date = entry.get("dateTime", "").split("T")[0]
                    if val not in (None, ""):
                        history.append({"date": date, "value": float(val)})
    except Exception as e:
        print("fetch_30d_daily error:", e)

    return history


def risk_from_median(current, median):
    # Tunable rule: low <= median + 0.5 ft, medium <= median + 1.5 ft, high > median + 1.5 ft
    if current is None or median is None:
        return "unknown"
    if current <= median + 0.5:
        return "low"
    if current <= median + 1.5:
        return "medium"
    return "high"

# --------------------------
# API endpoints
# --------------------------
@app.route("/api/stations")
def api_stations():
    out = []
    for s in STATIONS:
        try:
            cur, t = fetch_current_gage(s["site"])
        except Exception as e:
            cur, t = None, None
        try:
            hist = fetch_30d_daily(s["site"])
            median = statistics.median([h['value'] for h in hist]) if hist else None
        except Exception as e:
            hist, median = [], None
        risk = risk_from_median(cur, median)
        out.append({
            "site": s["site"],
            "name": s["name"],
            "city": s["city"],
            "lat": s["lat"],
            "lon": s["lon"],
            "gage_height_ft": cur,
            "time": t,
            "median_30d": median,
            "history_30d": hist,
            "risk": risk
        })
    return jsonify(out)

@app.route("/api/venues")
def api_venues():
    return jsonify(VENUES)

@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    data = request.json or {}
    vid = data.get("venue_id")
    person = {"name": data.get("name"), "email": data.get("email")}
    if not vid or not person["email"]:
        return jsonify({"error":"venue_id and email required"}), 400
    if vid not in SUBSCRIPTIONS:
        return jsonify({"error":"unknown venue"}), 404
    SUBSCRIPTIONS[vid].append(person)
    return jsonify({"status":"subscribed","venue":vid})

# Simple endpoint to view subscriptions (dev)
@app.route("/api/subscriptions")
def api_subs():
    return jsonify(SUBSCRIPTIONS)

@app.route("/")
def index():
    return render_template("index.html")


def notifier_loop():
    import time, smtplib
    while True:
        try:
            stations = requests.get("http://127.0.0.1:5000/api/stations", timeout=20).json()
            # If any station risk is high -> notify venue subscribers if within n km (simple distance)
            def dist_km(lat1, lon1, lat2, lon2):
                from math import radians, sin, cos, acos
                r=6371
                return acos(sin(radians(lat1))*sin(radians(lat2))+cos(radians(lat1))*cos(radians(lat2))*cos(radians(lon2-lon1))) * r
            for v in VENUES:
                # find nearest station
                nearest = min(stations, key=lambda s: dist_km(s["lat"], s["lon"], v["lat"], v["lon"]))
                # notify if nearest risk is medium/high
                if nearest["risk"] in ("medium","high"):
                    # Demo: print notifications
                    subs = SUBSCRIPTIONS.get(v["id"], [])
                    if subs:
                        print(f"[NOTIFY] Venue {v['name']} risk={nearest['risk']}. Notifying {len(subs)} subscribers.")
                        # real email logic could go here (commented below)
        except Exception as e:
            print("Notifier error:", e)
        time.sleep(60)



if __name__ == "__main__":
    print("Server running at http://127.0.0.1:5000")
    app.run(debug=True)
