#!/usr/bin/env python3
"""Generate full-resolution synthetic histories, backing up every existing CSV.

Defaults to five calendar years at ten-minute intervals. Seed and end time can
be supplied to reproduce the exact dataset. No production data is downsampled.
"""
import argparse
import csv
import json
import math
from pathlib import Path
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FIELDS = ['timestamp', 'sequence', 'temperature_c', 'humidity_percent', 'battery_mv', 'rssi']
ZONE = ZoneInfo('Europe/Zurich')
SENSORS = (
    # file, temperature baseline, humidity baseline, daily swing, season, battery life, last charge
    ('red', 21.3, 47, 0.9, 2.6, 64, 12),
    ('green', 20.1, 52, 1.2, 3.2, 83, 26),
    ('sensor3', 23.0, 44, 0.7, 2.2, 51, 41),
)


def clamp(value, low, high):
    return max(low, min(high, value))


def voltage_from_percent(percent):
    curve = [(0, 3.30), (10, 3.46), (20, 3.58), (30, 3.63),
             (50, 3.66), (70, 3.84), (80, 3.94), (90, 4.05), (100, 4.20)]
    for (p1, v1), (p2, v2) in zip(curve, curve[1:]):
        if percent <= p2:
            return v1 + (v2 - v1) * (percent - p1) / (p2 - p1)
    return 4.20


def build_weather(start, days, seed):
    """Shared daily weather and multi-day seasonal extremes, with slow changes."""
    rng = random.Random(seed)
    weather = []
    state = 0.0
    for day in range(days + 2):
        state = .82 * state + rng.gauss(0, .45)
        weather.append(state)
    episodes = []
    for year in range(start.year, (start + timedelta(days=days + 1)).year + 1):
        for summer in (True, False):
            center = datetime(year, rng.choice((7, 8) if summer else (1, 2)),
                              rng.randint(6, 24), 12, tzinfo=timezone.utc)
            episodes.append(((center - start).total_seconds() / 86400,
                             rng.uniform(3, 6), rng.uniform(2, 4) * (1 if summer else -1)))
    return weather, episodes


