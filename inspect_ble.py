#!/usr/bin/env python3

import asyncio

from bleak import BleakClient, BleakScanner


TARGET_ADDRESS = "EB:87:C8:D3:20:16"

SCAN_TIMEOUT = 75.0
CONNECT_TIMEOUT = 15.0
MAX_ATTEMPTS = 5


async def find_target():

    print(
        f"Waiting for {TARGET_ADDRESS}..."
    )

    def match(device, advertisement):

        return (
            device.address.upper()
            == TARGET_ADDRESS.upper()
        )

    return await BleakScanner.find_device_by_filter(
        match,
        timeout=SCAN_TIMEOUT,
    )


async def inspect_once(attempt):

    print()
    print(
        f"=== Attempt "
        f"{attempt}/{MAX_ATTEMPTS} ==="
    )

    device = await find_target()

    if device is None:
        raise RuntimeError(
            "Target sensor not found."
        )

    print(
        f"Found: {device.address}"
    )

    print("Connecting...")

    async with BleakClient(
        device,
        timeout=CONNECT_TIMEOUT,
    ) as client:

        print("Connected.")
        print()
        print("GATT database")
        print("=============")

        for service in client.services:

            print()
            print(
                f"SERVICE {service.uuid}"
            )

            for characteristic in service.characteristics:

                print()
                print(
                    f"  {characteristic.uuid}"
                )

                print(
                    "    properties:",
                    characteristic.properties,
                )

                for descriptor in characteristic.descriptors:

                    print(
                        "    descriptor:",
                        descriptor.uuid,
                    )

        print()
        print(
            "Inspection complete."
        )


async def main():

    for attempt in range(
        1,
        MAX_ATTEMPTS + 1,
    ):

        try:

            await inspect_once(
                attempt
            )

            return

        except Exception as error:

            print()
            print(
                "Attempt failed:"
            )

            print(
                f"  {type(error).__name__}: "
                f"{error}"
            )

            if attempt < MAX_ATTEMPTS:

                print(
                    "Waiting for another "
                    "advertisement..."
                )

                await asyncio.sleep(1)

    print()
    print(
        "All attempts failed."
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )