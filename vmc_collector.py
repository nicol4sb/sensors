#!/usr/bin/env python3

import asyncio
import csv
import struct
import time
from datetime import datetime, timedelta
from pathlib import Path

from bleak import BleakClient, BleakScanner
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


# ============================================================
# Configuration
# ============================================================

SENSORS = {
    1: {
        "name": "NNTempHumidityRed",
        "label": "Red",
        "color": "red",
    },
    2: {
        "name": "NNTempHumidityGreen",
        "label": "Green",
        "color": "green",
    },
}

COMPANY_ID = 0x0059

SCAN_TIMEOUT = 180.0
CONNECT_TIMEOUT = 20.0
STREAM_TIMEOUT = 90.0

OUTPUT_DIRECTORY = Path("vmc_data")


# ============================================================
# BLE UUIDs
# ============================================================

SERVICE_UUID = "7a100000-4c7f-4f4d-432d-564d4353454e"

METADATA_UUID = "7a100001-4c7f-4f4d-432d-564d4353454e"
CURRENT_UUID = "7a100002-4c7f-4f4d-432d-564d4353454e"

HISTORY_INDEX_UUID = "7a100003-4c7f-4f4d-432d-564d4353454e"
HISTORY_BLOCK_UUID = "7a100004-4c7f-4f4d-432d-564d4353454e"

BATTERY_CURRENT_UUID = "7a100005-4c7f-4f4d-432d-564d4353454e"
BATTERY_INDEX_UUID = "7a100006-4c7f-4f4d-432d-564d4353454e"
BATTERY_BLOCK_UUID = "7a100007-4c7f-4f4d-432d-564d4353454e"


# ============================================================
# Time helpers
# ============================================================

def local_now():
    """
    Return timezone-aware local system time.

    The nRF sensor has no real-time clock, so the computer's
    clock anchors the newest sensor sample.
    """
    return datetime.now().astimezone()


def local_timezone():
    """
    Return the computer's local timezone.

    Used explicitly by Matplotlib so that timezone-aware CSV
    timestamps such as +02:00 are displayed as local time
    instead of UTC.
    """
    return datetime.now().astimezone().tzinfo


def timestamp_to_string(timestamp):
    """
    ISO-8601 including timezone offset.

    Example:
        2026-09-23T19:23:30+02:00
    """
    return timestamp.isoformat(timespec="seconds")


def string_to_timestamp(value):
    return datetime.fromisoformat(value)


# ============================================================
# Battery percentage estimate
# ============================================================

def battery_percent(millivolts):
    """
    Approximate LiPo state of charge from voltage.

    This is intentionally approximate. LiPo voltage versus
    state-of-charge is nonlinear and depends on load,
    temperature, battery chemistry, age, etc.

    Raw millivolts remain the authoritative measurement.
    """

    voltage = millivolts / 1000.0

    points = [
        (3.20, 0),
        (3.30, 5),
        (3.50, 10),
        (3.60, 20),
        (3.70, 40),
        (3.80, 60),
        (3.90, 75),
        (4.00, 85),
        (4.10, 95),
        (4.20, 100),
    ]

    if voltage <= points[0][0]:
        return 0.0

    if voltage >= points[-1][0]:
        return 100.0

    for i in range(len(points) - 1):

        v1, p1 = points[i]
        v2, p2 = points[i + 1]

        if v1 <= voltage <= v2:

            fraction = (
                (voltage - v1)
                / (v2 - v1)
            )

            return (
                p1
                + fraction * (p2 - p1)
            )

    return 0.0


# ============================================================
# Metadata
# ============================================================

