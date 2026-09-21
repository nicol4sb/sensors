#include <bluefruit.h>
#include <Adafruit_TinyUSB.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>

// ============================================================
// CONFIGURATION
// ============================================================

#define SENSOR_ID                1
#define SENSOR_NAME              "NNTempHumidity1"

#define SAMPLE_INTERVAL_MS       60000UL      // 1 minute
#define BATTERY_INTERVAL_MS      600000UL     // 10 minutes
#define GATT_WINDOW_MS           10000UL       // 10 seconds

#define HISTORY_CAPACITY         1440         // 24 h @ 1/min
#define BATTERY_HISTORY_CAPACITY 144          // 24 h @ 1/10 min

#define HISTORY_BLOCK_SAMPLES    20
#define BATTERY_BLOCK_SAMPLES    20

// nice!nano-style battery input.
// Community Pro Micro board definition normally maps A3 -> P0.04.
#define BATTERY_PIN 4

// Battery divider calibration.
//
// Genuine nice!nano v1-style circuitry uses a divided battery signal.
// Start with 2.0. Verify against a multimeter and adjust if necessary.
#define BATTERY_DIVIDER_RATIO    2.0f

// ============================================================
// UUIDs
// ============================================================

#define SERVICE_UUID \
  "7a100000-4c7f-4f4d-432d-564d4353454e"

#define META_UUID \
  "7a100001-4c7f-4f4d-432d-564d4353454e"

#define CURRENT_UUID \
  "7a100002-4c7f-4f4d-432d-564d4353454e"

#define HISTORY_INDEX_UUID \
  "7a100003-4c7f-4f4d-432d-564d4353454e"

#define HISTORY_BLOCK_UUID \
  "7a100004-4c7f-4f4d-432d-564d4353454e"

#define BATTERY_CURRENT_UUID \
  "7a100005-4c7f-4f4d-432d-564d4353454e"

#define BATTERY_INDEX_UUID \
  "7a100006-4c7f-4f4d-432d-564d4353454e"

#define BATTERY_BLOCK_UUID \
  "7a100007-4c7f-4f4d-432d-564d4353454e"


// ============================================================
// SENSOR
// ============================================================

Adafruit_SHT4x sht4;


// ============================================================
// SAMPLE STRUCTURES
// ============================================================

struct __attribute__((packed)) VMCSample
{
  uint32_t seq;
  int16_t temperature_centi;
  uint16_t humidity_centi;
};

static_assert(sizeof(VMCSample) == 8, "VMCSample must be 8 bytes");


struct __attribute__((packed)) BatterySample
{
  uint32_t seq;
  uint16_t millivolts;
};

static_assert(sizeof(BatterySample) == 6, "BatterySample must be 6 bytes");


// ============================================================
// ENVIRONMENT HISTORY
// ============================================================

VMCSample history[HISTORY_CAPACITY];

uint16_t historyHead = 0;
uint16_t historyCount = 0;

uint32_t nextSequence = 1;

VMCSample currentSample = {};

bool haveSample = false;


// ============================================================
// BATTERY HISTORY
// ============================================================

BatterySample batteryHistory[BATTERY_HISTORY_CAPACITY];

uint16_t batteryHead = 0;
uint16_t batteryCount = 0;

BatterySample currentBattery = {};

bool haveBattery = false;


// ============================================================
// BLE
// ============================================================

BLEService vmcService(SERVICE_UUID);

BLECharacteristic metaChar(META_UUID);
BLECharacteristic currentChar(CURRENT_UUID);

BLECharacteristic historyIndexChar(HISTORY_INDEX_UUID);
BLECharacteristic historyBlockChar(HISTORY_BLOCK_UUID);

BLECharacteristic batteryCurrentChar(BATTERY_CURRENT_UUID);
BLECharacteristic batteryIndexChar(BATTERY_INDEX_UUID);
BLECharacteristic batteryBlockChar(BATTERY_BLOCK_UUID);


// ============================================================
// STATE
// ============================================================

