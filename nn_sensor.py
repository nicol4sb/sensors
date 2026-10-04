#!/usr/bin/env python3

import asyncio
import csv
import struct
import time
from datetime import datetime, timedelta

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from bleak import BleakClient, BleakScanner


# ============================================================
# SENSOR
# ============================================================

SENSOR_ID = 1
SENSOR_NAME = "NNTempHumidityRed"

COMPANY_ID = 0x0059

SCAN_TIMEOUT = 75.0
CONNECT_TIMEOUT = 15.0
STREAM_TIMEOUT = 30.0

CSV_FILENAME = "green_history.csv"
BATTERY_CSV_FILENAME = "green_battery.csv"


# ============================================================
# UUIDs
# ============================================================

META_UUID = "7a100001-4c7f-4f4d-432d-564d4353454e"

HISTORY_INDEX_UUID = "7a100003-4c7f-4f4d-432d-564d4353454e"
HISTORY_BLOCK_UUID = "7a100004-4c7f-4f4d-432d-564d4353454e"

BATTERY_INDEX_UUID = "7a100006-4c7f-4f4d-432d-564d4353454e"
BATTERY_BLOCK_UUID = "7a100007-4c7f-4f4d-432d-564d4353454e"


# ============================================================
# FIND SENSOR
# ============================================================

async def find_sensor():

    print()
    print(f"Scanning for {SENSOR_NAME} (ID {SENSOR_ID})...")

    def match(device, advertisement):

        data = advertisement.manufacturer_data.get(COMPANY_ID)

        if not data:
            return False

        if len(data) < 1:
            return False

        return data[0] == SENSOR_ID

    device = await BleakScanner.find_device_by_filter(
        match,
        timeout=SCAN_TIMEOUT,
    )

    if device is None:
        raise RuntimeError(
            f"Sensor ID {SENSOR_ID} not found "
            f"within {SCAN_TIMEOUT:.0f} seconds."
        )

    print(f"Found: {device.address}")

    return device


# ============================================================
# PARSERS
# ============================================================

def parse_metadata(data):

    if len(data) != 16:
        raise RuntimeError(
            f"Metadata should be 16 bytes; got {len(data)}."
        )

    return {
        "protocol_version": data[0],
        "sensor_id": data[1],
        "sample_interval": struct.unpack_from("<H", data, 2)[0],
        "history_count": struct.unpack_from("<H", data, 4)[0],
        "history_capacity": struct.unpack_from("<H", data, 6)[0],
        "newest_sequence": struct.unpack_from("<I", data, 8)[0],
        "battery_count": struct.unpack_from("<H", data, 12)[0],
        "battery_interval": struct.unpack_from("<H", data, 14)[0],
    }


def parse_environment(data, offset=0):

    sequence, temperature, humidity = struct.unpack_from(
        "<IhH",
        data,
        offset,
    )

    return {
        "sequence": sequence,
        "temperature": temperature / 100.0,
        "humidity": humidity / 100.0,
    }


def parse_battery(data, offset=0):

    sequence, millivolts = struct.unpack_from(
        "<IH",
        data,
        offset,
    )

    return {
        "sequence": sequence,
        "millivolts": millivolts,
        "voltage": millivolts / 1000.0,
    }


# ============================================================
# ENVIRONMENT STREAM
# ============================================================

async def download_environment(client, expected_count):

    if expected_count == 0:
        return [], 0.0

    samples = []
    complete = asyncio.Event()
    stream_error = None

    def handler(sender, data):

        nonlocal stream_error

        try:

            if len(data) < 4:
                raise RuntimeError(
                    "Environment notification too short."
                )

            start_index, count = struct.unpack_from(
                "<HH",
                data,
                0,
            )

            required_length = 4 + count * 8

            if len(data) != required_length:
                raise RuntimeError(
                    "Environment packet length error: "
                    f"expected {required_length}, "
                    f"received {len(data)}."
                )

            if start_index != len(samples):
                raise RuntimeError(
                    "Environment stream out of sequence: "
                    f"expected index {len(samples)}, "
                    f"received {start_index}."
                )

            offset = 4

            for _ in range(count):

                samples.append(
                    parse_environment(
                        data,
                        offset,
                    )
                )

                offset += 8

            print(
                f"\rEnvironment: "
                f"{len(samples)}/{expected_count}",
                end="",
                flush=True,
            )

            if len(samples) >= expected_count:
                complete.set()

        except Exception as exc:

            stream_error = exc
            complete.set()

    print()
    print("Subscribing to environmental notifications...")

    await client.start_notify(
        HISTORY_BLOCK_UUID,
        handler,
    )

    print("Subscription active.")
    print("Requesting environmental stream...")

    start_time = time.monotonic()

    try:

        # One write starts the entire stream.
        await client.write_gatt_char(
            HISTORY_INDEX_UUID,
            struct.pack("<H", 0),
            response=True,
        )

        await asyncio.wait_for(
            complete.wait(),
            timeout=STREAM_TIMEOUT,
        )

    except asyncio.TimeoutError:

        raise RuntimeError(
            "Environment stream timed out. "
            f"Received {len(samples)}/{expected_count} samples."
        )

    finally:

        try:
            await client.stop_notify(
                HISTORY_BLOCK_UUID
            )
        except Exception:
            pass

    elapsed = time.monotonic() - start_time

    print()

    if stream_error is not None:
        raise stream_error

    if len(samples) != expected_count:
        raise RuntimeError(
            "Environment stream incomplete: "
            f"{len(samples)}/{expected_count} samples."
        )

    return samples, elapsed


