#!/usr/bin/env python3

import asyncio
import csv
import struct
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from bleak import BleakClient, BleakScanner


# ============================================================
# CONFIGURATION
# ============================================================

SENSOR_ID = 1
SENSOR_NAME = "NNTempHumidity1"

COMPANY_ID = 0x0059

SCAN_TIMEOUT = 70.0

CSV_FILENAME = "NNTempHumidity1_history.csv"
BATTERY_CSV_FILENAME = "NNTempHumidity1_battery.csv"


# ============================================================
# UUIDs
# ============================================================

META_UUID = \
    "7a100001-4c7f-4f4d-432d-564d4353454e"

CURRENT_UUID = \
    "7a100002-4c7f-4f4d-432d-564d4353454e"

HISTORY_INDEX_UUID = \
    "7a100003-4c7f-4f4d-432d-564d4353454e"

HISTORY_BLOCK_UUID = \
    "7a100004-4c7f-4f4d-432d-564d4353454e"

BATTERY_CURRENT_UUID = \
    "7a100005-4c7f-4f4d-432d-564d4353454e"

BATTERY_INDEX_UUID = \
    "7a100006-4c7f-4f4d-432d-564d4353454e"

BATTERY_BLOCK_UUID = \
    "7a100007-4c7f-4f4d-432d-564d4353454e"


# ============================================================
# FIND SENSOR
# ============================================================

async def find_sensor():

    print()
    print(f"Scanning for {SENSOR_NAME}...")
    print(
        f"Manufacturer 0x{COMPANY_ID:04X}, "
        f"sensor ID {SENSOR_ID}"
    )

    print(
        "The sensor advertises for 2 seconds "
        "once per minute."
    )

    def match(device, advertisement):

        manufacturer_data = (
            advertisement.manufacturer_data
        )

        if COMPANY_ID not in manufacturer_data:
            return False

        data = manufacturer_data[COMPANY_ID]

        if len(data) < 1:
            return False

        if data[0] != SENSOR_ID:
            return False

        print()
        print("Sensor found!")
        print(f"  Address   : {device.address}")
        print(f"  BLE name  : {advertisement.local_name}")
        print(f"  Sensor ID : {data[0]}")

        return True

    device = await BleakScanner.find_device_by_filter(
        match,
        timeout=SCAN_TIMEOUT,
    )

    if device is None:
        raise RuntimeError(
            f"Sensor ID {SENSOR_ID} was not found "
            f"within {SCAN_TIMEOUT:.0f} seconds."
        )

    return device


# ============================================================
# METADATA
# ============================================================

def parse_metadata(data):

    if len(data) < 16:
        raise RuntimeError(
            f"Expected 16 metadata bytes, got {len(data)}."
        )

    return {
        "protocol_version": data[0],
        "sensor_id": data[1],

        "sample_interval":
            struct.unpack_from("<H", data, 2)[0],

        "history_count":
            struct.unpack_from("<H", data, 4)[0],

        "history_capacity":
            struct.unpack_from("<H", data, 6)[0],

        "newest_sequence":
            struct.unpack_from("<I", data, 8)[0],

        "battery_count":
            struct.unpack_from("<H", data, 12)[0],

        "battery_interval":
            struct.unpack_from("<H", data, 14)[0],
    }


# ============================================================
# ENVIRONMENT SAMPLE
# ============================================================

def parse_sample(data, offset=0):

    seq, temp_raw, humidity_raw = struct.unpack_from(
        "<IhH",
        data,
        offset,
    )

    return {
        "sequence": seq,
        "temperature": temp_raw / 100.0,
        "humidity": humidity_raw / 100.0,
    }


# ============================================================
# BATTERY SAMPLE
# ============================================================

def parse_battery(data, offset=0):

    seq, millivolts = struct.unpack_from(
        "<IH",
        data,
        offset,
    )

    return {
        "sequence": seq,
        "millivolts": millivolts,
        "voltage": millivolts / 1000.0,
    }