volatile bool clientConnected = false;

bool gattWindowOpen = false;

uint32_t gattWindowStarted = 0;
uint32_t lastMeasurementTime = 0;
uint32_t lastBatteryTime = 0;

uint16_t requestedHistoryIndex = 0;
uint16_t requestedBatteryIndex = 0;


// ============================================================
// LITTLE ENDIAN
// ============================================================

void putLE16(uint8_t *p, uint16_t v)
{
  p[0] = v & 0xFF;
  p[1] = (v >> 8) & 0xFF;
}


void putLE32(uint8_t *p, uint32_t v)
{
  p[0] = v & 0xFF;
  p[1] = (v >> 8) & 0xFF;
  p[2] = (v >> 16) & 0xFF;
  p[3] = (v >> 24) & 0xFF;
}


uint16_t getLE16(const uint8_t *p)
{
  return ((uint16_t)p[0]) |
         ((uint16_t)p[1] << 8);
}


// ============================================================
// ENVIRONMENT HISTORY
// ============================================================

void addHistory(const VMCSample &s)
{
  history[historyHead] = s;

  historyHead = (historyHead + 1) % HISTORY_CAPACITY;

  if (historyCount < HISTORY_CAPACITY) {
    historyCount++;
  }
}


VMCSample getHistory(uint16_t index)
{
  uint16_t oldest =
    (historyCount < HISTORY_CAPACITY)
      ? 0
      : historyHead;

  uint16_t physical =
    (oldest + index) % HISTORY_CAPACITY;

  return history[physical];
}


// ============================================================
// BATTERY HISTORY
// ============================================================

void addBatteryHistory(const BatterySample &s)
{
  batteryHistory[batteryHead] = s;

  batteryHead =
    (batteryHead + 1) %
    BATTERY_HISTORY_CAPACITY;

  if (batteryCount < BATTERY_HISTORY_CAPACITY) {
    batteryCount++;
  }
}


BatterySample getBatteryHistory(uint16_t index)
{
  uint16_t oldest =
    (batteryCount < BATTERY_HISTORY_CAPACITY)
      ? 0
      : batteryHead;

  uint16_t physical =
    (oldest + index) %
    BATTERY_HISTORY_CAPACITY;

  return batteryHistory[physical];
}


// ============================================================
// METADATA
//
// 16 bytes:
//
// 0       protocol version = 2
// 1       sensor ID
// 2-3     environmental interval seconds
// 4-5     environmental history count
// 6-7     environmental history capacity
// 8-11    newest sequence
// 12-13   battery history count
// 14-15   battery interval seconds
// ============================================================

void updateMetadata()
{
  uint8_t data[16] = {};

  data[0] = 2;
  data[1] = SENSOR_ID;

  putLE16(&data[2], SAMPLE_INTERVAL_MS / 1000UL);
  putLE16(&data[4], historyCount);
  putLE16(&data[6], HISTORY_CAPACITY);

  uint32_t newest = 0;

  if (historyCount > 0) {
    newest = getHistory(historyCount - 1).seq;
  }

  putLE32(&data[8], newest);

  putLE16(&data[12], batteryCount);
  putLE16(&data[14], BATTERY_INTERVAL_MS / 1000UL);

  metaChar.write(data, sizeof(data));
}


// ============================================================
// CURRENT ENVIRONMENT
// ============================================================

void updateCurrentCharacteristic()
{
  uint8_t data[8] = {};

  if (haveSample) {
    putLE32(&data[0], currentSample.seq);
    putLE16(&data[4], (uint16_t)currentSample.temperature_centi);
    putLE16(&data[6], currentSample.humidity_centi);
  }

  currentChar.write(data, sizeof(data));
}


// ============================================================
// CURRENT BATTERY
//
// 6 bytes:
// sequence + millivolts
// ============================================================

void updateBatteryCharacteristic()
{
  uint8_t data[6] = {};

  if (haveBattery) {
    putLE32(&data[0], currentBattery.seq);
    putLE16(&data[4], currentBattery.millivolts);
  }

  batteryCurrentChar.write(data, sizeof(data));
}