# ============================================================
# BATTERY STREAM
# ============================================================

async def download_battery(client, expected_count):

    if expected_count == 0:
        return [], 0.0

    samples = []
    complete = asyncio.Event()
    stream_error = None

    def handler(sender, data):

        nonlocal stream_error

        try:

            if len(data) < 4:
                raise RuntimeError(
                    "Battery notification too short."
                )

            start_index, count = struct.unpack_from(
                "<HH",
                data,
                0,
            )

            required_length = 4 + count * 6

            if len(data) != required_length:
                raise RuntimeError(
                    "Battery packet length error: "
                    f"expected {required_length}, "
                    f"received {len(data)}."
                )

            if start_index != len(samples):
                raise RuntimeError(
                    "Battery stream out of sequence: "
                    f"expected index {len(samples)}, "
                    f"received {start_index}."
                )

            offset = 4

            for _ in range(count):

                samples.append(
                    parse_battery(
                        data,
                        offset,
                    )
                )

                offset += 6

            print(
                f"\rBattery: "
                f"{len(samples)}/{expected_count}",
                end="",
                flush=True,
            )

            if len(samples) >= expected_count:
                complete.set()

        except Exception as exc:

            stream_error = exc
            complete.set()

    print()
    print("Subscribing to battery notifications...")

    await client.start_notify(
        BATTERY_BLOCK_UUID,
        handler,
    )

    print("Subscription active.")
    print("Requesting battery stream...")

    start_time = time.monotonic()

    try:

        await client.write_gatt_char(
            BATTERY_INDEX_UUID,
            struct.pack("<H", 0),
            response=True,
        )

        await asyncio.wait_for(
            complete.wait(),
            timeout=STREAM_TIMEOUT,
        )

    except asyncio.TimeoutError:

        raise RuntimeError(
            "Battery stream timed out. "
            f"Received {len(samples)}/{expected_count} samples."
        )

    finally:

        try:
            await client.stop_notify(
                BATTERY_BLOCK_UUID
            )
        except Exception:
            pass

    elapsed = time.monotonic() - start_time

    print()

    if stream_error is not None:
        raise stream_error

    if len(samples) != expected_count:
        raise RuntimeError(
            "Battery stream incomplete: "
            f"{len(samples)}/{expected_count} samples."
        )

    return samples, elapsed


# ============================================================
# TIMESTAMPS
# ============================================================

def add_timestamps(environment, batteries, interval):

    if not environment:
        return

    newest_time = datetime.now()
    newest_sequence = environment[-1]["sequence"]

    for sample in environment:

        sequence_difference = (
            newest_sequence - sample["sequence"]
        )

        sample["timestamp"] = (
            newest_time
            - timedelta(
                seconds=sequence_difference * interval
            )
        )

    for sample in batteries:

        sequence_difference = (
            newest_sequence - sample["sequence"]
        )

        sample["timestamp"] = (
            newest_time
            - timedelta(
                seconds=sequence_difference * interval
            )
        )


# ============================================================
# BATTERY %
# ============================================================

def battery_percent(voltage):

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
                (voltage - v1)
                / (v2 - v1)
            )

            return p1 + fraction * (p2 - p1)

    return 0.0


# ============================================================
# CSV
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

        for sample in samples:

            writer.writerow([
                sample["timestamp"].isoformat(
                    timespec="seconds"
                ),
                sample["sequence"],
                f'{sample["temperature"]:.2f}',
                f'{sample["humidity"]:.2f}',
            ])

    print(f"Saved {CSV_FILENAME}")


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

        for sample in samples:

            writer.writerow([
                sample["timestamp"].isoformat(
                    timespec="seconds"
                ),
                sample["sequence"],
                f'{sample["voltage"]:.3f}',
                sample["millivolts"],
                f'{battery_percent(sample["voltage"]):.1f}',
            ])

    print(f"Saved {BATTERY_CSV_FILENAME}")