def generate(path, sensor_index, start, end, seed, weather, episodes):
    name, baseline, humidity_base, swing, season_swing, battery_days, last_charge = SENSORS[sensor_index]
    rng = random.Random(seed + 1000 + sensor_index)
    total_days = (end - start).total_seconds() / 86400
    count = int((end - start).total_seconds() // 600) + 1
    # Sparse room-specific shower/cooking/window events; Gaussian pulses avoid jumps.
    events = {}
    for day in range(math.ceil(total_days) + 1):
        if rng.random() < .65:
            humid = rng.random() < .7
            events[day] = (rng.uniform(0, 24), rng.uniform(.15, .55),
                           rng.uniform(.3, 1.4) if humid else -rng.uniform(1.5, 3.5),
                           rng.uniform(10, 22) if humid else -rng.uniform(4, 10))
    # Guaranteed recent examples make 1h/24h/7d views useful too.
    recent_events = [(total_days - 2 / 24, .25 / 24, .8, 16),
                     (total_days - 3.3, .4 / 24, -2.5, -7)]
    temp_noise = humidity_noise = radio_noise = 0.0
    minimum_temp, maximum_temp = math.inf, -math.inf
    minimum_humidity, maximum_humidity = math.inf, -math.inf
    full_count = 0
    with path.open('w', newline='') as output:
        writer = csv.writer(output)
        writer.writerow(FIELDS)
        for index in range(count):
            instant = start + timedelta(minutes=index * 10)
            local = instant.astimezone(ZONE)
            day = index / 144
            day_index = int(day)
            fraction = day - day_index
            hour = local.hour + local.minute / 60
            year_start = datetime(local.year, 1, 1, tzinfo=ZONE)
            next_year = datetime(local.year + 1, 1, 1, tzinfo=ZONE)
            year_fraction = (local - year_start).total_seconds() / (next_year - year_start).total_seconds()
            # Annual maximum in late July, minimum in late January.
            season = math.cos(2 * math.pi * (year_fraction - .56))
            daily = math.sin(2 * math.pi * (hour - 9) / 24)
            front = weather[day_index] * (1 - fraction) + weather[day_index + 1] * fraction
            extreme = sum(amplitude * math.exp(-.5 * ((day - center) / width) ** 2)
                          for center, width, amplitude in episodes if abs(day - center) < width * 4)
            temp_noise = .85 * temp_noise + rng.gauss(0, .035)
            humidity_noise = .88 * humidity_noise + rng.gauss(0, .14)
            temperature = baseline + season_swing * season + swing * daily + .5 * front + .65 * extreme + temp_noise
            humidity = humidity_base + 8 * season - 2.2 * daily + 1.6 * front - .8 * extreme + humidity_noise
            for adjacent in (day_index - 1, day_index, day_index + 1):
                event = events.get(adjacent)
                if event:
                    center_hour, width_hour, temp_peak, humidity_peak = event
                    pulse = math.exp(-.5 * ((day - adjacent - center_hour / 24) / (width_hour / 24)) ** 2)
                    temperature += temp_peak * pulse
                    humidity += humidity_peak * pulse
            for center, width, temp_peak, humidity_peak in recent_events:
                if abs(day - center) < width * 5:
                    pulse = math.exp(-.5 * ((day - center) / width) ** 2)
                    temperature += temp_peak * pulse
                    humidity += humidity_peak * pulse
            humidity = clamp(humidity, 18, 88)
            # Recharge for ~2 hours, hold full for ~1 hour, then discharge slowly.
            age = (day - total_days + last_charge) % battery_days
            if age < .05:
                percent = 100
            elif age > battery_days - .09:
                percent = 20 + 80 * (age - (battery_days - .09)) / .09
            else:
                percent = 100 - 80 * (age - .05) / (battery_days - .14)
            battery = round(clamp(voltage_from_percent(percent) * 1000 + rng.gauss(0, 2), 3300, 4200))
            full_count += battery >= 4170
            radio_noise = .75 * radio_noise + rng.gauss(0, 1.4)
            rssi = round(clamp((-66, -61, -73)[sensor_index] + radio_noise
                               - (rng.uniform(8, 18) if rng.random() < .002 else 0), -96, -40))
            writer.writerow([local.isoformat(), index % 65536,
                             f'{temperature:.2f}', f'{humidity:.2f}', battery, rssi])
            minimum_temp, maximum_temp = min(minimum_temp, temperature), max(maximum_temp, temperature)
            minimum_humidity, maximum_humidity = min(minimum_humidity, humidity), max(maximum_humidity, humidity)
    return dict(file=f'{name}.csv', readings=count, temperature_range=[round(minimum_temp, 2), round(maximum_temp, 2)],
                humidity_range=[round(minimum_humidity, 2), round(maximum_humidity, 2)],
                full_battery_readings=full_count, local_events=len(events) + len(recent_events))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years', type=int, default=5)
    parser.add_argument('--seed', type=int, default=20261101)
    parser.add_argument('--end', help='ISO timestamp with timezone; defaults to current UTC minute')
    parser.add_argument('--directory', type=Path, default=Path(__file__).resolve().parents[1] / 'vmc_data')
    args = parser.parse_args()
    if not 1 <= args.years <= 50:
        parser.error('--years must be between 1 and 50')
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)
    if end.tzinfo is None:
        parser.error('--end must include a timezone')
    end = end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    try:
        start = end.replace(year=end.year - args.years)
    except ValueError:  # February 29 in a non-leap starting year.
        start = end.replace(year=end.year - args.years, day=28)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / datetime.now().strftime('backup_before_regeneration_%Y%m%d_%H%M%S_%f')
    backup.mkdir()
    for name, *_ in SENSORS:
        original = directory / f'{name}.csv'
        if original.exists():
            shutil.copy2(original, backup / original.name)
    if (directory / 'demo_manifest.json').exists():
        shutil.copy2(directory / 'demo_manifest.json', backup / 'demo_manifest.json')
    print(f'Original data backed up in {backup}', flush=True)
    weather, episodes = build_weather(start, math.ceil((end - start).total_seconds() / 86400), args.seed)
    summaries = []
    # Generate all files successfully before replacing any current history.
    with tempfile.TemporaryDirectory(prefix='.demo_generation_', dir=directory) as temporary:
        staging = Path(temporary)
        for sensor_index, (name, *_) in enumerate(SENSORS):
            summary = generate(staging / f'{name}.csv', sensor_index, start, end, args.seed, weather, episodes)
            summaries.append(summary)
            print(json.dumps(summary), flush=True)
        manifest = dict(synthetic=True, seed=args.seed, start=start.isoformat(), end=end.isoformat(),
                        timezone='Europe/Zurich', interval_seconds=600, sensors=summaries,
                        backup=str(backup), model='Indoor seasonal/weather/daily cycles, local pulses, battery recharge cycles')
        (staging / 'demo_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        for name in [f'{sensor[0]}.csv' for sensor in SENSORS] + ['demo_manifest.json']:
            (staging / name).replace(directory / name)
    print('Five-year demo data installed; all full-resolution readings retained.' if args.years == 5
          else 'Demo data installed; all full-resolution readings retained.')


if __name__ == '__main__':
    main()