// ============================================================
// ENVIRONMENT HISTORY BLOCK
// ============================================================

void updateHistoryBlock()
{
  uint8_t data[4 + HISTORY_BLOCK_SAMPLES * 8];

  uint16_t start = requestedHistoryIndex;

  if (start > historyCount) {
    start = historyCount;
  }

  uint16_t remaining = historyCount - start;

  uint16_t count =
    min(remaining, (uint16_t)HISTORY_BLOCK_SAMPLES);

  putLE16(&data[0], start);
  putLE16(&data[2], count);

  uint16_t pos = 4;

  for (uint16_t i = 0; i < count; i++) {

    VMCSample s = getHistory(start + i);

    putLE32(&data[pos], s.seq);
    putLE16(&data[pos + 4], (uint16_t)s.temperature_centi);
    putLE16(&data[pos + 6], s.humidity_centi);

    pos += 8;
  }

  historyBlockChar.write(data, pos);
}


// ============================================================
// BATTERY HISTORY BLOCK
// ============================================================

void updateBatteryBlock()
{
  uint8_t data[4 + BATTERY_BLOCK_SAMPLES * 6];

  uint16_t start = requestedBatteryIndex;

  if (start > batteryCount) {
    start = batteryCount;
  }

  uint16_t remaining = batteryCount - start;

  uint16_t count =
    min(remaining, (uint16_t)BATTERY_BLOCK_SAMPLES);

  putLE16(&data[0], start);
  putLE16(&data[2], count);

  uint16_t pos = 4;

  for (uint16_t i = 0; i < count; i++) {

    BatterySample s =
      getBatteryHistory(start + i);

    putLE32(&data[pos], s.seq);
    putLE16(&data[pos + 4], s.millivolts);

    pos += 6;
  }

  batteryBlockChar.write(data, pos);
}


// ============================================================
// GATT WRITE CALLBACKS
// ============================================================

void historyIndexWritten(
  uint16_t conn_hdl,
  BLECharacteristic *chr,
  uint8_t *data,
  uint16_t len)
{
  (void)conn_hdl;
  (void)chr;

  if (len != 2) return;

  requestedHistoryIndex = getLE16(data);

  updateHistoryBlock();
}


void batteryIndexWritten(
  uint16_t conn_hdl,
  BLECharacteristic *chr,
  uint8_t *data,
  uint16_t len)
{
  (void)conn_hdl;
  (void)chr;

  if (len != 2) return;

  requestedBatteryIndex = getLE16(data);

  updateBatteryBlock();
}


// ============================================================
// BATTERY MEASUREMENT
// ============================================================

uint16_t readBatteryMillivolts()
{
  analogReadResolution(12);

  // nRF52840 internal VDDH/5 SAADC input.
  // The ADC itself sees VDDH divided by 5.
  uint32_t raw = analogReadVDDHDIV5();

  // Default Adafruit SAADC range is 0..3.6 V.
  // 12-bit ADC = 0..4095.
  //
  // Because VDDH is internally divided by 5:
  //
  // VDDH = ADC_voltage * 5

  float millivolts =
    ((float)raw * 3600.0f * 5.0f) / 4095.0f;

  if (millivolts > 6000.0f) {
    millivolts = 6000.0f;
  }

  return (uint16_t)roundf(millivolts);
}


void measureBattery()
{
  currentBattery.seq =
    currentSample.seq;

  currentBattery.millivolts =
    readBatteryMillivolts();

  haveBattery = true;

  addBatteryHistory(currentBattery);

  updateBatteryCharacteristic();

  Serial.print("Battery = ");

  Serial.print(
    currentBattery.millivolts / 1000.0f,
    3
  );

  Serial.println(" V");
}


// ============================================================
// SHT40
// ============================================================