# ============================================================
# GRAPH
# ============================================================

def show_graph(environment, batteries):

    if not environment:
        return

    times = [
        sample["timestamp"]
        for sample in environment
    ]

    temperatures = [
        sample["temperature"]
        for sample in environment
    ]

    humidities = [
        sample["humidity"]
        for sample in environment
    ]

    battery_times = [
        sample["timestamp"]
        for sample in batteries
    ]

    voltages = [
        sample["voltage"]
        for sample in batteries
    ]

    fig, axes = plt.subplots(
        3,
        1,
        figsize=(13, 10),
        sharex=True,
    )

    fig.suptitle(
        "Green sensor history"
    )

    axes[0].plot(
        times,
        temperatures,
        marker=".",
        color="green",
    )

    axes[0].set_ylabel("°C")
    axes[0].set_title("Temperature")
    axes[0].grid(True, alpha=0.3)

    axes[1].plot(
        times,
        humidities,
        marker=".",
        color="green",
    )

    axes[1].set_ylabel("% RH")
    axes[1].set_title("Relative humidity")
    axes[1].grid(True, alpha=0.3)

    if batteries:

        axes[2].plot(
            battery_times,
            voltages,
            marker="o",
            markersize=4,
            color="green",
        )

    axes[2].set_ylabel("Volts")
    axes[2].set_xlabel("Time")
    axes[2].set_title("Battery voltage")
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

    plt.show()


# ============================================================
# MAIN
# ============================================================

async def main():

    print()
    print("======================================")
    print("VMC — Green streaming test")
    print("======================================")

    device = await find_sensor()

    print()
    print("Connecting...")

    async with BleakClient(
        device,
        timeout=CONNECT_TIMEOUT,
    ) as client:

        print("Connected.")

        # ----------------------------------------------------
        # METADATA ONLY
        # ----------------------------------------------------

        raw_metadata = await client.read_gatt_char(
            META_UUID
        )

        meta = parse_metadata(
            raw_metadata
        )

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
            f'Newest sequence  : '
            f'{meta["newest_sequence"]}'
        )

        print(
            f'Battery samples  : '
            f'{meta["battery_count"]}'
        )

        if meta["protocol_version"] != 3:
            raise RuntimeError(
                "This program requires protocol v3. "
                f"Sensor reports v"
                f'{meta["protocol_version"]}.'
            )

        if meta["sensor_id"] != SENSOR_ID:
            raise RuntimeError(
                f"Expected sensor ID {SENSOR_ID}; "
                f"sensor reports "
                f'{meta["sensor_id"]}.'
            )

        # ----------------------------------------------------
        # ENVIRONMENT STREAM
        # ----------------------------------------------------

        environment, env_time = (
            await download_environment(
                client,
                meta["history_count"],
            )
        )

        # ----------------------------------------------------
        # BATTERY STREAM
        # ----------------------------------------------------

        batteries, battery_time = (
            await download_battery(
                client,
                meta["battery_count"],
            )
        )

    # ========================================================
    # DISCONNECTED
    # ========================================================

    print()
    print("Disconnected.")

    # ========================================================
    # SHOW NEWEST VALUES FROM STREAM
    # ========================================================

    if environment:

        current = environment[-1]

        print()
        print("Newest environmental sample")
        print("---------------------------")

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

    if batteries:

        current_battery = batteries[-1]

        print()
        print("Newest battery sample")
        print("---------------------")

        print(
            f'Sequence : '
            f'{current_battery["sequence"]}'
        )

        print(
            f'Voltage  : '
            f'{current_battery["voltage"]:.3f} V'
        )

        print(
            f'Estimated: '
            f'{battery_percent(current_battery["voltage"]):.0f} %'
        )

    # ========================================================
    # PERFORMANCE
    # ========================================================

    print()
    print("Transfer performance")
    print("--------------------")

    print(
        f"Environment : "
        f"{len(environment)} samples "
        f"in {env_time:.3f} seconds"
    )

    print(
        f"Battery     : "
        f"{len(batteries)} samples "
        f"in {battery_time:.3f} seconds"
    )

    # ========================================================
    # TIMESTAMPS
    # ========================================================

    add_timestamps(
        environment,
        batteries,
        meta["sample_interval"],
    )

    # ========================================================
    # SAVE
    # ========================================================

    save_environment_csv(
        environment
    )

    if batteries:

        save_battery_csv(
            batteries
        )

    # ========================================================
    # GRAPH
    # ========================================================

    show_graph(
        environment,
        batteries
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print()
        print("Stopped.")

    except Exception as error:

        print()
        print()
        print("ERROR")
        print("-----")
        print(
            f"Type    : "
            f"{type(error).__name__}"
        )
        print(
            f"Details : "
            f"{repr(error)}"
        )