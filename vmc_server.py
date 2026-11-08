#!/usr/bin/env python3



import asyncio

import csv
import json
import tempfile

import os

import struct

import threading
import time

from datetime import datetime, timedelta



from bleak import BleakScanner

from flask import Flask, jsonify, render_template_string, request





# ============================================================

# Configuration

# ============================================================



COMPANY_ID = 0x0059

# Persist beside the script by default, independent of the launch directory.
# Deployments may point at an existing data directory with VMC_DATA_DIR.
_data_dir_override = os.environ.get("VMC_DATA_DIR", "").strip()
DATA_DIR = os.path.expanduser(_data_dir_override) if _data_dir_override else "vmc_data"
if not os.path.isabs(DATA_DIR):
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_DIR)
DATA_DIR = os.path.abspath(DATA_DIR)



SENSORS = {

    1: {

        "name": "salon",

        "color": "#ff5a5f",

        "file": "sensor1.csv",

    },

    2: {

        "name": "Parents",

        "color": "#35d07f",

        "file": "sensor2.csv",

    },

    3: {

        "name": "buanderie",

        "color": "#5da9ff",

        "file": "sensor3.csv",

    },

    4: {

        "name": "Outside",

        "color": "#f5b942",

        "file": "sensor4.csv",

    },

    5: {

        "name": "Sensor 5",

        "color": "#b38cff",

        "file": "sensor5.csv",

    },

    6: {

        "name": "Sensor 6",

        "color": "#36c9c6",

        "file": "sensor6.csv",

    },

}





# ============================================================

# Shared data

# ============================================================



latest_data = {}



data_lock = threading.Lock()
csv_lock = threading.Lock()
scanner_lock = threading.Lock()
history_cache_lock = threading.Lock()
history_cache = {}

scanner_state = {
    "running": False,
    "status": "starting",
    "restart_count": 0,
    "last_error": None,
    "last_started": None,
}





# ============================================================

# CSV storage

# ============================================================



CSV_HEADER = [

    "timestamp",

    "sequence",

    "temperature_c",

    "humidity_percent",

    "battery_mv",

    "rssi",

]





def ensure_data_directory():

    os.makedirs(DATA_DIR, exist_ok=True)



    for sensor in SENSORS.values():

        path = os.path.join(DATA_DIR, sensor["file"])



        if not os.path.exists(path):

            with open(path, "w", newline="") as f:

                writer = csv.writer(f)

                writer.writerow(CSV_HEADER)





def write_csv(sensor_id, packet):

    sensor = SENSORS[sensor_id]

    path = os.path.join(DATA_DIR, sensor["file"])



    row = [

        packet["timestamp"],

        packet["sequence"],

        f"{packet['temperature']:.2f}",

        f"{packet['humidity']:.2f}",

        packet["battery_mv"],

        packet["rssi"],

    ]



    with csv_lock:

        with open(path, "a", newline="") as f:

            writer = csv.writer(f)

            writer.writerow(row)





# ============================================================

# LiPo battery estimate

#

# Approximate 1S LiPo state of charge from voltage.

# This is deliberately shown as an estimate.

# ============================================================



LIPO_CURVE = [

    (3.30, 0),

    (3.46, 10),

    (3.58, 20),

    (3.63, 30),

    (3.66, 50),

    (3.84, 70),

    (3.94, 80),

    (4.05, 90),

    (4.17, 100),

    (4.20, 100),

]





def battery_percent(voltage):

    if voltage <= LIPO_CURVE[0][0]:

        return 0



    if voltage >= LIPO_CURVE[-1][0]:

        return 100



    for i in range(len(LIPO_CURVE) - 1):

        v1, p1 = LIPO_CURVE[i]

        v2, p2 = LIPO_CURVE[i + 1]



        if v1 <= voltage <= v2:

            fraction = (voltage - v1) / (v2 - v1)

            percent = p1 + fraction * (p2 - p1)



            return max(

                0,

                min(100, int(round(percent))),

            )



    return 0