# ============================================================
# ENVIRONMENT HISTORY BLOCK
# ============================================================

async def read_history_block(client, start_index):

    await client.write_gatt_char(
        HISTORY_INDEX_UUID,
        struct.pack("<H", start_index),
        response=True,
    )

    await asyncio.sleep(0.05)

    data = await client.read_gatt_char(
        HISTORY_BLOCK_UUID
    )

    if len(data) < 4:
        raise RuntimeError(
            "Environment history response too short."
        )

    returned_start, count = struct.unpack_from(
        "<HH",
        data,
        0,
    )

    expected = 4 + count * 8

    if len(data) < expected:
        raise RuntimeError(
            f"Incomplete environment history block: "
            f"expected {expected}, got {len(data)}."
        )

    samples = []

    offset = 4

    for _ in range(count):

        samples.append(
            parse_sample(data, offset)
        )

        offset += 8

    return returned_start, samples


# ============================================================
# BATTERY HISTORY BLOCK
# ============================================================

async def read_battery_block(client, start_index):

    await client.write_gatt_char(
        BATTERY_INDEX_UUID,
        struct.pack("<H", start_index),
        response=True,
    )

    await asyncio.sleep(0.05)

    data = await client.read_gatt_char(
        BATTERY_BLOCK_UUID
    )

    if len(data) < 4:
        raise RuntimeError(
            "Battery history response too short."
        )

    returned_start, count = struct.unpack_from(
        "<HH",
        data,
        0,
    )

    expected = 4 + count * 6

    if len(data) < expected:
        raise RuntimeError(
            f"Incomplete battery history block: "
            f"expected {expected}, got {len(data)}."
        )

    samples = []

    offset = 4

    for _ in range(count):

        samples.append(
            parse_battery(data, offset)
        )

        offset += 6

    return returned_start, samples


# ============================================================
# DOWNLOAD ENVIRONMENT HISTORY
# ============================================================

async def download_history(client, count):

    print()
    print(f"Downloading {count} environmental samples...")

    samples = []

    index = 0

    while index < count:

        returned_start, block = (
            await read_history_block(
                client,
                index,
            )
        )

        if returned_start != index:
            raise RuntimeError(
                f"Requested environment index {index}, "
                f"sensor returned {returned_start}."
            )

        if not block:
            break

        samples.extend(block)

        index += len(block)

        print(
            f"\r  {len(samples)}/{count}",
            end="",
            flush=True,
        )

    print()

    return samples[:count]


# ============================================================
# DOWNLOAD BATTERY HISTORY
# ============================================================

async def download_battery_history(client, count):

    print()
    print(f"Downloading {count} battery samples...")

    samples = []

    index = 0

    while index < count:

        returned_start, block = (
            await read_battery_block(
                client,
                index,
            )
        )

        if returned_start != index:
            raise RuntimeError(
                f"Requested battery index {index}, "
                f"sensor returned {returned_start}."
            )

        if not block:
            break

        samples.extend(block)

        index += len(block)

        print(
            f"\r  {len(samples)}/{count}",
            end="",
            flush=True,
        )

    print()

    return samples[:count]


# ============================================================
# TIMESTAMPS
# ============================================================

def add_timestamps(
    environment,
    batteries,
    sample_interval,
):

    if not environment:
        return

    newest_time = datetime.now()

    newest_sequence = (
        environment[-1]["sequence"]
    )

    for sample in environment:

        delta_sequences = (
            newest_sequence -
            sample["sequence"]
        )

        sample["timestamp"] = (
            newest_time -
            timedelta(
                seconds=(
                    delta_sequences *
                    sample_interval
                )
            )
        )

    # Battery samples contain the environmental sequence number
    # at which the battery reading was made, so they can use
    # exactly the same time base.

    for battery in batteries:

        delta_sequences = (
            newest_sequence -
            battery["sequence"]
        )

        battery["timestamp"] = (
            newest_time -
            timedelta(
                seconds=(
                    delta_sequences *
                    sample_interval
                )
            )
        )