def parse_metadata(data):

    if len(data) != 16:
        raise ValueError(
            f"Metadata should be 16 bytes, "
            f"received {len(data)}"
        )

    (
        protocol_version,
        sensor_id,
        environment_interval_seconds,
        environment_history_count,
        environment_history_capacity,
        newest_sequence,
        battery_history_count,
        battery_interval_seconds,
    ) = struct.unpack(
        "<BBHHHIHH",
        data,
    )

    return {
        "protocol_version":
            protocol_version,

        "sensor_id":
            sensor_id,

        "environment_interval_seconds":
            environment_interval_seconds,

        "environment_history_count":
            environment_history_count,

        "environment_history_capacity":
            environment_history_capacity,

        "newest_sequence":
            newest_sequence,

        "battery_history_count":
            battery_history_count,

        "battery_interval_seconds":
            battery_interval_seconds,
    }


# ============================================================
# Manufacturer data
# ============================================================

def manufacturer_sensor_id(
    advertisement_data,
):

    manufacturer_data = (
        advertisement_data.manufacturer_data
    )

    payload = manufacturer_data.get(
        COMPANY_ID
    )

    if payload is None:
        return None

    if len(payload) < 1:
        return None

    return payload[0]


# ============================================================
# Environment history download
# ============================================================

async def download_environment_history(
    client,
    expected_count,
):

    if expected_count == 0:
        return []

    received = {}

    complete = asyncio.Event()

    packet_count = 0

    def notification_handler(
        sender,
        data,
    ):

        nonlocal packet_count

        try:

            if len(data) < 4:
                return

            start_index, count = (
                struct.unpack_from(
                    "<HH",
                    data,
                    0,
                )
            )

            expected_length = (
                4 + count * 8
            )

            if len(data) != expected_length:

                print(
                    "  Environment packet "
                    "length mismatch: "
                    f"expected {expected_length}, "
                    f"got {len(data)}"
                )

                return

            packet_count += 1

            offset = 4

            for i in range(count):

                (
                    sequence,
                    temperature_raw,
                    humidity_raw,
                ) = struct.unpack_from(
                    "<IhH",
                    data,
                    offset,
                )

                logical_index = (
                    start_index + i
                )

                received[
                    logical_index
                ] = {
                    "sequence":
                        sequence,

                    "temperature":
                        temperature_raw / 100.0,

                    "humidity":
                        humidity_raw / 100.0,
                }

                offset += 8

            if len(received) >= expected_count:
                complete.set()

        except Exception as exc:

            print(
                "  Environment notification "
                f"error: {exc}"
            )

    await client.start_notify(
        HISTORY_BLOCK_UUID,
        notification_handler,
    )

    transfer_start = time.monotonic()

    try:

        await client.write_gatt_char(
            HISTORY_INDEX_UUID,
            struct.pack(
                "<H",
                0,
            ),
            response=True,
        )

        await asyncio.wait_for(
            complete.wait(),
            timeout=STREAM_TIMEOUT,
        )

    finally:

        await client.stop_notify(
            HISTORY_BLOCK_UUID
        )

    transfer_time = (
        time.monotonic()
        - transfer_start
    )

    missing = [
        index
        for index in range(
            expected_count
        )
        if index not in received
    ]

    if missing:

        raise RuntimeError(
            "Environment history missing "
            f"{len(missing)} samples"
        )

    samples = [
        received[index]
        for index in range(
            expected_count
        )
    ]

    print(
        f"  Environment: "
        f"{len(samples)} samples, "
        f"{packet_count} packets, "
        f"{transfer_time:.2f} s"
    )

    return samples


# ============================================================
# Battery history download
# ============================================================

