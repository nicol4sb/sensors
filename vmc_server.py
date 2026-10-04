#!/usr/bin/env python3



import asyncio

import csv

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

DATA_DIR = "vmc_data"



SENSORS = {

    1: {

        "name": "Red",

        "color": "#ff5a5f",

        "file": "red.csv",

    },

    2: {

        "name": "Green",

        "color": "#35d07f",

        "file": "green.csv",

    },

    3: {

        "name": "Sensor 3",

        "color": "#5da9ff",

        "file": "sensor3.csv",

    },

}





# ============================================================

# Shared data

# ============================================================



latest_data = {}



data_lock = threading.Lock()
csv_lock = threading.Lock()
scanner_lock = threading.Lock()

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



def read_history(hours=None):

    result = {}



    cutoff = None



    if hours is not None:

        cutoff = (

            datetime.now().astimezone()

            - timedelta(hours=hours)

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



                                points.append({

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

                                })



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





<style>



/* ==========================================================

   Theme

   ========================================================== */



:root {

    --bg: #090d14;

    --panel: #111722;

    --panel-light: #151d2a;



    --border: #202b3a;

    --border-light: #293649;



    --text: #f3f6fa;

    --text-secondary: #a9b4c3;

    --text-muted: #6f7c8d;



    --accent: #69a7ff;



    --battery: #46d982;

    --battery-low: #ffcc45;

    --battery-critical: #ff5a5f;



    --shadow:

        0 10px 30px

        rgba(0, 0, 0, 0.24);

}





* {

    box-sizing: border-box;

}





html,

body {

    margin: 0;

    padding: 0;



    min-height: 100%;



    background:

        radial-gradient(

            circle at top,

            #111a28 0,

            #090d14 48%,

            #070a10 100%

        );



    color: var(--text);



    font-family:

        Inter,

        ui-sans-serif,

        system-ui,

        -apple-system,

        BlinkMacSystemFont,

        "Segoe UI",

        sans-serif;



    -webkit-font-smoothing: antialiased;

}





body {

    min-height: 100vh;

}





/* ==========================================================

   Main dashboard

   ========================================================== */



.dashboard {

    width: 100%;

    max-width: 1500px;



    min-height: 100vh;



    margin: 0 auto;



    padding: 14px 18px;



    display: flex;

    flex-direction: column;



    gap: 10px;

}





/* ==========================================================

   Header

   ========================================================== */



.header {

    display: flex;

    justify-content: space-between;

    align-items: center;



    gap: 20px;



    flex: 0 0 auto;

}





.title-area {

    display: flex;

    align-items: center;



    gap: 11px;

}





.logo {

    width: 35px;

    height: 35px;



    border-radius: 10px;



    display: grid;

    place-items: center;



    background:

        linear-gradient(

            145deg,

            #1d2c40,

            #111824

        );



    border: 1px solid #2a3b52;

}





.logo-wave {

    font-size: 18px;

    color: #7bb4ff;

}





.title {

    margin: 0;



    font-size: 20px;

    line-height: 1.05;



    font-weight: 720;



    letter-spacing: -0.3px;

}





.subtitle {

    margin-top: 3px;



    color: var(--text-muted);



    font-size: 10px;



    text-transform: uppercase;

    letter-spacing: 1.2px;

}





/* ==========================================================

   History controls

   ========================================================== */



.history-controls {

    display: flex;

    align-items: center;



    padding: 3px;



    border-radius: 9px;



    background: #0c111a;



    border: 1px solid var(--border);

}





.history-controls button {

    appearance: none;



    min-width: 43px;



    border: 0;



    padding: 6px 9px;



    border-radius: 6px;



    background: transparent;



    color: var(--text-muted);



    font-family: inherit;

    font-size: 11px;

    font-weight: 650;



    cursor: pointer;

}





.history-controls button:hover {

    color: var(--text);

    background: #17202d;

}





.history-controls button.active {

    color: #ffffff;



    background:

        linear-gradient(

            180deg,

            #2c405b,

            #213248

        );

}





/* ==========================================================

   Sensor cards

   ========================================================== */



.sensor-grid {

    display: grid;



    grid-template-columns:

        repeat(

            auto-fit,

            minmax(320px, 1fr)

        );



    gap: 10px;



    flex: 0 0 auto;

}





.sensor-card {

    position: relative;



    overflow: hidden;



    padding: 13px 16px;



    border-radius: 13px;



    background:

        linear-gradient(

            145deg,

            rgba(23, 31, 44, 0.98),

            rgba(14, 20, 30, 0.98)

        );



    border: 1px solid var(--border);



    box-shadow: var(--shadow);

}





.sensor-card::before {

    content: "";



    position: absolute;



    left: 0;

    top: 0;

    bottom: 0;



    width: 3px;



    background: var(--sensor-color);

}





.sensor-top {

    display: flex;

    justify-content: space-between;

    align-items: center;



    margin-bottom: 10px;

}





.sensor-identity {

    display: flex;

    align-items: center;



    gap: 7px;

}





.sensor-dot {

    width: 8px;

    height: 8px;



    border-radius: 50%;



    background: var(--sensor-color);



    box-shadow:

        0 0 10px

        var(--sensor-color);

}





.sensor-name {

    font-size: 12px;

    font-weight: 750;



    text-transform: uppercase;



    letter-spacing: 0.8px;



    color: var(--sensor-color);

}





.sequence {

    color: var(--text-muted);



    font-size: 9px;

    font-variant-numeric: tabular-nums;

}





/* ==========================================================

   Main values

   ========================================================== */



.main-values {

    display: grid;



    grid-template-columns:

        repeat(2, 1fr);



    gap: 18px;



    margin-bottom: 12px;

}





.value-number {

    color: #ffffff;



    font-size: clamp(25px, 2.2vw, 34px);



    line-height: 1;



    font-weight: 650;



    letter-spacing: -1px;



    font-variant-numeric: tabular-nums;

}





.value-unit {

    color: var(--text-secondary);



    font-size: 15px;

    font-weight: 500;



    letter-spacing: 0;

}





.value-label {

    margin-top: 5px;



    color: var(--text-muted);



    font-size: 8px;



    font-weight: 700;



    text-transform: uppercase;

    letter-spacing: 1.1px;

}





/* ==========================================================

   Sensor bottom

   ========================================================== */



.sensor-bottom {

    display: flex;



    justify-content: space-between;

    align-items: center;



    gap: 15px;



    padding-top: 9px;



    border-top:

        1px solid

        rgba(150, 170, 195, 0.10);

}





.sensor-indicators {

    display: flex;

    align-items: center;



    gap: 22px;

}





/* ==========================================================

   iPhone-style battery

   ========================================================== */



.battery-info {

    display: flex;

    align-items: center;



    gap: 8px;

}





.battery-shell {

    position: relative;



    width: 31px;

    height: 15px;



    padding: 2px;



    border:

        1.5px solid

        #8c98a7;



    border-radius: 4.5px;

}





.battery-shell::after {

    content: "";



    position: absolute;



    right: -4px;

    top: 4px;



    width: 2.5px;

    height: 5px;



    border-radius:

        0 2px 2px 0;



    background: #8c98a7;

}





.battery-fill {

    height: 100%;



    border-radius: 2px;



    background: var(--battery);



    transition:

        width 0.3s ease,

        background 0.3s ease;

}





.battery-values {

    display: flex;

    align-items: baseline;



    gap: 5px;



    white-space: nowrap;

}





.battery-percent {

    color: #ffffff;



    font-size: 12px;

    font-weight: 700;



    font-variant-numeric: tabular-nums;

}





.battery-voltage {

    color: var(--text-muted);



    font-size: 9px;



    font-variant-numeric: tabular-nums;

}





/* ==========================================================

   RSSI

   ========================================================== */



.signal-info {

    display: flex;

    align-items: center;



    gap: 7px;

}





.signal-bars {

    height: 16px;



    display: flex;

    align-items: flex-end;



    gap: 2px;

}





.signal-bar {

    display: block;



    width: 3px;



    border-radius: 1.5px;



    background: #293441;

}





.signal-bar:nth-child(1) {

    height: 4px;

}





.signal-bar:nth-child(2) {

    height: 7px;

}





.signal-bar:nth-child(3) {

    height: 10px;

}





.signal-bar:nth-child(4) {

    height: 13px;

}





.signal-bar:nth-child(5) {

    height: 16px;

}





.signal-bar.active {

    background: #b8c6d8;

}





.signal-count {

    color: var(--text-secondary);



    font-size: 10px;

    font-weight: 650;

}





.signal-rssi {

    color: var(--text-muted);



    font-size: 9px;



    font-variant-numeric: tabular-nums;

}





/* ==========================================================

   Last seen

   ========================================================== */



.last-seen {

    color: var(--text-muted);



    font-size: 9px;



    white-space: nowrap;

}





.last-seen-age {

    color: var(--text-secondary);

}





/* ==========================================================

   Charts

   ========================================================== */



.charts {

    min-height: 0;



    flex: 1 1 auto;



    display: grid;



    grid-template-rows:

        repeat(

            2,

            minmax(190px, 1fr)

        );



    gap: 10px;

}





.chart-card {

    min-height: 0;



    display: flex;

    flex-direction: column;



    padding: 10px 13px 8px;



    border-radius: 13px;



    background:

        linear-gradient(

            145deg,

            rgba(18, 25, 36, 0.98),

            rgba(12, 17, 26, 0.98)

        );



    border: 1px solid var(--border);



    box-shadow: var(--shadow);

}





.chart-heading {

    display: flex;

    justify-content: space-between;

    align-items: center;



    flex: 0 0 auto;



    margin-bottom: 3px;

}





.chart-title {

    color: #eaf0f7;



    font-size: 11px;



    font-weight: 720;



    text-transform: uppercase;

    letter-spacing: 0.8px;

}





.chart-unit {

    color: var(--text-muted);



    font-size: 9px;

}





.chart-container {

    position: relative;



    flex: 1 1 auto;



    min-height: 0;

}





/* ==========================================================

   Footer

   ========================================================== */



.footer {

    flex: 0 0 auto;



    display: flex;

    justify-content: flex-end;

    align-items: center;



    gap: 6px;



    min-height: 10px;



    color: var(--text-muted);



    font-size: 8px;

}





.status-dot {

    width: 5px;

    height: 5px;



    border-radius: 50%;



    background: #46d982;



    box-shadow:

        0 0 6px

        rgba(70, 217, 130, 0.7);

}





/* ==========================================================

   Compact laptop screens

   ========================================================== */



@media (

    min-width: 701px

)

and (

    max-height: 800px

) {



    .dashboard {

        padding-top: 9px;

        padding-bottom: 7px;



        gap: 7px;

    }



    .sensor-card {

        padding-top: 9px;

        padding-bottom: 9px;

    }



    .sensor-top {

        margin-bottom: 7px;

    }



    .main-values {

        margin-bottom: 8px;

    }



    .value-number {

        font-size: 25px;

    }



    .sensor-bottom {

        padding-top: 7px;

    }



    .charts {

        gap: 7px;



        grid-template-rows:

            repeat(

                2,

                minmax(165px, 1fr)

            );

    }



}





/* ==========================================================

   Mobile

   ========================================================== */



@media (

    max-width: 700px

) {



    .dashboard {

        min-height: auto;



        padding: 12px 10px 18px;



        gap: 10px;

    }





    .header {

        align-items: flex-start;



        flex-direction: column;



        gap: 10px;

    }





    .history-controls {

        width: 100%;

    }





    .history-controls button {

        flex: 1;



        min-width: 0;



        padding: 8px 4px;



        font-size: 11px;

    }





    .sensor-grid {

        grid-template-columns: 1fr;

    }





    .sensor-card {

        padding: 13px 14px;

    }





    .value-number {

        font-size: 29px;

    }





    .sensor-bottom {

        align-items: flex-start;



        flex-direction: column;



        gap: 8px;

    }





    .sensor-indicators {

        width: 100%;



        justify-content: space-between;

    }





    .last-seen {

        width: 100%;



        text-align: right;

    }





    .charts {

        display: block;

    }





    .chart-card {

        height: 265px;



        margin-bottom: 10px;

    }





    .footer {

        justify-content: center;



        padding-top: 2px;

    }



}





/* ==========================================================

   Very narrow phone

   ========================================================== */



@media (

    max-width: 380px

) {



    .value-number {

        font-size: 25px;

    }





    .sensor-indicators {

        gap: 10px;

    }





    .battery-values {

        gap: 3px;

    }





    .chart-card {

        height: 235px;

    }



}



</style>



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





        <div class="history-controls">



            <button

                type="button"

                data-hours="1">

                1h

            </button>



            <button

                type="button"

                data-hours="6">

                6h

            </button>



            <button

                type="button"

                data-hours="24"

                class="active">

                24h

            </button>



            <button

                type="button"

                data-hours="168">

                7d

            </button>



            <button

                type="button"

                data-hours="all">

                All

            </button>



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





<script>





// ============================================================

// State

// ============================================================



let historyHours = 24;



let temperatureChart = null;

let humidityChart = null;



let lastHistorySignature = "";





// ============================================================

// Desktop / touch detection

// ============================================================



function isTouchDevice() {



    return (

        window.matchMedia(

            "(hover: none)"

        ).matches

        ||

        window.matchMedia(

            "(pointer: coarse)"

        ).matches

    );

}





// ============================================================

// Relative time

// ============================================================



function relativeTime(timestamp) {



    const then =

        new Date(timestamp).getTime();



    const now =

        Date.now();



    let seconds =

        Math.floor(

            (now - then) / 1000

        );





    if (!Number.isFinite(seconds)) {

        return "unknown";

    }





    if (seconds < 0) {

        seconds = 0;

    }





    if (seconds < 5) {

        return "just now";

    }





    if (seconds < 60) {

        return `${seconds} sec ago`;

    }





    const minutes =

        Math.floor(seconds / 60);





    if (minutes < 60) {



        return (

            minutes === 1

            ? "1 min ago"

            : `${minutes} min ago`

        );

    }





    const hours =

        Math.floor(minutes / 60);





    if (hours < 24) {



        return (

            hours === 1

            ? "1 hour ago"

            : `${hours} hours ago`

        );

    }





    const days =

        Math.floor(hours / 24);





    return (

        days === 1

        ? "1 day ago"

        : `${days} days ago`

    );

}





// ============================================================

// RSSI

// ============================================================



function rssiBars(rssi) {



    if (rssi >= -55) {

        return 5;

    }



    if (rssi >= -65) {

        return 4;

    }



    if (rssi >= -75) {

        return 3;

    }



    if (rssi >= -85) {

        return 2;

    }



    return 1;

}





function signalBars(count) {



    let html = "";



    for (

        let i = 1;

        i <= 5;

        i++

    ) {



        html += `

            <span

                class="signal-bar

                ${i <= count ? "active" : ""}">

            </span>

        `;

    }



    return html;

}





// ============================================================

// Battery

// ============================================================



function batteryColor(percent) {



    if (percent <= 15) {

        return "var(--battery-critical)";

    }



    if (percent <= 30) {

        return "var(--battery-low)";

    }



    return "var(--battery)";

}





// ============================================================

// Sensor card

// ============================================================



function sensorCard(sensor) {



    const bars =

        rssiBars(sensor.rssi);



    const battery =

        Math.max(

            0,

            Math.min(

                100,

                sensor.battery_percent

            )

        );



    const batteryFill =

        battery === 0

        ? 0

        : Math.max(5, battery);





    return `



        <div

            class="sensor-card"

            style="

                --sensor-color:

                ${sensor.color};

            ">





            <div class="sensor-top">



                <div class="sensor-identity">



                    <span class="sensor-dot"></span>



                    <span class="sensor-name">

                        ${sensor.name}

                    </span>



                </div>





                <div class="sequence">

                    SEQ ${sensor.sequence}

                </div>



            </div>





            <div class="main-values">





                <div>



                    <div class="value-number">



                        ${sensor.temperature.toFixed(2)}



                        <span class="value-unit">

                            °C

                        </span>



                    </div>



                    <div class="value-label">

                        Temperature

                    </div>



                </div>





                <div>



                    <div class="value-number">



                        ${sensor.humidity.toFixed(2)}



                        <span class="value-unit">

                            %

                        </span>



                    </div>



                    <div class="value-label">

                        Humidity

                    </div>



                </div>





            </div>





            <div class="sensor-bottom">





                <div class="sensor-indicators">





                    <div class="battery-info">



                        <div

                            class="battery-shell"

                            title="${sensor.battery_voltage.toFixed(3)} V">



                            <div

                                class="battery-fill"

                                style="

                                    width: ${batteryFill}%;

                                    background:

                                    ${batteryColor(battery)};

                                ">

                            </div>



                        </div>





                        <div class="battery-values">



                            <span class="battery-percent">

                                ${battery}%

                            </span>



                            <span class="battery-voltage">

                                ${sensor.battery_voltage.toFixed(3)} V

                            </span>



                        </div>



                    </div>





                    <div class="signal-info">



                        <div

                            class="signal-bars"

                            title="${sensor.rssi} dBm">



                            ${signalBars(bars)}



                        </div>





                        <span class="signal-count">

                            ${bars}/5

                        </span>





                        <span class="signal-rssi">

                            ${sensor.rssi} dBm

                        </span>



                    </div>





                </div>





                <div class="last-seen">



                    Last seen



                    <span

                        class="last-seen-age"

                        data-timestamp="${sensor.timestamp}">



                        ${relativeTime(

                            sensor.timestamp

                        )}



                    </span>



                </div>





            </div>





        </div>



    `;

}





// ============================================================

// Update relative "last seen"

// ============================================================



function updateLastSeenLabels() {



    document

        .querySelectorAll(

            ".last-seen-age"

        )

        .forEach(



            element => {



                element.textContent =

                    relativeTime(

                        element.dataset.timestamp

                    );



            }



        );

}





// ============================================================

// Graph time formatting

// ============================================================



function formatGraphTime(milliseconds) {



    const date =

        new Date(milliseconds);





    if (

        historyHours === 1

        ||

        historyHours === 6

        ||

        historyHours === 24

    ) {



        return date.toLocaleTimeString(

            [],

            {

                hour: "2-digit",

                minute: "2-digit"

            }

        );

    }





    if (historyHours === 168) {



        return date.toLocaleString(

            [],

            {

                weekday: "short",

                hour: "2-digit"

            }

        );

    }





    return date.toLocaleDateString(

        [],

        {

            month: "short",

            day: "numeric"

        }

    );

}





// ============================================================

// Find nearest point in a dataset

//

// Uses binary search because historical points are sorted by

// timestamp.

// ============================================================



function nearestPoint(

    points,

    targetX

) {



    if (

        !points

        ||

        points.length === 0

    ) {

        return null;

    }





    if (targetX <= points[0].x) {

        return points[0];

    }





    const lastIndex =

        points.length - 1;





    if (

        targetX >=

        points[lastIndex].x

    ) {

        return points[lastIndex];

    }





    let low = 0;

    let high = lastIndex;





    while (

        low <= high

    ) {



        const mid =

            Math.floor(

                (low + high) / 2

            );





        const x =

            points[mid].x;





        if (x === targetX) {

            return points[mid];

        }





        if (x < targetX) {

            low = mid + 1;

        }



        else {

            high = mid - 1;

        }

    }





    const before =

        points[high];



    const after =

        points[low];





    if (

        Math.abs(

            targetX - before.x

        )

        <=

        Math.abs(

            after.x - targetX

        )

    ) {

        return before;

    }





    return after;

}





// ============================================================

// Dataset

// ============================================================



function makeDataset(

    sensor,

    field

) {



    const points =

        sensor.points.map(



            point => ({



                x:

                    point.timestamp_ms,



                y:

                    point[field]



            })



        );





    points.sort(

        (a, b) =>

            a.x - b.x

    );





    return {



        label:

            sensor.name,



        data:

            points,



        borderColor:

            sensor.color,



        backgroundColor:

            sensor.color,



        borderWidth:

            2.2,



        pointRadius:

            0,



        pointHoverRadius:

            0,



        pointHitRadius:

            0,



        tension:

            0.15,



        spanGaps:

            false



    };

}





// ============================================================

// Desktop crosshair + combined tooltip plugin

// ============================================================



const desktopInspectorPlugin = {



    id:

        "desktopInspector",





    afterEvent(

        chart,

        args

    ) {



        if (isTouchDevice()) {

            return;

        }





        const event =

            args.event;





        if (

            event.type === "mouseout"

        ) {



            if (

                chart.$inspectionX !== null

            ) {



                chart.$inspectionX =

                    null;



                chart.$inspectionTime =

                    null;



                chart.draw();

            }



            return;

        }





        if (

            event.type !== "mousemove"

        ) {

            return;

        }





        const area =

            chart.chartArea;





        if (!area) {

            return;

        }





        const x =

            event.x;





        const y =

            event.y;





        if (

            x < area.left

            ||

            x > area.right

            ||

            y < area.top

            ||

            y > area.bottom

        ) {



            if (

                chart.$inspectionX !== null

            ) {



                chart.$inspectionX =

                    null;



                chart.$inspectionTime =

                    null;



                chart.draw();

            }



            return;

        }





        chart.$inspectionX =

            x;





        chart.$inspectionTime =

            chart.scales.x.getValueForPixel(

                x

            );





        args.changed =

            true;

    },





    afterDraw(chart) {



        if (isTouchDevice()) {

            return;

        }





        if (

            chart.$inspectionX == null

            ||

            chart.$inspectionTime == null

        ) {

            return;

        }





        const ctx =

            chart.ctx;



        const area =

            chart.chartArea;



        const x =

            chart.$inspectionX;





        // -----------------------------------------------

        // Vertical guide line

        // -----------------------------------------------



        ctx.save();



        ctx.beginPath();



        ctx.moveTo(

            x,

            area.top

        );



        ctx.lineTo(

            x,

            area.bottom

        );



        ctx.lineWidth =

            1;



        ctx.strokeStyle =

            "rgba(210,225,242,0.28)";



        ctx.stroke();



        ctx.restore();





        // -----------------------------------------------

        // Collect nearest value from every sensor

        // -----------------------------------------------



        const rows = [];





        for (

            const dataset

            of chart.data.datasets

        ) {



            const point =

                nearestPoint(

                    dataset.data,

                    chart.$inspectionTime

                );





            if (!point) {

                continue;

            }





            rows.push({

                label:

                    dataset.label,



                color:

                    dataset.borderColor,



                value:

                    point.y,



                timestamp:

                    point.x

            });

        }





        if (

            rows.length === 0

        ) {

            return;

        }





        // Use cursor time for the heading.

        const heading =

            new Date(

                chart.$inspectionTime

            ).toLocaleString();





        // -----------------------------------------------

        // Tooltip geometry

        // -----------------------------------------------



        ctx.save();





        const titleFont =

            "600 11px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";



        const bodyFont =

            "600 11px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";





        ctx.font =

            titleFont;





        let width =

            ctx.measureText(

                heading

            ).width;





        for (

            const row

            of rows

        ) {



            ctx.font =

                bodyFont;





            const unit =

                chart.$measurementType

                === "temperature"

                ? " °C"

                : " %";





            const text =

                row.label

                +

                "   "

                +

                Number(

                    row.value

                ).toFixed(2)

                +

                unit;





            width =

                Math.max(

                    width,

                    ctx.measureText(

                        text

                    ).width + 20

                );

        }





        const padding =

            10;



        const titleHeight =

            17;



        const rowHeight =

            18;





        width +=

            padding * 2;





        const height =

            padding * 2

            +

            titleHeight

            +

            rows.length

            *

            rowHeight;





        let boxX =

            x + 12;





        let boxY =

            area.top + 8;





        if (

            boxX + width

            >

            area.right

        ) {



            boxX =

                x

                -

                width

                -

                12;

        }





        if (

            boxX < area.left

        ) {



            boxX =

                area.left + 4;

        }





        // -----------------------------------------------

        // Rounded tooltip background

        // -----------------------------------------------



        const radius =

            8;





        ctx.beginPath();



        ctx.roundRect(

            boxX,

            boxY,

            width,

            height,

            radius

        );



        ctx.fillStyle =

            "rgba(20,29,42,0.97)";



        ctx.fill();





        ctx.lineWidth =

            1;



        ctx.strokeStyle =

            "rgba(72,91,116,0.95)";



        ctx.stroke();





        // -----------------------------------------------

        // Tooltip title

        // -----------------------------------------------



        ctx.font =

            titleFont;



        ctx.fillStyle =

            "#aab7c7";



        ctx.textBaseline =

            "top";





        ctx.fillText(

            heading,

            boxX + padding,

            boxY + padding

        );





        // -----------------------------------------------

        // Sensor rows

        // -----------------------------------------------



        let rowY =

            boxY

            +

            padding

            +

            titleHeight;





        for (

            const row

            of rows

        ) {



            const unit =

                chart.$measurementType

                === "temperature"

                ? " °C"

                : " %";





            // Sensor color dot



            ctx.beginPath();



            ctx.arc(

                boxX + padding + 4,

                rowY + 7,

                3.5,

                0,

                Math.PI * 2

            );



            ctx.fillStyle =

                row.color;



            ctx.fill();





            // Sensor label



            ctx.font =

                bodyFont;



            ctx.fillStyle =

                "#eef3f8";





            const text =

                row.label

                +

                "   "

                +

                Number(

                    row.value

                ).toFixed(2)

                +

                unit;





            ctx.fillText(

                text,

                boxX + padding + 14,

                rowY

            );





            rowY +=

                rowHeight;

        }





        ctx.restore();

    }



};





// ============================================================

// Chart options

// ============================================================



function chartOptions(type) {



    const touch =

        isTouchDevice();





    return {



        responsive:

            true,



        maintainAspectRatio:

            false,



        animation:

            false,



        normalized:

            true,



        parsing:

            false,





        // We use our own desktop inspector instead of the

        // standard Chart.js tooltip.



        interaction: {



            mode:

                undefined,



            intersect:

                false



        },





        events:

            touch

            ? []

            : [

                "mousemove",

                "mouseout"

            ],





        layout: {



            padding: {



                left: 2,

                right: 5,

                top: 0,

                bottom: 0



            }



        },





        plugins: {





            legend: {



                display:

                    true,



                position:

                    "top",



                align:

                    "end",



                labels: {



                    color:

                        "#9eabba",



                    boxWidth:

                        18,



                    boxHeight:

                        2,



                    padding:

                        13,



                    font: {



                        size:

                            10,



                        weight:

                            "600"



                    }



                }



            },





            // Disable normal Chart.js tooltip.

            // Our custom inspector displays all sensors.



            tooltip: {



                enabled:

                    false



            }



        },





        scales: {





            x: {



                type:

                    "linear",





                border: {



                    color:

                        "#2a3545"



                },





                grid: {



                    color:

                        "rgba(170,190,215,0.08)",



                    drawTicks:

                        false



                },





                ticks: {



                    color:

                        "#7e8b9c",



                    padding:

                        7,



                    maxTicksLimit:

                        window.innerWidth <= 700

                        ? 5

                        : 9,



                    maxRotation:

                        0,



                    autoSkip:

                        true,



                    font: {



                        size:

                            9,



                        weight:

                            "500"



                    },





                    callback:

                        function(value) {



                            return formatGraphTime(

                                value

                            );

                        }



                }



            },





            y: {



                border: {



                    display:

                        false



                },





                grid: {



                    color:

                        "rgba(170,190,215,0.10)",



                    drawTicks:

                        false



                },





                ticks: {



                    color:

                        "#8795a7",



                    padding:

                        8,



                    maxTicksLimit:

                        6,



                    font: {



                        size:

                            10,



                        weight:

                            "500"



                    },





                    callback:

                        function(value) {



                            if (

                                type ===

                                "temperature"

                            ) {



                                return (

                                    Number(value)

                                        .toFixed(1)

                                    +

                                    "°"

                                );

                            }





                            return (

                                Number(value)

                                    .toFixed(0)

                                +

                                "%"

                            );

                        }



                }



            }



        }



    };

}





// ============================================================

// Create chart

// ============================================================



function createChart(

    canvasId,

    type,

    datasets

) {



    const chart =

        new Chart(



            document.getElementById(

                canvasId

            ),



            {



                type:

                    "line",





                data: {



                    datasets:

                        datasets



                },





                options:

                    chartOptions(type),





                plugins: [

                    desktopInspectorPlugin

                ]



            }



        );





    chart.$measurementType =

        type;



    chart.$inspectionX =

        null;



    chart.$inspectionTime =

        null;





    return chart;

}





// ============================================================

// History signature

// ============================================================



function historySignature(history) {



    const parts = [];





    for (

        const sensorId

        of Object.keys(history).sort()

    ) {



        const points =

            history[sensorId].points;





        if (points.length === 0) {



            parts.push(

                sensorId + ":empty"

            );



            continue;

        }





        const last =

            points[

                points.length - 1

            ];





        parts.push(

            sensorId

            +

            ":"

            +

            points.length

            +

            ":"

            +

            last.sequence

            +

            ":"

            +

            last.timestamp_ms

        );

    }





    return parts.join("|");

}





// ============================================================

// Update graph history

// ============================================================



async function updateHistory(

    force = false

) {



    try {



        let url =

            "/api/history";





        if (

            historyHours !== "all"

        ) {



            url +=

                "?hours="

                +

                historyHours;

        }





        const response =

            await fetch(

                url,

                {

                    cache: "no-store"

                }

            );





        if (!response.ok) {



            throw new Error(

                `HTTP ${response.status}`

            );

        }





        const history =

            await response.json();





        const signature =

            historySignature(history);





        if (

            !force

            &&

            signature === lastHistorySignature

        ) {

            return;

        }





        lastHistorySignature =

            signature;





        const sensors =

            Object.values(history)

            .filter(



                sensor =>

                    sensor.points.length > 0



            );





        const temperatureDatasets =

            sensors.map(



                sensor =>

                    makeDataset(

                        sensor,

                        "temperature"

                    )



            );





        const humidityDatasets =

            sensors.map(



                sensor =>

                    makeDataset(

                        sensor,

                        "humidity"

                    )



            );





        if (temperatureChart) {

            temperatureChart.destroy();

        }





        if (humidityChart) {

            humidityChart.destroy();

        }





        temperatureChart =

            createChart(

                "temperature-chart",

                "temperature",

                temperatureDatasets

            );





        humidityChart =

            createChart(

                "humidity-chart",

                "humidity",

                humidityDatasets

            );



    }



    catch (error) {



        console.error(

            "History update failed:",

            error

        );



    }



}





// ============================================================

// Update live sensor cards

// ============================================================



async function updateSensors() {



    try {



        const response =

            await fetch(

                "/api/sensors",

                {

                    cache: "no-store"

                }

            );





        if (!response.ok) {



            throw new Error(

                `HTTP ${response.status}`

            );

        }





        const sensors =

            await response.json();



        const statusResponse =
            await fetch(
                "/api/status",
                { cache: "no-store" }
            );



        const scannerStatus =
            statusResponse.ok
                ? await statusResponse.json()
                : null;





        const grid =

            document.getElementById(

                "sensor-grid"

            );





        const values =

            Object.values(sensors);





        values.sort(

            (a, b) =>

                a.sensor_id

                -

                b.sensor_id

        );





        if (values.length === 0) {



            grid.innerHTML =

                "";



            document.getElementById(

                "status"

            ).textContent =

                scannerStatus && scannerStatus.running
                    ? "BLE scanner active - waiting for advertisements"
                    : "BLE scanner restarting";



            return;

        }





        grid.innerHTML =

            values.map(

                sensor =>

                    sensorCard(sensor)

            ).join("");





        document.getElementById(

            "status"

        ).textContent =

            scannerStatus && scannerStatus.running
                ? "BLE scanner active"
                : "BLE scanner restarting";



    }



    catch (error) {



        document.getElementById(

            "status"

        ).textContent =

            "Server connection error";



    }



}





// ============================================================

// History controls

// ============================================================



document

    .querySelectorAll(

        ".history-controls button"

    )

    .forEach(



        button => {



            button.addEventListener(



                "click",



                async () => {





                    document

                        .querySelectorAll(

                            ".history-controls button"

                        )

                        .forEach(



                            other =>

                                other.classList.remove(

                                    "active"

                                )



                        );





                    button.classList.add(

                        "active"

                    );





                    const value =

                        button.dataset.hours;





                    if (

                        value === "all"

                    ) {



                        historyHours =

                            "all";



                    }



                    else {



                        historyHours =

                            Number(value);



                    }





                    lastHistorySignature =

                        "";





                    await updateHistory(

                        true

                    );



                }



            );



        }



    );





// ============================================================

// Initial load

// ============================================================



updateSensors();

updateHistory(true);





// ============================================================

// Timers

// ============================================================



setInterval(

    updateSensors,

    1000

);





setInterval(

    updateLastSeenLabels,

    1000

);





setInterval(

    updateHistory,

    5000

);





</script>



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

        read_history(hours)

    )





# ============================================================

# Main

# ============================================================



if __name__ == "__main__":



    ensure_data_directory()



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