# ============================================================
# APPROXIMATE LIPO %
# ============================================================

def battery_percent(voltage):

    # Approximate resting single-cell LiPo curve.
    #
    # Voltage remains the useful measured quantity.
    # Percentage is deliberately only an estimate.

    curve = [
        (3.20, 0),
        (3.50, 5),
        (3.60, 10),
        (3.70, 20),
        (3.75, 30),
        (3.79, 40),
        (3.83, 50),
        (3.87, 60),
        (3.92, 70),
        (3.98, 80),
        (4.06, 90),
        (4.20, 100),
    ]

    if voltage <= curve[0][0]:
        return 0.0

    if voltage >= curve[-1][0]:
        return 100.0

    for i in range(len(curve) - 1):

        v1, p1 = curve[i]
        v2, p2 = curve[i + 1]

        if v1 <= voltage <= v2:

            fraction = (
                (voltage - v1) /
                (v2 - v1)
            )

            return (
                p1 +
                fraction * (p2 - p1)
            )

    return 0.0


# ============================================================
# SAVE ENVIRONMENT CSV
# ============================================================

def save_environment_csv(samples):

    with open(
        CSV_FILENAME,
        "w",
        newline="",
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "timestamp",
            "sequence",
            "temperature_c",
            "humidity_percent",
        ])

        for s in samples:

            writer.writerow([
                s["timestamp"].isoformat(
                    timespec="seconds"
                ),
                s["sequence"],
                f'{s["temperature"]:.2f}',
                f'{s["humidity"]:.2f}',
            ])

    print(f"Saved {CSV_FILENAME}")


# ============================================================
# SAVE BATTERY CSV
# ============================================================

def save_battery_csv(samples):

    with open(
        BATTERY_CSV_FILENAME,
        "w",
        newline="",
    ) as file:

        writer = csv.writer(file)

        writer.writerow([
            "timestamp",
            "sequence",
            "battery_voltage",
            "battery_millivolts",
            "estimated_percent",
        ])

        for s in samples:

            percent = battery_percent(
                s["voltage"]
            )

            writer.writerow([
                s["timestamp"].isoformat(
                    timespec="seconds"
                ),
                s["sequence"],
                f'{s["voltage"]:.3f}',
                s["millivolts"],
                f"{percent:.1f}",
            ])

    print(f"Saved {BATTERY_CSV_FILENAME}")


# ============================================================
# GRAPH
# ============================================================

