#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <bluefruit.h>
#include <math.h>

#include "nrf_soc.h"


// ============================================================
// VMC SENSOR - LOW POWER TEST
//
// SAME CODE FOR ALL 3 SENSORS.
//
// Change ONLY SENSOR_ID:
//
//   1 = Red
//   2 = Green
//   3 = Sensor 3
//
// Every 10 minutes:
//   wake
//   read SHT40
//   read battery
//   advertise for 2 seconds
//   System-ON sleep
//
// No Serial
// No LEDs
// No history
// No BLE connection
//
// Production interval: 10 minutes.
// ============================================================


// ============================================================
// SENSOR ID
//
// CHANGE ONLY THIS VALUE FOR EACH SENSOR:
//
//   1 = Red
//   2 = Green
//   3 = Sensor 3
// ============================================================

#define SENSOR_ID 2

#if SENSOR_ID < 1 || SENSOR_ID > 3
#error "SENSOR_ID must be 1, 2, or 3"
#endif


static const uint32_t MEASUREMENT_INTERVAL_MS = 600000UL;



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

  if (!sht4.getEvent(
          &humidityEvent,
          &temperatureEvent
      ))
  {
    return;
  }


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
  // Clamp humidity to physical range
  // ----------------------------------------------------------

  if (humidity < 0.0f)
  {
    humidity = 0.0f;
  }

  if (humidity > 100.0f)
  {
    humidity = 100.0f;
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
  // About 20 advertising events during our 2-second
  // diagnostic burst.
  // ----------------------------------------------------------

  Bluefruit.Advertising.setInterval(
      160,
      160
  );


  // ----------------------------------------------------------
  // Advertise for 2 seconds. The fast timeout is also configured
  // as 2 seconds in setup so it cannot exceed this total timeout.
  //
  // Bluefruit/SoftDevice handles radio operation.
  // ----------------------------------------------------------

  Bluefruit.Advertising.start(
      2
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


  // Manufacturer data does not need a scan response. Using
  // non-connectable, non-scannable advertising minimizes radio
  // work and prevents connection attempts.

  Bluefruit.Advertising.setType(
      BLE_GAP_ADV_TYPE_NONCONNECTABLE_NONSCANNABLE_UNDIRECTED
  );


  // Keep a two-second total timeout from inheriting Bluefruit's
  // default 30-second fast-advertising phase.

  Bluefruit.Advertising.setFastTimeout(
      2
  );



  // ----------------------------------------------------------
  // TX power
  //
  // +8 dBm for better reception margin.
  // ----------------------------------------------------------

  Bluefruit.setTxPower(
      8
  );



  // ----------------------------------------------------------
  // First measurement immediately
  // ----------------------------------------------------------

  takeMeasurementAndAdvertise();

}


// ============================================================
// LOOP
// ============================================================

void loop()
{


  // Block this task so the RTOS idle task can enter its low-power
  // state while BLE continues asynchronously.

  delay(MEASUREMENT_INTERVAL_MS);

  takeMeasurementAndAdvertise();
}
