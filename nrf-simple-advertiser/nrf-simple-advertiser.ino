#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <bluefruit.h>
#include <math.h>

#include "nrf_soc.h"

// ============================================================
// VMC RED SENSOR - LOW POWER TEST
//
// Every 10 seconds:
//   wake
//   read SHT40
//   read battery
//   advertise for 1 second
//   System-ON sleep
//
// No Serial
// No LEDs
// No history
// No BLE connection
//
// Once validated:
//   - shorten advertising burst
//   - change 10 seconds -> 10 minutes
// ============================================================

#define SENSOR_ID   2
#define SENSOR_NAME "VMC-Sensor-Green"

static const uint32_t MEASUREMENT_INTERVAL_MS = 10000UL;


// ============================================================
// SENSOR
// ============================================================

Adafruit_SHT4x sht4;


// ============================================================
// BLE PACKET
//
// Bleak receives 12 bytes as:
//
// manufacturer_data[0x0059]
//
// Python:
// struct.unpack("<BBIhHH", data)
// ============================================================

struct __attribute__((packed)) SensorPacket
{
  uint8_t protocolVersion;
  uint8_t sensorId;

  uint32_t sequence;

  int16_t temperature;
  uint16_t humidity;

  uint16_t batteryMillivolts;
};


// ============================================================
// STATE
// ============================================================

uint32_t sequenceNumber = 0;

uint32_t lastMeasurement = 0;


// ============================================================
// BATTERY
// ============================================================

uint16_t readBatteryMillivolts()
{
  analogReadResolution(12);

  uint32_t raw =
      analogReadVDDHDIV5();

  float millivolts =
      ((float)raw * 3600.0f * 5.0f)
      / 4095.0f;

  if (millivolts > 6000.0f)
  {
    millivolts = 6000.0f;
  }

  return (uint16_t)roundf(
      millivolts
  );
}


// ============================================================
// TAKE MEASUREMENT AND ADVERTISE
// ============================================================

void takeMeasurementAndAdvertise()
{
  // ----------------------------------------------------------
  // Read SHT40
  // ----------------------------------------------------------

  sensors_event_t humidityEvent;
  sensors_event_t temperatureEvent;

  sht4.getEvent(
      &humidityEvent,
      &temperatureEvent
  );

  float temperature =
      temperatureEvent.temperature;

  float humidity =
      humidityEvent.relative_humidity;


  // ----------------------------------------------------------
  // Invalid measurement?
  //
  // Don't waste radio energy sending garbage.
  // ----------------------------------------------------------

  if (
      isnan(temperature)
      ||
      isnan(humidity)
  )
  {
    return;
  }


  // ----------------------------------------------------------
  // Battery
  // ----------------------------------------------------------

  uint16_t batteryMillivolts =
      readBatteryMillivolts();


  // ----------------------------------------------------------
  // Sequence
  // ----------------------------------------------------------

  sequenceNumber++;


  // ----------------------------------------------------------
  // Packet
  // ----------------------------------------------------------

  SensorPacket packet;

  packet.protocolVersion =
      1;

  packet.sensorId =
      SENSOR_ID;

  packet.sequence =
      sequenceNumber;

  packet.temperature =
      (int16_t)roundf(
          temperature * 100.0f
      );

  packet.humidity =
      (uint16_t)roundf(
          humidity * 100.0f
      );

  packet.batteryMillivolts =
      batteryMillivolts;


  // ----------------------------------------------------------
  // Manufacturer data
  //
  // 0x0059 = Bluetooth Company Identifier
  //
  // BLE sends:
  //
  // 59 00
  // + our 12-byte packet
  // ----------------------------------------------------------

  uint8_t manufacturerData[
      2 + sizeof(SensorPacket)
  ];

  manufacturerData[0] =
      0x59;

  manufacturerData[1] =
      0x00;

  memcpy(
      &manufacturerData[2],
      &packet,
      sizeof(packet)
  );


  // ----------------------------------------------------------
  // Previous advertising burst has already timed out.
  //
  // Do NOT call Advertising.stop().
  // ----------------------------------------------------------

  Bluefruit.Advertising.clearData();


  Bluefruit.Advertising.addFlags(
      BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE
  );


  Bluefruit.Advertising.addManufacturerData(
      manufacturerData,
      sizeof(manufacturerData)
  );


  // ----------------------------------------------------------
  // Advertising interval
  //
  // 160 * 0.625 ms = 100 ms
  //
  // About 10 advertising events during our 1-second
  // diagnostic burst.
  // ----------------------------------------------------------

  Bluefruit.Advertising.setInterval(
      160,
      160
  );


  // ----------------------------------------------------------
  // Advertise for 1 second.
  //
  // Bluefruit/SoftDevice handles radio operation.
  // ----------------------------------------------------------

  Bluefruit.Advertising.start(
      1
  );
}