# ============================================================

# Historical data

# ============================================================



class HistoryAggregator:
    """Keep recent detail and stream older readings into UTC-aligned averages."""

    def __init__(self, now):
        self.now_ms = now.timestamp() * 1000
        self.recent = []
        self.buckets = {}

    def add(self, point):
        age_ms = self.now_ms - point["timestamp_ms"]
        if age_ms <= 86400000:
            self.recent.append(point)
            return
        period_ms = 3600000 if age_ms <= 30 * 86400000 else 86400000
        self.add_bucket(point, period_ms, point["temperature"], point["humidity"], 1)

    def add_bucket(self, point, period_ms, temperature_sum, humidity_sum, count):
        key = (period_ms, int(point["timestamp_ms"] // period_ms))
        bucket = self.buckets.get(key)
        if bucket is None:
            bucket = {"latest": point, "temperature": 0.0,
                      "humidity": 0.0, "count": 0, "period_ms": period_ms}
            self.buckets[key] = bucket
        bucket["temperature"] += temperature_sum
        bucket["humidity"] += humidity_sum
        bucket["count"] += count
        if point["timestamp_ms"] > bucket["latest"]["timestamp_ms"]:
            bucket["latest"] = point

    def advance(self, now):
        """Age summaries without rereading source CSVs; retain unrounded sums."""
        self.now_ms = now.timestamp() * 1000
        recent, self.recent = self.recent, []
        changed = False
        for point in recent:
            changed |= self.now_ms - point["timestamp_ms"] > 86400000
            self.add(point)
        for key, bucket in list(self.buckets.items()):
            if key[0] == 3600000 and self.now_ms - bucket["latest"]["timestamp_ms"] > 30 * 86400000:
                del self.buckets[key]
                self.add_bucket(bucket["latest"], 86400000, bucket["temperature"],
                                bucket["humidity"], bucket["count"])
                changed = True
        return changed

    def points(self):
        points = list(self.recent)
        for bucket in self.buckets.values():
            points.append({
                **bucket["latest"],
                "temperature": round(bucket["temperature"] / bucket["count"], 2),
                "humidity": round(bucket["humidity"] / bucket["count"], 2),
                "sample_count": bucket["count"],
                "period_seconds": bucket["period_ms"] // 1000,
            })
        return points


def history_point(row):
    timestamp = datetime.fromisoformat(row["timestamp"])
    return {
        "timestamp": row["timestamp"], "timestamp_ms": timestamp.timestamp() * 1000,
        "sequence": int(row["sequence"]), "temperature": float(row["temperature_c"]),
        "humidity": float(row["humidity_percent"]), "battery_mv": int(row["battery_mv"]),
        "rssi": int(row["rssi"]),
    }


def file_signature(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def save_history_cache(path, entry, source_mode):
    aggregator = entry["aggregator"]
    payload = {key: value for key, value in entry.items() if key != "aggregator"}
    payload.update(version=1, recent=aggregator.recent, buckets=list(aggregator.buckets.values()))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=DATA_DIR, prefix=".history-cache-", delete=False) as output:
            temporary = output.name
            # Match source CSV access, including launches under different accounts.
            os.fchmod(output.fileno(), source_mode & 0o666)
            json.dump(payload, output, separators=(",", ":"))
        os.replace(temporary, path)
    except OSError as error:
        print(f"Could not persist history cache {path}: {error}")
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_history_cache(path, now):
    try:
        with open(path) as source:
            payload = json.load(source)
        if payload["version"] != 1:
            return None
        aggregator = HistoryAggregator(now)
        aggregator.recent = payload.pop("recent")
        for bucket in payload.pop("buckets"):
            point = bucket["latest"]
            aggregator.buckets[(bucket["period_ms"], int(point["timestamp_ms"] // bucket["period_ms"]))] = bucket
        # Validate essential metadata before accepting a rebuildable sidecar.
        assert len(payload["signature"]) == 4 and payload["offset"] >= 0
        payload["aggregator"] = aggregator
        return payload
    except (OSError, ValueError, KeyError, TypeError, AssertionError, ZeroDivisionError):
        return None


def cached_sensor_history(sensor_id, sensor, now):
    """Incrementally summarize append-only CSVs; replacement/truncation rebuilds."""
    path = os.path.join(DATA_DIR, sensor["file"])
    cache_path = os.path.join(DATA_DIR, f".history-cache-{sensor_id}.json")
    entry = history_cache.get(path)
    if entry is None:
        entry = load_history_cache(cache_path, now)
    # The caller owns history_cache_lock. Keep lock ordering cache -> CSV.
    with csv_lock:
        if not os.path.exists(path):
            history_cache.pop(path, None)
            return {"aggregator": HistoryAggregator(now), "latest": None, "last_full_charge": None}
        with open(path, "rb") as source:
            source_stat = os.fstat(source.fileno())
            source_mode = source_stat.st_mode
            signature = file_signature(source_stat)
            previous = entry["signature"] if entry else None
            # Same inode with growth is the supported append path. Same-size edits,
            # truncation, and atomic replacement all invalidate derived summaries.
            if previous is not None and (previous[:2] != signature[:2]
                    or signature[2] < previous[2]
                    or (signature[2] == previous[2] and signature[3] != previous[3])):
                entry = None
            if entry is None:
                entry = dict(aggregator=HistoryAggregator(now), signature=signature,
                             offset=0, latest=None, last_full_charge=None)
                dirty = True
            else:
                dirty = entry["aggregator"].advance(now)
            source.seek(entry["offset"])
            if entry["offset"] == 0:
                header = source.readline()
                if next(csv.reader([header.decode("utf-8")]), []) != CSV_HEADER:
                    raise ValueError(f"Unexpected CSV header in {path}")
                entry["offset"] = source.tell()
            while True:
                line = source.readline()
                if not line or not line.endswith(b"\n"):
                    break  # Retry a partially appended row on the next request.
                entry["offset"] = source.tell()
                dirty = True
                try:
                    row = next(csv.DictReader([line.decode("utf-8")], fieldnames=CSV_HEADER))
                    point = history_point(row)
                except (ValueError, KeyError, TypeError, UnicodeError, csv.Error):
                    continue
                entry["aggregator"].add(point)
                if entry["latest"] is None or point["timestamp_ms"] >= entry["latest"]["timestamp_ms"]:
                    entry["latest"] = point
                if battery_percent(point["battery_mv"] / 1000.0) == 100:
                    full = entry["last_full_charge"]
                    if full is None or point["timestamp_ms"] > datetime.fromisoformat(full).timestamp() * 1000:
                        entry["last_full_charge"] = point["timestamp"]
            entry["signature"] = signature
    history_cache[path] = entry
    if dirty:
        save_history_cache(cache_path, entry, source_mode)
    return entry


def read_cached_history(hours=None):
    now = datetime.now().astimezone()
    cutoff = -float("inf") if hours is None else (now - timedelta(hours=hours)).timestamp() * 1000
    result = {}
    with history_cache_lock:
        for sensor_id, sensor in SENSORS.items():
            entry = cached_sensor_history(sensor_id, sensor, now)
            points = [point for point in entry["aggregator"].points() if point["timestamp_ms"] >= cutoff]
            points.sort(key=lambda point: point["timestamp_ms"])
            result[str(sensor_id)] = dict(name=sensor["name"], color=sensor["color"], points=points)
    return result


def read_history(hours=None, aggregate=False):
    if aggregate:
        return read_cached_history(hours)

    result = {}



    now = datetime.now().astimezone()
    cutoff = None



    if hours is not None:

        cutoff = (

            now - timedelta(hours=hours)

        )



    with csv_lock:

        for sensor_id, sensor in SENSORS.items():

            path = os.path.join(

                DATA_DIR,

                sensor["file"],

            )



            points = []



            if os.path.exists(path):

                try:

                    with open(path, "r", newline="") as f:

                        reader = csv.DictReader(f)



                        for row in reader:

                            try:

                                timestamp = datetime.fromisoformat(

                                    row["timestamp"]

                                )



                                if (

                                    cutoff is not None

                                    and timestamp < cutoff

                                ):

                                    continue



                                point = {

                                    "timestamp":

                                        row["timestamp"],



                                    "timestamp_ms":

                                        timestamp.timestamp() * 1000.0,



                                    "sequence":

                                        int(row["sequence"]),



                                    "temperature":

                                        float(row["temperature_c"]),



                                    "humidity":

                                        float(row["humidity_percent"]),



                                    "battery_mv":

                                        int(row["battery_mv"]),



                                    "rssi":

                                        int(row["rssi"]),

                                }
                                points.append(point)



                            except (

                                ValueError,

                                KeyError,

                                TypeError,

                            ):

                                continue



                except OSError as error:

                    print(

                        f"Could not read {path}: {error}"

                    )



            points.sort(

                key=lambda p: p["timestamp_ms"]

            )



            result[str(sensor_id)] = {

                "name": sensor["name"],

                "color": sensor["color"],

                "points": points,

            }



    return result





# ============================================================

# BLE packet parser

#

# 12 bytes:

#

# byte 0      protocol

# byte 1      sensor ID

# byte 2-5    sequence uint32 LE

# byte 6-7    temperature x100 int16 LE

# byte 8-9    humidity x100 uint16 LE

# byte 10-11  battery mV uint16 LE

# ============================================================



def parse_sensor_packet(data):

    if len(data) != 12:

        return None



    (

        protocol,

        sensor_id,

        sequence,

        temperature_raw,

        humidity_raw,

        battery_mv,

    ) = struct.unpack("<BBIhHH", data)



    if protocol != 1:

        return None



    voltage = battery_mv / 1000.0



    return {

        "protocol": protocol,

        "sensor_id": sensor_id,

        "sequence": sequence,

        "temperature": temperature_raw / 100.0,

        "humidity": humidity_raw / 100.0,

        "battery_mv": battery_mv,

        "battery_voltage": voltage,

        "battery_percent": battery_percent(voltage),

    }





# ============================================================

# BLE callback

# ============================================================



def detection_callback(device, advertisement_data):

    payload = (

        advertisement_data

        .manufacturer_data

        .get(COMPANY_ID)

    )



    if payload is None:

        return



    packet = parse_sensor_packet(bytes(payload))



    if packet is None:

        return



    sensor_id = packet["sensor_id"]



    if sensor_id not in SENSORS:

        return



    sensor = SENSORS[sensor_id]



    with data_lock:

        previous = latest_data.get(sensor_id)



        # Ignore repeated advertisements for the same measurement.

        if (

            previous is not None

            and previous["sequence"] == packet["sequence"]

        ):

            return



        now = datetime.now().astimezone()



        packet["timestamp"] = now.isoformat(

            timespec="seconds"

        )



        packet["rssi"] = advertisement_data.rssi

        packet["name"] = sensor["name"]

        packet["color"] = sensor["color"]



        packet["last_full_charge"] = (
            packet["timestamp"] if packet["battery_percent"] == 100
            else previous.get("last_full_charge") if previous else None
        )
        latest_data[sensor_id] = packet



    write_csv(sensor_id, packet)



    print(

        f"{now.strftime('%H:%M:%S')}  "

        f"{sensor['name']:<8}  "

        f"seq={packet['sequence']:<6}  "

        f"T={packet['temperature']:.2f} C  "

        f"RH={packet['humidity']:.2f} %  "

        f"BAT={packet['battery_voltage']:.3f} V "

        f"(~{packet['battery_percent']}%)  "

        f"RSSI={packet['rssi']} dBm"

    )





# ============================================================

# BLE scanner

# ============================================================



async def ble_scanner_session():
    """Run one BLE scanning session and rebuild it after suspend/resume."""

    print()
    print("Starting BLE scanner...")

    scanner = BleakScanner(
        detection_callback=detection_callback
    )

    await scanner.start()

    with scanner_lock:
        scanner_state["running"] = True
        scanner_state["status"] = "running"
        scanner_state["last_error"] = None
        scanner_state["last_started"] = (
            datetime.now().astimezone().isoformat(timespec="seconds")
        )

    print("BLE scanner running.")

    last_tick = time.monotonic()

    try:
        while True:
            await asyncio.sleep(5)

            now_tick = time.monotonic()
            gap = now_tick - last_tick
            last_tick = now_tick

            # Normally this loop wakes every ~5 seconds. A much larger gap
            # means the machine probably slept or the process was stalled.
            # Rebuilding the Bleak scanner gives BlueZ a clean session.
            if gap > 30:
                raise RuntimeError(
                    f"system sleep/stall detected ({gap:.1f} s gap)"
                )

    finally:
        with scanner_lock:
            scanner_state["running"] = False
            scanner_state["status"] = "restarting"

        try:
            await scanner.stop()
        except Exception as error:
            print(f"BLE scanner stop warning: {error}")


async def ble_supervisor():
    """Keep BLE scanning alive for the lifetime of the web server."""

    while True:
        try:
            await ble_scanner_session()

        except Exception as error:
            message = f"{type(error).__name__}: {error}"

            with scanner_lock:
                scanner_state["running"] = False
                scanner_state["status"] = "restarting"
                scanner_state["restart_count"] += 1
                scanner_state["last_error"] = message

            print()
            print(f"BLE scanner problem: {message}")
            print("Restarting BLE scanner in 5 seconds...")
            print()

            await asyncio.sleep(5)


def run_ble():
    try:
        asyncio.run(ble_supervisor())
    except Exception as error:
        message = f"{type(error).__name__}: {error}"

        with scanner_lock:
            scanner_state["running"] = False
            scanner_state["status"] = "stopped"
            scanner_state["last_error"] = message

        print(f"BLE worker stopped unexpectedly: {message}")

# ============================================================

# Flask

# ============================================================



app = Flask(__name__)





# ============================================================

# Dashboard HTML

# ============================================================



PAGE = r"""
<!DOCTYPE html>

<html lang="en">

<head>

<meta charset="utf-8">

<meta

    name="viewport"

    content="width=device-width, initial-scale=1">

<meta

    name="theme-color"

    content="#090d14">

<title>VMC Monitor</title>

<script

    src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js">

</script>

<link rel="stylesheet" href="{{ url_for('static', filename='dashboard.css') }}">

</head>

<body>

<div class="dashboard">

    <div class="header">

        <div class="title-area">

            <div class="logo">

                <span class="logo-wave">⌁</span>

            </div>

            <div>

                <h1 class="title">

                    VMC Monitor

                </h1>

                <div class="subtitle">

                    Environmental sensors

                </div>

            </div>

        </div>

        <div class="history-controls" role="group" aria-label="History time range">
            <button type="button" data-hours="1" aria-pressed="false">1h</button>
            <button type="button" data-hours="12" aria-pressed="false">12h</button>
            <button type="button" data-hours="24" class="active" aria-pressed="true">1d</button>
            <button type="button" data-hours="168" aria-pressed="false">7d</button>
            <button type="button" data-hours="all" aria-pressed="false">All</button>
        </div>

    </div>

    <div

        id="sensor-grid"

        class="sensor-grid">

    </div>

    <div class="charts">

        <div class="chart-card">

            <div class="chart-heading">

                <div class="chart-title">

                    Temperature

                </div>

                <div class="chart-unit">

                    °C

                </div>

            </div>

            <div class="chart-container">

                <canvas

                    id="temperature-chart">

                </canvas>

            </div>

        </div>

        <div class="chart-card">

            <div class="chart-heading">

                <div class="chart-title">

                    Relative Humidity

                </div>

                <div class="chart-unit">

                    %

                </div>

            </div>

            <div class="chart-container">

                <canvas

                    id="humidity-chart">

                </canvas>

            </div>

        </div>

    </div>

    <div class="footer">

        <span class="status-dot"></span>

        <span id="status">

            Waiting for BLE advertisements

        </span>

    </div>

</div>

<dialog id="sensor-details" class="sensor-details" aria-labelledby="sensor-details-title">
    <div class="sensor-details-header">
        <div>
            <p id="sensor-details-id" class="sensor-details-id"></p>
            <h2 id="sensor-details-title"></h2>
        </div>
        <button id="sensor-details-close" type="button" aria-label="Close sensor details" autofocus>×</button>
    </div>
    <div id="sensor-details-body" class="sensor-details-body"></div>
</dialog>
<script src="{{ url_for('static', filename='charts.js') }}" defer></script>
<script src="{{ url_for('static', filename='dashboard.js') }}" defer></script>

</body>

</html>
"""





# ============================================================

# Flask routes

# ============================================================



@app.route("/")

def index():

    return render_template_string(PAGE)





@app.route("/api/sensors")

def api_sensors():

    with data_lock:

        result = {

            str(sensor_id): dict(data)

            for sensor_id, data

            in latest_data.items()

        }



    now = datetime.now().astimezone().timestamp()
    for data in result.values():
        last_full = data.get("last_full_charge")
        data["days_since_full_charge"] = (
            max(0, int((now - datetime.fromisoformat(last_full).timestamp()) // 86400))
            if last_full else None
        )
    return jsonify(result)





@app.route("/api/status")
def api_status():
    with scanner_lock:
        status = dict(scanner_state)

    return jsonify(status)


@app.route("/api/history")

def api_history():

    hours_argument = request.args.get("hours")



    hours = None



    if hours_argument is not None:

        try:

            hours = float(hours_argument)



            if hours <= 0:

                hours = None



        except ValueError:

            hours = None



    return jsonify(

        read_history(hours, aggregate=True)

    )





# ============================================================

# Main

# ============================================================



def restore_latest_data():
    """Restore exact latest measurements and charge dates from the derived cache."""
    restored = {}
    now = datetime.now().astimezone()
    with history_cache_lock:
        for sensor_id, sensor in SENSORS.items():
            entry = cached_sensor_history(sensor_id, sensor, now)
            if entry["latest"] is None:
                continue
            packet = dict(entry["latest"])
            voltage = packet["battery_mv"] / 1000.0
            packet.update(
                sensor_id=sensor_id, protocol=1, name=sensor["name"], color=sensor["color"],
                battery_voltage=voltage, battery_percent=battery_percent(voltage),
                last_full_charge=entry["last_full_charge"],
            )
            restored[sensor_id] = packet
    with data_lock:
        latest_data.update(restored)


if __name__ == "__main__":



    ensure_data_directory()
    restore_latest_data()



    print()

    print("======================================")

    print(" VMC Environmental Monitor")

    print("======================================")

    print()



    print("CSV data directory:")

    print(f"  {os.path.abspath(DATA_DIR)}")

    print()



    ble_thread = threading.Thread(

        target=run_ble,

        daemon=True,

    )



    ble_thread.start()



    print("Web dashboard:")

    print("  http://localhost")

    print()

    print("From another device on the LAN:")

    print("  http://<computer-ip>")

    print()



    # Port 80 is a privileged port on Linux.

    # Run this script using the venv Python as root:

    #

    # sudo ~/vmc-venv/bin/python vmc_server.py



    app.run(

        host="0.0.0.0",

        port=5000,

        debug=False,

        use_reloader=False,

    )