bool takeMeasurement()
{
  sensors_event_t humidity;
  sensors_event_t temp;

  sht4.getEvent(&humidity, &temp);

  if (
    isnan(temp.temperature) ||
    isnan(humidity.relative_humidity)
  ) {
    Serial.println("SHT40 read failed");
    return false;
  }

  currentSample.seq = nextSequence++;

  currentSample.temperature_centi =
    (int16_t)roundf(temp.temperature * 100.0f);

  float rh = humidity.relative_humidity;

  if (rh < 0.0f) rh = 0.0f;
  if (rh > 100.0f) rh = 100.0f;

  currentSample.humidity_centi =
    (uint16_t)roundf(rh * 100.0f);

  haveSample = true;

  addHistory(currentSample);

  updateCurrentCharacteristic();

  Serial.print("#");
  Serial.print(currentSample.seq);

  Serial.print(" T=");
  Serial.print(currentSample.temperature_centi / 100.0f, 2);

  Serial.print("C RH=");
  Serial.print(currentSample.humidity_centi / 100.0f, 2);

  Serial.println("%");

  return true;
}


// ============================================================
// BLE CALLBACKS
// ============================================================

void connectCallback(uint16_t conn_hdl)
{
  clientConnected = true;

  Serial.println("*** GATT CONNECTED ***");

  BLEConnection *connection =
    Bluefruit.Connection(conn_hdl);

  if (connection) {
    connection->requestMtuExchange(247);
  }
}


void disconnectCallback(
  uint16_t conn_hdl,
  uint8_t reason)
{
  (void)conn_hdl;
  (void)reason;

  clientConnected = false;

  Serial.println("*** GATT DISCONNECTED ***");

  if (gattWindowOpen) {
    Bluefruit.Advertising.stop();
    gattWindowOpen = false;
  }
}


// ============================================================
// ADVERTISING
// ============================================================

bool startAdvertising()
{
  Bluefruit.Advertising.stop();

  Bluefruit.Advertising.clearData();
  Bluefruit.ScanResponse.clearData();

  Bluefruit.Advertising.addFlags(
    BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE
  );

  Bluefruit.Advertising.addTxPower();

  uint8_t mfgData[] = {
    0x59,
    0x00,
    SENSOR_ID
  };

  Bluefruit.Advertising.addManufacturerData(
    mfgData,
    sizeof(mfgData)
  );

  Bluefruit.Advertising.addName();

  Bluefruit.Advertising.setType(
    BLE_GAP_ADV_TYPE_CONNECTABLE_SCANNABLE_UNDIRECTED
  );

  // ~100 ms while the short advertising window is open.
  Bluefruit.Advertising.setInterval(160, 160);

  Bluefruit.Advertising.setFastTimeout(0);

  return Bluefruit.Advertising.start(0);
}


void openGattWindow()
{
  if (clientConnected) return;

  if (!startAdvertising()) {
    Serial.println("Advertising start failed");
    return;
  }

  gattWindowOpen = true;
  gattWindowStarted = millis();

  Serial.println("BLE window OPEN");
}


void serviceGattWindow()
{
  if (!gattWindowOpen) return;

  // Once connected, allow the GATT transfer to take as long
  // as necessary.
  if (clientConnected) return;

  if (
    (uint32_t)(millis() - gattWindowStarted)
      >= GATT_WINDOW_MS
  ) {
    Bluefruit.Advertising.stop();

    gattWindowOpen = false;

    Serial.println("BLE window CLOSED");
  }
}


// ============================================================
// GATT SETUP
// ============================================================