// ============================================================
// SETUP
// ============================================================

void setup()
{
  // ----------------------------------------------------------
  // LED OFF
  // ----------------------------------------------------------

  pinMode(
      LED_BUILTIN,
      OUTPUT
  );

  ledOff(
      LED_BUILTIN
  );


  // ----------------------------------------------------------
  // I2C
  // ----------------------------------------------------------

  Wire.begin();


  // ----------------------------------------------------------
  // SHT40
  // ----------------------------------------------------------

  if (!sht4.begin())
  {
    // Fatal sensor error.
    //
    // Sleep instead of spinning at full CPU power.

    while (true)
    {
      sd_app_evt_wait();
    }
  }


  // Medium precision:
  // plenty for room monitoring.

  sht4.setPrecision(
      SHT4X_MED_PRECISION
  );


  // Heater OFF.

  sht4.setHeater(
      SHT4X_NO_HEATER
  );


  // ----------------------------------------------------------
  // BLUEFRUIT
  // ----------------------------------------------------------

  Bluefruit.begin(
      1,
      0
  );


  // No automatic status LED.

  Bluefruit.autoConnLed(
      false
  );


  pinMode(
      LED_BUILTIN,
      OUTPUT
  );

  ledOff(
      LED_BUILTIN
  );


  // ----------------------------------------------------------
  // BLE identity
  // ----------------------------------------------------------

  Bluefruit.setName(
      SENSOR_NAME
  );


  // ----------------------------------------------------------
  // TX power
  //
  // 0 dBm is a sensible starting point.
  //
  // Later:
  //   test -4 dBm
  //   test -8 dBm
  // ----------------------------------------------------------

  Bluefruit.setTxPower(
      0
  );


  // ----------------------------------------------------------
  // Scan response
  //
  // Configure name once.
  // ----------------------------------------------------------

  Bluefruit.ScanResponse.clearData();


  Bluefruit.ScanResponse.addName();


  // ----------------------------------------------------------
  // First measurement immediately
  // ----------------------------------------------------------

  takeMeasurementAndAdvertise();


  lastMeasurement =
      millis();
}


// ============================================================
// LOOP
// ============================================================

void loop()
{
  uint32_t now =
      millis();


  // ----------------------------------------------------------
  // Is it time for another measurement?
  // ----------------------------------------------------------

  if (
      (uint32_t)(
          now - lastMeasurement
      )
      >= MEASUREMENT_INTERVAL_MS
  )
  {
    lastMeasurement =
        now;


    takeMeasurementAndAdvertise();
  }


  // ----------------------------------------------------------
  // SYSTEM-ON LOW POWER SLEEP
  //
  // CPU stops executing here.
  //
  // It wakes when the SoftDevice / timer / interrupt has
  // something to process.
  //
  // RAM retained.
  // RTC/timing retained.
  // BLE remains functional.
  //
  // This is NOT a busy wait.
  // ----------------------------------------------------------

  sd_app_evt_wait();
}