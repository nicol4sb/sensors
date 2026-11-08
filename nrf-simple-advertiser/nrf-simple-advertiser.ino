#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <bluefruit.h>
#include <math.h>

#include "nrf_soc.h"


// ============================================================
// VMC SENSOR - USB-POWERED OUTSIDE / LOW-POWER INDOOR
//
// SAME CODE FOR ALL 6 SENSORS.
//
// Change ONLY SENSOR_ID:
//
//   1 = salon
//   2 = Parents
//   3 = buanderie
//   4 = Outside (USB power)
//   5 = Sensor 5
//   6 = Sensor 6
//
// Sensor 4: measure every minute and continuously repeat the
// latest reading every 100 ms at maximum nRF52840 TX power.
//
// Other sensors, every 10 minutes:
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
// ============================================================


// ============================================================
// SENSOR ID
//
// CHANGE ONLY THIS VALUE FOR EACH SENSOR:
//
//   1 = salon
//   2 = Parents
//   3 = buanderie
//   4 = Outside (USB power)
//   5 = Sensor 5
//   6 = Sensor 6
// ============================================================

#define SENSOR_ID 4


#if SENSOR_ID == 4
// Mains power: keep the latest sample available between measurements.
static const uint32_t MEASUREMENT_INTERVAL_MS = 600UL;
static const uint16_t ADVERTISING_TIMEOUT_SECONDS = 0; // No timeout.
#else
// Battery power: a short advertising burst every ten minutes.
static const uint32_t MEASUREMENT_INTERVAL_MS = 600000UL;
static const uint16_t ADVERTISING_TIMEOUT_SECONDS = 2;
#endif



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
  // Stop before modifying the payload used by the radio.
  // Sensor 4 is still advertising the previous sample here.
  // ----------------------------------------------------------

  if (Bluefruit.Advertising.isRunning())
  {
    Bluefruit.Advertising.stop();
  }

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
  // Sensor 4 repeats continuously between one-minute samples.
  // Battery sensors send about 20 events in a 2-second burst.
  // ----------------------------------------------------------

  Bluefruit.Advertising.setInterval(
      160,
      160
  );


  // ----------------------------------------------------------
  // A zero timeout keeps sensor 4 advertising until the next
  // sample replaces the payload. Battery sensors stop after 2 s.
  //
  // Bluefruit/SoftDevice handles radio operation.
  // ----------------------------------------------------------

  Bluefruit.Advertising.start(
      ADVERTISING_TIMEOUT_SECONDS
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


  // Match the fast phase to the total timeout. Zero keeps the
  // USB-powered outside sensor advertising indefinitely.

  Bluefruit.Advertising.setFastTimeout(
      ADVERTISING_TIMEOUT_SECONDS
  );



  // ----------------------------------------------------------
  // TX power
  //
  // +8 dBm is the nRF52840 maximum; already used by all sensors.
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