void setupGatt()
{
  vmcService.begin();

  // Metadata
  metaChar.setProperties(CHR_PROPS_READ);
  metaChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  metaChar.setFixedLen(16);
  metaChar.begin();

  // Current T/RH
  currentChar.setProperties(CHR_PROPS_READ);
  currentChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  currentChar.setFixedLen(8);
  currentChar.begin();

  // Environment history index
  historyIndexChar.setProperties(CHR_PROPS_WRITE);
  historyIndexChar.setPermission(SECMODE_NO_ACCESS, SECMODE_OPEN);
  historyIndexChar.setFixedLen(2);
  historyIndexChar.setWriteCallback(historyIndexWritten);
  historyIndexChar.begin();

  // Environment history block
  historyBlockChar.setProperties(CHR_PROPS_READ);
  historyBlockChar.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  historyBlockChar.setMaxLen(
    4 + HISTORY_BLOCK_SAMPLES * 8
  );
  historyBlockChar.begin();

  // Current battery
  batteryCurrentChar.setProperties(CHR_PROPS_READ);
  batteryCurrentChar.setPermission(
    SECMODE_OPEN,
    SECMODE_NO_ACCESS
  );
  batteryCurrentChar.setFixedLen(6);
  batteryCurrentChar.begin();

  // Battery history index
  batteryIndexChar.setProperties(CHR_PROPS_WRITE);
  batteryIndexChar.setPermission(
    SECMODE_NO_ACCESS,
    SECMODE_OPEN
  );
  batteryIndexChar.setFixedLen(2);
  batteryIndexChar.setWriteCallback(
    batteryIndexWritten
  );
  batteryIndexChar.begin();

  // Battery history block
  batteryBlockChar.setProperties(CHR_PROPS_READ);
  batteryBlockChar.setPermission(
    SECMODE_OPEN,
    SECMODE_NO_ACCESS
  );
  batteryBlockChar.setMaxLen(
    4 + BATTERY_BLOCK_SAMPLES * 6
  );
  batteryBlockChar.begin();

  updateMetadata();
  updateCurrentCharacteristic();
  updateBatteryCharacteristic();
  updateHistoryBlock();
  updateBatteryBlock();
}


// ============================================================
// SETUP
// ============================================================

void setup()
{
  // Built-in LED OFF
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);

  Serial.begin(115200);

  uint32_t start = millis();

  while (!Serial && millis() - start < 3000UL) {
    delay(10);
  }

  Serial.println();
  Serial.println("==============================");
  Serial.println(SENSOR_NAME);
  Serial.println("==============================");

  // I2C
  Wire.setPins(2, 3);
  Wire.begin();

  if (!sht4.begin(&Wire)) {
    Serial.println("ERROR: SHT40 not found");

    while (1) {
      delay(1000);
    }
  }

  // Lower-energy measurement than high precision.
  sht4.setPrecision(SHT4X_MED_PRECISION);

  // Heater explicitly disabled.
  sht4.setHeater(SHT4X_NO_HEATER);

  Serial.println("SHT40 OK - heater OFF");

  // BLE
  Bluefruit.begin(1, 0);

  Bluefruit.setTxPower(4);
  Bluefruit.setName(SENSOR_NAME);

  Bluefruit.Periph.setConnectCallback(connectCallback);
  Bluefruit.Periph.setDisconnectCallback(disconnectCallback);

  setupGatt();

  Serial.println("BLE OK");
  Serial.println("Sample: 1 minute");
  Serial.println("Battery: 10 minutes");
  Serial.println("BLE window: 2 seconds");

  // First environmental sample immediately.
  lastMeasurementTime =
    millis() - SAMPLE_INTERVAL_MS;

  // First battery measurement immediately.
  lastBatteryTime =
    millis() - BATTERY_INTERVAL_MS;
}


// ============================================================
// LOOP
// ============================================================

void loop()
{
  serviceGattWindow();

  uint32_t now = millis();

  if (
    (uint32_t)(now - lastMeasurementTime)
      >= SAMPLE_INTERVAL_MS
  ) {
    lastMeasurementTime = now;

    if (takeMeasurement()) {

      // Battery every ten minutes.
      if (
        !haveBattery ||
        (uint32_t)(now - lastBatteryTime)
          >= BATTERY_INTERVAL_MS
      ) {
        lastBatteryTime = now;

        measureBattery();
      }

      updateMetadata();

      if (!clientConnected) {
        openGattWindow();
      }
    }
  }

  // Keep this delay-free for now because that is the
  // configuration already proven reliable on this board.
}