async def download_battery_history(
    client,
    expected_count,
):

    if expected_count == 0:
        return []

    received = {}

    complete = asyncio.Event()

    packet_count = 0

    def notification_handler(
        sender,
        data,
    ):

        nonlocal packet_count

        try:

            if len(data) < 4:
                return

            start_index, count = (
                struct.unpack_from(
                    "<HH",
                    data,
                    0,
                )
            )

            expected_length = (
                4 + count * 6
            )

            if len(data) != expected_length:

                print(
                    "  Battery packet "
                    "length mismatch: "
                    f"expected {expected_length}, "
                    f"got {len(data)}"
                )

                return

            packet_count += 1

            offset = 4

            for i in range(count):

                (
                    sequence,
                    millivolts,
                ) = struct.unpack_from(
                    "<IH",
                    data,
                    offset,
                )

                logical_index = (
                    start_index + i
                )

                received[
                    logical_index
                ] = {
                    "sequence":
                        sequence,

                    "millivolts":
                        millivolts,
                }

                offset += 6

            if len(received) >= expected_count:
                complete.set()

        except Exception as exc:

            print(
                "  Battery notification "
                f"error: {exc}"
            )

    await client.start_notify(
        BATTERY_BLOCK_UUID,
        notification_handler,
    )

    transfer_start = time.monotonic()

    try:

        await client.write_gatt_char(
            BATTERY_INDEX_UUID,
            struct.pack(
                "<H",
                0,
            ),
            response=True,
        )

        await asyncio.wait_for(
            complete.wait(),
            timeout=STREAM_TIMEOUT,
        )

    finally:

        await client.stop_notify(
            BATTERY_BLOCK_UUID
        )

    transfer_time = (
        time.monotonic()
        - transfer_start
    )

    missing = [
        index
        for index in range(
            expected_count
        )
        if index not in received
    ]

    if missing:

        raise RuntimeError(
            "Battery history missing "
            f"{len(missing)} samples"
        )

    samples = [
        received[index]
        for index in range(
            expected_count
        )
    ]

    print(
        f"  Battery:     "
        f"{len(samples)} samples, "
        f"{packet_count} packets, "
        f"{transfer_time:.2f} s"
    )

    return samples


# ============================================================
# Timestamp reconstruction
# ============================================================

def timestamp_environment_samples(
    samples,
    newest_sequence,
    interval_seconds,
    newest_time,
):

    for sample in samples:

        sequence_difference = (
            newest_sequence
            - sample["sequence"]
        )

        timestamp = (
            newest_time
            - timedelta(
                seconds=(
                    sequence_difference
                    * interval_seconds
                )
            )
        )

        sample["timestamp"] = (
            timestamp
        )

    return samples


def timestamp_battery_samples(
    samples,
    newest_environment_sequence,
    environment_interval_seconds,
    newest_time,
):

    """
    Battery sequence values use the environmental
    sequence timeline.

    Therefore battery timestamps are reconstructed
    relative to the newest environmental sequence.
    """

    for sample in samples:

        sequence_difference = (
            newest_environment_sequence
            - sample["sequence"]
        )

        timestamp = (
            newest_time
            - timedelta(
                seconds=(
                    sequence_difference
                    * environment_interval_seconds
                )
            )
        )

        sample["timestamp"] = (
            timestamp
        )

    return samples


# ============================================================
# CSV paths
# ============================================================

def environment_csv_path(sensor):

    filename = (
        sensor["label"].lower()
        + "_environment.csv"
    )

    return (
        OUTPUT_DIRECTORY
        / filename
    )


def battery_csv_path(sensor):

    filename = (
        sensor["label"].lower()
        + "_battery.csv"
    )

    return (
        OUTPUT_DIRECTORY
        / filename
    )


# ============================================================
# Read environment CSV
# ============================================================

def read_environment_csv(path):

    if not path.exists():
        return []

    rows = []

    with path.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:

        reader = csv.DictReader(
            file
        )

        for row in reader:

            try:

                rows.append({
                    "timestamp":
                        string_to_timestamp(
                            row["timestamp"]
                        ),

                    "session":
                        int(
                            row.get(
                                "session",
                                0,
                            )
                        ),

                    "sequence":
                        int(
                            row["sequence"]
                        ),

                    "temperature":
                        float(
                            row[
                                "temperature_c"
                            ]
                        ),

                    "humidity":
                        float(
                            row[
                                "humidity_percent"
                            ]
                        ),
                })

            except Exception as exc:

                print(
                    "  Warning: skipped "
                    f"bad row in {path}: "
                    f"{exc}"
                )

    return rows