def show_graph(environment, batteries):

    if not environment:
        return

    env_times = [
        s["timestamp"]
        for s in environment
    ]

    temperatures = [
        s["temperature"]
        for s in environment
    ]

    humidities = [
        s["humidity"]
        for s in environment
    ]

    battery_times = [
        s["timestamp"]
        for s in batteries
    ]

    voltages = [
        s["voltage"]
        for s in batteries
    ]

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(13, 10),
        sharex=True,
    )

    fig.suptitle(
        f"{SENSOR_NAME} — 24-hour history"
    )

    # --------------------------------------------------------
    # Temperature
    # --------------------------------------------------------

    axes[0].plot(
        env_times,
        temperatures,
        marker=".",
    )

    axes[0].set_ylabel("°C")
    axes[0].set_title("Temperature")
    axes[0].grid(True, alpha=0.3)

    # --------------------------------------------------------
    # Humidity
    # --------------------------------------------------------

    axes[1].plot(
        env_times,
        humidities,
        marker=".",
    )

    axes[1].set_ylabel("% RH")
    axes[1].set_title("Relative humidity")
    axes[1].grid(True, alpha=0.3)

    # --------------------------------------------------------
    # Battery
    # --------------------------------------------------------

    if batteries:

        axes[2].plot(
            battery_times,
            voltages,
            marker="o",
            markersize=4,
        )

    axes[2].set_ylabel("Volts")
    axes[2].set_title("Battery voltage")
    axes[2].set_xlabel("Time")
    axes[2].grid(True, alpha=0.3)

    locator = mdates.AutoDateLocator()

    formatter = mdates.ConciseDateFormatter(
        locator
    )

    axes[2].xaxis.set_major_locator(
        locator
    )

    axes[2].xaxis.set_major_formatter(
        formatter
    )

    fig.tight_layout()

    print()
    print("Opening graph...")

    plt.show()


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print("======================================")
    print("NN Temperature / Humidity / Battery")
    print("======================================")

    device = await find_sensor()

    print()
    print("Connecting...")

    async with BleakClient(
        device,
        timeout=15.0,
    ) as client:

        print("Connected.")

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        raw = await client.read_gatt_char(
            META_UUID
        )

        meta = parse_metadata(raw)

        print()
        print("Sensor information")
        print("------------------")
        print(
            f'Protocol version : '
            f'{meta["protocol_version"]}'
        )
        print(
            f'Sensor ID        : '
            f'{meta["sensor_id"]}'
        )
        print(
            f'Sample interval  : '
            f'{meta["sample_interval"]} seconds'
        )
        print(
            f'Environment      : '
            f'{meta["history_count"]}/'
            f'{meta["history_capacity"]}'
        )
        print(
            f'Battery samples  : '
            f'{meta["battery_count"]}'
        )
        print(
            f'Battery interval : '
            f'{meta["battery_interval"]} seconds'
        )

        # ----------------------------------------------------
        # Current environment
        # ----------------------------------------------------

        raw_current = (
            await client.read_gatt_char(
                CURRENT_UUID
            )
        )

        current = parse_sample(
            raw_current
        )

        print()
        print("Current measurement")
        print("-------------------")
        print(
            f'Sequence    : '
            f'{current["sequence"]}'
        )
        print(
            f'Temperature : '
            f'{current["temperature"]:.2f} °C'
        )
        print(
            f'Humidity    : '
            f'{current["humidity"]:.2f} %'
        )

        # ----------------------------------------------------
        # Current battery
        # ----------------------------------------------------

        raw_battery = (
            await client.read_gatt_char(
                BATTERY_CURRENT_UUID
            )
        )

        battery = parse_battery(
            raw_battery
        )

        percent = battery_percent(
            battery["voltage"]
        )

        print()
        print("Battery")
        print("-------")
        print(
            f'Voltage     : '
            f'{battery["voltage"]:.3f} V'
        )
        print(
            f'Estimated   : '
            f'{percent:.0f} %'
        )
        print(
            f'Measured at : '
            f'sequence #{battery["sequence"]}'
        )

        # ----------------------------------------------------
        # Download histories
        # ----------------------------------------------------

        environment = (
            await download_history(
                client,
                meta["history_count"],
            )
        )

        batteries = (
            await download_battery_history(
                client,
                meta["battery_count"],
            )
        )

    print()
    print("Disconnected.")

    # --------------------------------------------------------
    # Timestamps
    # --------------------------------------------------------

    add_timestamps(
        environment,
        batteries,
        meta["sample_interval"],
    )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print()
    print(
        f"Received {len(environment)} "
        f"environment samples."
    )

    print(
        f"Received {len(batteries)} "
        f"battery samples."
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    save_environment_csv(
        environment
    )

    if batteries:
        save_battery_csv(
            batteries
        )

    # --------------------------------------------------------
    # Graph
    # --------------------------------------------------------

    show_graph(
        environment,
        batteries
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        print()
        print("Stopped.")

    except Exception as error:

        print()
        print("ERROR:")
        print(error)

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print()
        print("Stopped.")

    except Exception as error:
        print()
        print("ERROR:")
        print(f"Type    : {type(error).__name__}")
        print(f"Details : {repr(error)}")
        raise