# ============================================================
# Read battery CSV
# ============================================================

def read_battery_csv(path):

    if not path.exists():
        return []

    rows = []

    with path.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:

        reader = csv.DictReader(
            file
        )

        for row in reader:

            try:

                rows.append({
                    "timestamp":
                        string_to_timestamp(
                            row["timestamp"]
                        ),

                    "session":
                        int(
                            row.get(
                                "session",
                                0,
                            )
                        ),

                    "sequence":
                        int(
                            row["sequence"]
                        ),

                    "millivolts":
                        int(
                            row["millivolts"]
                        ),
                })

            except Exception as exc:

                print(
                    "  Warning: skipped "
                    f"bad row in {path}: "
                    f"{exc}"
                )

    return rows


# ============================================================
# Session detection
# ============================================================

def determine_session(
    existing_environment,
    downloaded_environment,
):

    """
    Sequence numbers restart when the nRF resets
    or is reflashed.

    Each restart therefore becomes a new session.
    """

    if not downloaded_environment:
        return 0

    if not existing_environment:
        return 1

    highest_session = max(
        row["session"]
        for row in existing_environment
    )

    current_session_rows = [
        row
        for row in existing_environment
        if row["session"]
        == highest_session
    ]

    if not current_session_rows:
        return highest_session + 1

    existing_max_sequence = max(
        row["sequence"]
        for row in current_session_rows
    )

    downloaded_max_sequence = max(
        row["sequence"]
        for row in downloaded_environment
    )

    downloaded_min_sequence = min(
        row["sequence"]
        for row in downloaded_environment
    )

    # Normal case:
    #
    # Sensor history overlaps or extends
    # the current session.

    if (
        downloaded_max_sequence
        >= existing_max_sequence
    ):
        return highest_session

    # Sequence counter has gone backwards.
    # Most likely reboot/reflash.

    print()
    print(
        "  Sequence reset detected:"
    )

    print(
        "    previous maximum = "
        f"{existing_max_sequence}"
    )

    print(
        "    sensor now has    = "
        f"{downloaded_min_sequence}"
        f"..{downloaded_max_sequence}"
    )

    print(
        "    starting session "
        f"{highest_session + 1}"
    )

    return highest_session + 1


# ============================================================
# Merge environment history
# ============================================================

def merge_environment_history(
    existing,
    downloaded,
    session,
):

    """
    Existing timestamps always win.

    Unique key:
        (session, sequence)

    Therefore repeated downloads don't duplicate
    measurements, while sequence resets are safe.
    """

    merged = {}

    for sample in existing:

        key = (
            sample["session"],
            sample["sequence"],
        )

        merged[key] = sample

    added = 0

    for sample in downloaded:

        key = (
            session,
            sample["sequence"],
        )

        if key in merged:
            continue

        new_sample = dict(
            sample
        )

        new_sample["session"] = (
            session
        )

        merged[key] = (
            new_sample
        )

        added += 1

    result = list(
        merged.values()
    )

    result.sort(
        key=lambda row:
            row["timestamp"]
    )

    return result, added


# ============================================================
# Merge battery history
# ============================================================

def merge_battery_history(
    existing,
    downloaded,
    session,
):

    merged = {}

    for sample in existing:

        key = (
            sample["session"],
            sample["sequence"],
        )

        merged[key] = sample

    added = 0

    for sample in downloaded:

        key = (
            session,
            sample["sequence"],
        )

        if key in merged:
            continue

        new_sample = dict(
            sample
        )

        new_sample["session"] = (
            session
        )

        merged[key] = (
            new_sample
        )

        added += 1

    result = list(
        merged.values()
    )

    result.sort(
        key=lambda row:
            row["timestamp"]
    )

    return result, added


# ============================================================
# Write environment CSV
# ============================================================

def write_environment_csv(
    path,
    samples,
):

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(
            file
        )

        writer.writerow([
            "timestamp",
            "session",
            "sequence",
            "temperature_c",
            "humidity_percent",
        ])

        for sample in samples:

            writer.writerow([
                timestamp_to_string(
                    sample["timestamp"]
                ),

                sample["session"],

                sample["sequence"],

                f"{sample['temperature']:.2f}",

                f"{sample['humidity']:.2f}",
            ])


# ============================================================
# Write battery CSV
# ============================================================

def write_battery_csv(
    path,
    samples,
):

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(
            file
        )

        writer.writerow([
            "timestamp",
            "session",
            "sequence",
            "millivolts",
            "volts",
            "battery_percent_estimate",
        ])

        for sample in samples:

            millivolts = (
                sample["millivolts"]
            )

            writer.writerow([
                timestamp_to_string(
                    sample["timestamp"]
                ),

                sample["session"],

                sample["sequence"],

                millivolts,

                f"{millivolts / 1000.0:.3f}",

                f"{battery_percent(millivolts):.1f}",
            ])


# ============================================================
# Collect one sensor
# ============================================================

async def collect_sensor(
    device,
    sensor_id,
):

    sensor = (
        SENSORS[sensor_id]
    )

    print()
    print("=" * 60)

    print(
        f"{sensor['label']} sensor"
    )

    print("=" * 60)

    print(
        f"Connecting to "
        f"{device.address} ..."
    )

    async with BleakClient(
        device,
        timeout=CONNECT_TIMEOUT,
    ) as client:

        if not client.is_connected:

            raise RuntimeError(
                "BLE connection failed"
            )

        print("Connected.")

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        metadata_raw = (
            await client.read_gatt_char(
                METADATA_UUID
            )
        )

        # Capture computer time immediately after
        # reading metadata.

        metadata_time = (
            local_now()
        )

        metadata = (
            parse_metadata(
                metadata_raw
            )
        )

        if (
            metadata["protocol_version"]
            != 3
        ):

            raise RuntimeError(
                "Unsupported protocol "
                "version "
                f"{metadata['protocol_version']}"
            )

        if (
            metadata["sensor_id"]
            != sensor_id
        ):

            raise RuntimeError(
                "Sensor ID mismatch: "
                f"expected {sensor_id}, "
                "received "
                f"{metadata['sensor_id']}"
            )

        print(
            "Protocol:      "
            f"{metadata['protocol_version']}"
        )

        print(
            "Sensor ID:     "
            f"{metadata['sensor_id']}"
        )

        print(
            "Env interval:  "
            f"{metadata['environment_interval_seconds']} s"
        )

        print(
            "Env history:   "
            f"{metadata['environment_history_count']}"
            "/"
            f"{metadata['environment_history_capacity']}"
        )

        print(
            "Newest seq:    "
            f"{metadata['newest_sequence']}"
        )

        print(
            "Battery hist:  "
            f"{metadata['battery_history_count']}"
        )

        # ----------------------------------------------------
        # Environment
        # ----------------------------------------------------

        downloaded_environment = (
            await download_environment_history(
                client,
                metadata[
                    "environment_history_count"
                ],
            )
        )

        downloaded_environment = (
            timestamp_environment_samples(
                downloaded_environment,
                metadata[
                    "newest_sequence"
                ],
                metadata[
                    "environment_interval_seconds"
                ],
                metadata_time,
            )
        )

        # ----------------------------------------------------
        # Battery
        # ----------------------------------------------------

        downloaded_battery = (
            await download_battery_history(
                client,
                metadata[
                    "battery_history_count"
                ],
            )
        )

        downloaded_battery = (
            timestamp_battery_samples(
                downloaded_battery,
                metadata[
                    "newest_sequence"
                ],
                metadata[
                    "environment_interval_seconds"
                ],
                metadata_time,
            )
        )

    print("Disconnected.")

    # ========================================================
    # Persistent CSV merge
    # ========================================================

    env_path = (
        environment_csv_path(
            sensor
        )
    )

    battery_path = (
        battery_csv_path(
            sensor
        )
    )

    existing_environment = (
        read_environment_csv(
            env_path
        )
    )

    existing_battery = (
        read_battery_csv(
            battery_path
        )
    )

    session = determine_session(
        existing_environment,
        downloaded_environment,
    )

    (
        merged_environment,
        env_added,
    ) = merge_environment_history(
        existing_environment,
        downloaded_environment,
        session,
    )

    (
        merged_battery,
        battery_added,
    ) = merge_battery_history(
        existing_battery,
        downloaded_battery,
        session,
    )

    write_environment_csv(
        env_path,
        merged_environment,
    )

    write_battery_csv(
        battery_path,
        merged_battery,
    )

    print()

    print(
        f"CSV session:        "
        f"{session}"
    )

    print(
        f"Environment added:  "
        f"{env_added}"
    )

    print(
        f"Environment total:  "
        f"{len(merged_environment)}"
    )

    print(
        f"Battery added:      "
        f"{battery_added}"
    )

    print(
        f"Battery total:      "
        f"{len(merged_battery)}"
    )

    print(
        f"Environment CSV:    "
        f"{env_path}"
    )

    print(
        f"Battery CSV:        "
        f"{battery_path}"
    )


# ============================================================
# BLE discovery / collection
# ============================================================

async def collect_all_sensors():

    pending = set(
        SENSORS.keys()
    )

    while pending:

        queue = asyncio.Queue()

        def detection_callback(
            device,
            advertisement_data,
        ):

            sensor_id = (
                manufacturer_sensor_id(
                    advertisement_data
                )
            )

            if sensor_id not in pending:
                return

            try:

                queue.put_nowait(
                    (
                        sensor_id,
                        device,
                    )
                )

            except asyncio.QueueFull:
                pass

        print()

        print(
            "Scanning for: "
            + ", ".join(
                SENSORS[
                    sensor_id
                ]["label"]
                for sensor_id
                in sorted(pending)
            )
        )

        scanner = BleakScanner(
            detection_callback=
                detection_callback
        )

        await scanner.start()

        try:

            (
                sensor_id,
                device,
            ) = await asyncio.wait_for(
                queue.get(),
                timeout=SCAN_TIMEOUT,
            )

        except asyncio.TimeoutError:

            await scanner.stop()

            missing = ", ".join(
                SENSORS[
                    sensor_id
                ]["label"]
                for sensor_id
                in sorted(pending)
            )

            print(
                "Scan timeout. "
                f"Still missing: {missing}"
            )

            return

        await scanner.stop()

        sensor = (
            SENSORS[sensor_id]
        )

        print(
            f"Found "
            f"{sensor['label']} "
            f"({device.address})"
        )

        try:

            await collect_sensor(
                device,
                sensor_id,
            )

            pending.remove(
                sensor_id
            )

        except Exception as exc:

            print()

            print(
                f"{sensor['label']} "
                "collection failed:"
            )

            print(
                f"  "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            print(
                "Will retry when it "
                "advertises again."
            )

        # Give BlueZ a moment after disconnect.

        await asyncio.sleep(
            1.0
        )


# ============================================================
# Plot complete historical CSV data
# ============================================================

def plot_history():

    figure, axes = plt.subplots(
        3,
        1,
        figsize=(13, 10),
        sharex=True,
    )

    temperature_axis = (
        axes[0]
    )

    humidity_axis = (
        axes[1]
    )

    battery_axis = (
        axes[2]
    )

    anything_to_plot = False

    for (
        sensor_id,
        sensor,
    ) in SENSORS.items():

        environment = (
            read_environment_csv(
                environment_csv_path(
                    sensor
                )
            )
        )

        battery = (
            read_battery_csv(
                battery_csv_path(
                    sensor
                )
            )
        )

        # ----------------------------------------------------
        # Environment
        # ----------------------------------------------------

        if environment:

            anything_to_plot = True

            timestamps = [
                row["timestamp"]
                for row
                in environment
            ]

            temperatures = [
                row["temperature"]
                for row
                in environment
            ]

            humidities = [
                row["humidity"]
                for row
                in environment
            ]

            temperature_axis.plot(
                timestamps,
                temperatures,
                color=sensor["color"],
                label=sensor["label"],
                linewidth=1.2,
            )

            humidity_axis.plot(
                timestamps,
                humidities,
                color=sensor["color"],
                label=sensor["label"],
                linewidth=1.2,
            )

        # ----------------------------------------------------
        # Battery
        # ----------------------------------------------------

        if battery:

            anything_to_plot = True

            timestamps = [
                row["timestamp"]
                for row
                in battery
            ]

            voltages = [
                row["millivolts"]
                / 1000.0
                for row
                in battery
            ]

            battery_axis.plot(
                timestamps,
                voltages,
                color=sensor["color"],
                label=sensor["label"],
                linewidth=1.2,
                marker="o",
                markersize=3,
            )

    if not anything_to_plot:

        print(
            "No historical CSV data "
            "available to plot."
        )

        plt.close(
            figure
        )

        return

    # --------------------------------------------------------
    # Temperature
    # --------------------------------------------------------

    temperature_axis.set_ylabel(
        "Temperature (°C)"
    )

    temperature_axis.set_title(
        "Temperature"
    )

    temperature_axis.grid(
        True,
        alpha=0.3,
    )

    temperature_axis.legend()

    # --------------------------------------------------------
    # Humidity
    # --------------------------------------------------------

    humidity_axis.set_ylabel(
        "Relative humidity (%)"
    )

    humidity_axis.set_title(
        "Humidity"
    )

    humidity_axis.grid(
        True,
        alpha=0.3,
    )

    humidity_axis.legend()

    # --------------------------------------------------------
    # Battery
    # --------------------------------------------------------

    battery_axis.set_ylabel(
        "Battery (V)"
    )

    battery_axis.set_xlabel(
        "Local time"
    )

    battery_axis.set_title(
        "Battery voltage"
    )

    battery_axis.grid(
        True,
        alpha=0.3,
    )

    battery_axis.legend()

    # ========================================================
    # Date/time formatting
    #
    # IMPORTANT:
    #
    # CSV timestamps are timezone-aware, for example:
    #
    #   2026-09-23T19:23:30+02:00
    #
    # Matplotlib otherwise has a tendency to display these
    # using UTC.
    #
    # Explicitly tell both the locator and formatter to use
    # the computer's LOCAL timezone.
    # ========================================================

    plot_timezone = (
        local_timezone()
    )

    locator = (
        mdates.AutoDateLocator(
            tz=plot_timezone
        )
    )

    formatter = (
        mdates.ConciseDateFormatter(
            locator,
            tz=plot_timezone,
        )
    )

    battery_axis.xaxis.set_major_locator(
        locator
    )

    battery_axis.xaxis.set_major_formatter(
        formatter
    )

    # --------------------------------------------------------
    # Final layout
    # --------------------------------------------------------

    figure.suptitle(
        "VMC Environmental Sensor History"
    )

    figure.tight_layout()

    plt.show()


# ============================================================
# Main
# ============================================================

async def main():

    OUTPUT_DIRECTORY.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()

    print(
        "VMC BLE historical collector"
    )

    print(
        "Data directory:",
        OUTPUT_DIRECTORY.resolve(),
    )

    print(
        "Local timezone:",
        local_timezone(),
    )

    await collect_all_sensors()

    print()

    print(
        "Collection finished."
    )

    print(
        "Plotting complete "
        "historical CSV data..."
    )

    plot_history()


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print()
        print(
            "Stopped."
        )
