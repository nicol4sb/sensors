#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SHT4x.h>
#include <bluefruit.h>
#include <Adafruit_TinyUSB.h>
#include <math.h>

// ============================================================
// SENSOR IDENTITY
// ============================================================
//
// RED SENSOR:
//   SENSOR_ID   1
//   SENSOR_NAME "NNTempHumidityRed"
//
// GREEN SENSOR:
//   SENSOR_ID   2
//   SENSOR_NAME "NNTempHumidityGreen"
//
// CHANGE THESE TWO LINES WHEN FLASHING THE OTHER SENSOR.
// ============================================================

#define SENSOR_ID   2
#define SENSOR_NAME "NNTempHumidityGreen"


// ============================================================
// TIMING
// ============================================================

static const uint32_t ENV_INTERVAL_MS =
    60UL * 1000UL;

static const uint32_t BATTERY_INTERVAL_MS =
    10UL * 60UL * 1000UL;

static const uint32_t ADVERTISING_DURATION_SECONDS =
    10;


// ============================================================
// HISTORY CAPACITIES
// ============================================================

// 24 hours of one-minute environmental measurements

static const uint16_t ENV_HISTORY_CAPACITY =
    24 * 60;


// 24 hours of one battery measurement every 10 minutes

static const uint16_t BATTERY_HISTORY_CAPACITY =
    24 * 6;


// ============================================================
// BLE STREAM PACKET SIZES
// ============================================================
//
// ATT MTU target:
//     247 bytes
//
// Maximum notification characteristic payload:
//     247 - 3 = 244 bytes
//
// Environmental packet:
//     4 byte header
//     + 30 * 8 byte samples
//     = 244 bytes
//
// Battery packet:
//     4 byte header
//     + 40 * 6 byte samples
//     = 244 bytes
//
// ============================================================

static const uint16_t ENV_SAMPLES_PER_BLOCK = 30;
static const uint16_t BATTERY_SAMPLES_PER_BLOCK = 40;

static const uint16_t ENV_BLOCK_MAX =
    4 + ENV_SAMPLES_PER_BLOCK * 8;

static const uint16_t BATTERY_BLOCK_MAX =
    4 + BATTERY_SAMPLES_PER_BLOCK * 6;


// ============================================================
// DATA STRUCTURES
// ============================================================

struct __attribute__((packed)) EnvironmentSample
{
    uint32_t sequence;
    int16_t temperature;
    uint16_t humidity;
};


struct __attribute__((packed)) BatterySample
{
    uint32_t sequence;
    uint16_t millivolts;
};


// Compile-time sanity checks

static_assert(
    sizeof(EnvironmentSample) == 8,
    "EnvironmentSample must be 8 bytes"
);

static_assert(
    sizeof(BatterySample) == 6,
    "BatterySample must be 6 bytes"
);


// ============================================================
// HISTORY BUFFERS
// ============================================================

EnvironmentSample envHistory[ENV_HISTORY_CAPACITY];

BatterySample batteryHistory[BATTERY_HISTORY_CAPACITY];


uint16_t envHead = 0;
uint16_t envCount = 0;

uint16_t batteryHead = 0;
uint16_t batteryCount = 0;

uint32_t sequenceNumber = 0;


// ============================================================
// SENSOR
// ============================================================

Adafruit_SHT4x sht4;


// ============================================================
// BLE UUIDs
// ============================================================

BLEService vmcService(
    "7a100000-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic metadataCharacteristic(
    "7a100001-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic currentCharacteristic(
    "7a100002-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic historyIndexCharacteristic(
    "7a100003-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic historyBlockCharacteristic(
    "7a100004-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic batteryCurrentCharacteristic(
    "7a100005-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic batteryIndexCharacteristic(
    "7a100006-4c7f-4f4d-432d-564d4353454e"
);

BLECharacteristic batteryBlockCharacteristic(
    "7a100007-4c7f-4f4d-432d-564d4353454e"
);


// ============================================================
// STREAM STATE
// ============================================================

volatile bool envStreamRequested = false;
volatile uint16_t envRequestedIndex = 0;

volatile bool batteryStreamRequested = false;
volatile uint16_t batteryRequestedIndex = 0;


bool envStreaming = false;
uint16_t envStreamIndex = 0;
uint16_t envStreamCount = 0;
uint16_t envStreamOldestPhysical = 0;


bool batteryStreaming = false;
uint16_t batteryStreamIndex = 0;
uint16_t batteryStreamCount = 0;
uint16_t batteryStreamOldestPhysical = 0;


// ============================================================
// TIMERS
// ============================================================

uint32_t lastEnvironmentMeasurement = 0;
uint32_t lastBatteryMeasurement = 0;



// ============================================================
// LITTLE-ENDIAN HELPERS
// ============================================================

void put16(uint8_t *buffer, uint16_t value)
{
    buffer[0] = value & 0xFF;
    buffer[1] = (value >> 8) & 0xFF;
}


void put32(uint8_t *buffer, uint32_t value)
{
    buffer[0] = value & 0xFF;
    buffer[1] = (value >> 8) & 0xFF;
    buffer[2] = (value >> 16) & 0xFF;
    buffer[3] = (value >> 24) & 0xFF;
}


uint16_t get16(const uint8_t *buffer)
{
    return
        ((uint16_t)buffer[0]) |
        ((uint16_t)buffer[1] << 8);
}


// ============================================================
// BATTERY
// ============================================================

uint16_t readBatteryMillivolts()
{
    analogReadResolution(12);

    uint32_t raw = analogReadVDDHDIV5();

    float millivolts =
        ((float)raw * 3600.0f * 5.0f) / 4095.0f;

    if (millivolts > 6000.0f)
    {
        millivolts = 6000.0f;
    }

    return (uint16_t)roundf(millivolts);
}


// ============================================================
// CIRCULAR BUFFER HELPERS
// ============================================================

uint16_t envOldestPhysicalIndex()
{
    if (envCount < ENV_HISTORY_CAPACITY)
    {
        return 0;
    }

    return envHead;
}


uint16_t batteryOldestPhysicalIndex()
{
    if (batteryCount < BATTERY_HISTORY_CAPACITY)
    {
        return 0;
    }

    return batteryHead;
}


// ============================================================
// STORE ENVIRONMENT SAMPLE
// ============================================================

void storeEnvironmentSample(
    int16_t temperature,
    uint16_t humidity
)
{
    sequenceNumber++;

    EnvironmentSample sample;

    sample.sequence = sequenceNumber;
    sample.temperature = temperature;
    sample.humidity = humidity;

    envHistory[envHead] = sample;

    envHead++;

    if (envHead >= ENV_HISTORY_CAPACITY)
    {
        envHead = 0;
    }

    if (envCount < ENV_HISTORY_CAPACITY)
    {
        envCount++;
    }

    // Current characteristic

    uint8_t currentData[8];

    put32(
        currentData,
        sample.sequence
    );

    put16(
        currentData + 4,
        (uint16_t)sample.temperature
    );

    put16(
        currentData + 6,
        sample.humidity
    );

    currentCharacteristic.write(
        currentData,
        sizeof(currentData)
    );
}


// ============================================================
// STORE BATTERY SAMPLE
// ============================================================

void storeBatterySample(uint16_t millivolts)
{
    BatterySample sample;

    sample.sequence = sequenceNumber;
    sample.millivolts = millivolts;

    batteryHistory[batteryHead] = sample;

    batteryHead++;

    if (batteryHead >= BATTERY_HISTORY_CAPACITY)
    {
        batteryHead = 0;
    }

    if (batteryCount < BATTERY_HISTORY_CAPACITY)
    {
        batteryCount++;
    }

    uint8_t data[6];

    put32(
        data,
        sample.sequence
    );

    put16(
        data + 4,
        sample.millivolts
    );

    batteryCurrentCharacteristic.write(
        data,
        sizeof(data)
    );
}


// ============================================================
// METADATA
// ============================================================

void updateMetadata()
{
    uint8_t data[16];

    memset(
        data,
        0,
        sizeof(data)
    );

    // Protocol version

    data[0] = 3;

    // Sensor ID

    data[1] = SENSOR_ID;

    // Environmental interval seconds

    put16(
        data + 2,
        ENV_INTERVAL_MS / 1000
    );

    // Environmental history count

    put16(
        data + 4,
        envCount
    );

    // Environmental history capacity

    put16(
        data + 6,
        ENV_HISTORY_CAPACITY
    );

    // Newest sequence

    put32(
        data + 8,
        sequenceNumber
    );

    // Battery history count

    put16(
        data + 12,
        batteryCount
    );

    // Battery interval seconds

    put16(
        data + 14,
        BATTERY_INTERVAL_MS / 1000
    );

    metadataCharacteristic.write(
        data,
        sizeof(data)
    );
}


// ============================================================
// ADVERTISING
// ============================================================

void startAdvertising()
{
    if (Bluefruit.connected())
    {
        return;
    }

    Bluefruit.Advertising.stop();

    Bluefruit.Advertising.clearData();
    Bluefruit.ScanResponse.clearData();

    Bluefruit.Advertising.addFlags(
        BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE
    );

    Bluefruit.Advertising.addTxPower();

    Bluefruit.Advertising.addService(
        vmcService
    );

    uint8_t manufacturerData[3] =
    {
        0x59,
        0x00,
        SENSOR_ID
    };

    Bluefruit.Advertising.addManufacturerData(
        manufacturerData,
        sizeof(manufacturerData)
    );

    Bluefruit.ScanResponse.addName();

    Bluefruit.Advertising.setInterval(
        160,
        160
    );

    Bluefruit.Advertising.setFastTimeout(0);

    Bluefruit.Advertising.start(
        ADVERTISING_DURATION_SECONDS
    );
}


// ============================================================
// WRITE CALLBACKS
// ============================================================

void historyIndexWriteCallback(
    uint16_t connHandle,
    BLECharacteristic *characteristic,
    uint8_t *data,
    uint16_t length
)
{
    (void)connHandle;
    (void)characteristic;

    if (length < 2)
    {
        return;
    }

    envRequestedIndex = get16(data);
    envStreamRequested = true;
}


void batteryIndexWriteCallback(
    uint16_t connHandle,
    BLECharacteristic *characteristic,
    uint8_t *data,
    uint16_t length
)
{
    (void)connHandle;
    (void)characteristic;

    if (length < 2)
    {
        return;
    }

    batteryRequestedIndex = get16(data);
    batteryStreamRequested = true;
}


// ============================================================
// START ENVIRONMENT STREAM
// ============================================================

void beginEnvironmentStream()
{
    envStreamCount = envCount;

    envStreamOldestPhysical =
        envOldestPhysicalIndex();

    uint16_t requested =
        envRequestedIndex;

    if (requested > envStreamCount)
    {
        requested = envStreamCount;
    }

    envStreamIndex = requested;

    envStreaming =
        envStreamIndex < envStreamCount;
}


// ============================================================
// START BATTERY STREAM
// ============================================================

void beginBatteryStream()
{
    batteryStreamCount = batteryCount;

    batteryStreamOldestPhysical =
        batteryOldestPhysicalIndex();

    uint16_t requested =
        batteryRequestedIndex;

    if (requested > batteryStreamCount)
    {
        requested = batteryStreamCount;
    }

    batteryStreamIndex = requested;

    batteryStreaming =
        batteryStreamIndex < batteryStreamCount;
}


// ============================================================
// SEND ENVIRONMENT BLOCK
// ============================================================

void sendNextEnvironmentBlock()
{
    if (!envStreaming)
    {
        return;
    }

    if (!Bluefruit.connected())
    {
        envStreaming = false;
        return;
    }

    uint16_t remaining =
        envStreamCount - envStreamIndex;

    uint16_t numberToSend =
        remaining;

    if (numberToSend > ENV_SAMPLES_PER_BLOCK)
    {
        numberToSend =
            ENV_SAMPLES_PER_BLOCK;
    }

    uint8_t packet[ENV_BLOCK_MAX];

    put16(
        packet,
        envStreamIndex
    );

    put16(
        packet + 2,
        numberToSend
    );

    uint16_t offset = 4;

    for (
        uint16_t i = 0;
        i < numberToSend;
        i++
    )
    {
        uint16_t logicalIndex =
            envStreamIndex + i;

        uint16_t physicalIndex =
            (
                envStreamOldestPhysical
                + logicalIndex
            )
            % ENV_HISTORY_CAPACITY;

        EnvironmentSample &sample =
            envHistory[physicalIndex];

        put32(
            packet + offset,
            sample.sequence
        );

        put16(
            packet + offset + 4,
            (uint16_t)sample.temperature
        );

        put16(
            packet + offset + 6,
            sample.humidity
        );

        offset += 8;
    }

    bool sent =
        historyBlockCharacteristic.notify(
            packet,
            offset
        );

    if (sent)
    {
        envStreamIndex += numberToSend;

        if (envStreamIndex >= envStreamCount)
        {
            envStreaming = false;
        }
    }
}


// ============================================================
// SEND BATTERY BLOCK
// ============================================================

void sendNextBatteryBlock()
{
    if (!batteryStreaming)
    {
        return;
    }

    if (!Bluefruit.connected())
    {
        batteryStreaming = false;
        return;
    }

    uint16_t remaining =
        batteryStreamCount
        - batteryStreamIndex;

    uint16_t numberToSend =
        remaining;

    if (
        numberToSend
        > BATTERY_SAMPLES_PER_BLOCK
    )
    {
        numberToSend =
            BATTERY_SAMPLES_PER_BLOCK;
    }

    uint8_t packet[BATTERY_BLOCK_MAX];

    put16(
        packet,
        batteryStreamIndex
    );

    put16(
        packet + 2,
        numberToSend
    );

    uint16_t offset = 4;

    for (
        uint16_t i = 0;
        i < numberToSend;
        i++
    )
    {
        uint16_t logicalIndex =
            batteryStreamIndex + i;

        uint16_t physicalIndex =
            (
                batteryStreamOldestPhysical
                + logicalIndex
            )
            % BATTERY_HISTORY_CAPACITY;

        BatterySample &sample =
            batteryHistory[physicalIndex];

        put32(
            packet + offset,
            sample.sequence
        );

        put16(
            packet + offset + 4,
            sample.millivolts
        );

        offset += 6;
    }

    bool sent =
        batteryBlockCharacteristic.notify(
            packet,
            offset
        );

    if (sent)
    {
        batteryStreamIndex += numberToSend;

        if (
            batteryStreamIndex
            >= batteryStreamCount
        )
        {
            batteryStreaming = false;
        }
    }
}


// ============================================================
// SENSOR MEASUREMENT
// ============================================================

void takeEnvironmentMeasurement()
{
    sensors_event_t humidityEvent;
    sensors_event_t temperatureEvent;

    sht4.getEvent(
        &humidityEvent,
        &temperatureEvent
    );

    int16_t temperature =
        (int16_t)roundf(
            temperatureEvent.temperature
            * 100.0f
        );

    float rh =
        humidityEvent.relative_humidity;

    if (rh < 0.0f)
    {
        rh = 0.0f;
    }

    if (rh > 100.0f)
    {
        rh = 100.0f;
    }

    uint16_t humidity =
        (uint16_t)roundf(
            rh * 100.0f
        );

    storeEnvironmentSample(
        temperature,
        humidity
    );

    updateMetadata();

    startAdvertising();
}


// ============================================================
// BATTERY MEASUREMENT
// ============================================================

void takeBatteryMeasurement()
{
    uint16_t millivolts =
        readBatteryMillivolts();

    storeBatterySample(
        millivolts
    );

    updateMetadata();
}


// ============================================================
// SETUP BLE
// ============================================================

void setupBLE()
{
    // --------------------------------------------------------
    // IMPORTANT:
    //
    // Configure the SoftDevice BEFORE Bluefruit.begin().
    //
    // BANDWIDTH_MAX configures the peripheral connection for
    // high throughput and allows the larger ATT MTU.
    // --------------------------------------------------------

    Bluefruit.configPrphBandwidth(
        BANDWIDTH_MAX
    );

    Bluefruit.begin(
        1,
        0
    );

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

    Bluefruit.setTxPower(
        4
    );

    Bluefruit.setName(
        SENSOR_NAME
    );


    // --------------------------------------------------------
    // Connection interval
    //
    // Keep this fairly conservative for the first MTU test.
    // 12 units = 15 ms
    // 24 units = 30 ms
    // --------------------------------------------------------

    Bluefruit.Periph.setConnInterval(
        12,
        24
    );


    // ========================================================
    // SERVICE
    // ========================================================

    vmcService.begin();


    // ========================================================
    // METADATA
    // ========================================================

    metadataCharacteristic.setProperties(
        CHR_PROPS_READ
    );

    metadataCharacteristic.setPermission(
        SECMODE_OPEN,
        SECMODE_NO_ACCESS
    );

    metadataCharacteristic.setFixedLen(
        16
    );

    metadataCharacteristic.begin();


    // ========================================================
    // CURRENT ENVIRONMENT
    // ========================================================

    currentCharacteristic.setProperties(
        CHR_PROPS_READ
    );

    currentCharacteristic.setPermission(
        SECMODE_OPEN,
        SECMODE_NO_ACCESS
    );

    currentCharacteristic.setFixedLen(
        8
    );

    currentCharacteristic.begin();


    // ========================================================
    // ENVIRONMENT HISTORY COMMAND
    // ========================================================

    historyIndexCharacteristic.setProperties(
        CHR_PROPS_WRITE
    );

    historyIndexCharacteristic.setPermission(
        SECMODE_NO_ACCESS,
        SECMODE_OPEN
    );

    historyIndexCharacteristic.setFixedLen(
        2
    );

    historyIndexCharacteristic.setWriteCallback(
        historyIndexWriteCallback
    );

    historyIndexCharacteristic.begin();


    // ========================================================
    // ENVIRONMENT HISTORY NOTIFICATION
    //
    // VARIABLE LENGTH IS IMPORTANT.
    //
    // Maximum:
    //     244 bytes
    //
    // Last packet can be shorter.
    // ========================================================

    historyBlockCharacteristic.setProperties(
        CHR_PROPS_NOTIFY
    );

    historyBlockCharacteristic.setPermission(
        SECMODE_OPEN,
        SECMODE_NO_ACCESS
    );

    historyBlockCharacteristic.setMaxLen(
        ENV_BLOCK_MAX
    );

    historyBlockCharacteristic.begin();


    // ========================================================
    // CURRENT BATTERY
    // ========================================================

    batteryCurrentCharacteristic.setProperties(
        CHR_PROPS_READ
    );

    batteryCurrentCharacteristic.setPermission(
        SECMODE_OPEN,
        SECMODE_NO_ACCESS
    );

    batteryCurrentCharacteristic.setFixedLen(
        6
    );

    batteryCurrentCharacteristic.begin();


    // ========================================================
    // BATTERY HISTORY COMMAND
    // ========================================================

    batteryIndexCharacteristic.setProperties(
        CHR_PROPS_WRITE
    );

    batteryIndexCharacteristic.setPermission(
        SECMODE_NO_ACCESS,
        SECMODE_OPEN
    );

    batteryIndexCharacteristic.setFixedLen(
        2
    );

    batteryIndexCharacteristic.setWriteCallback(
        batteryIndexWriteCallback
    );

    batteryIndexCharacteristic.begin();


    // ========================================================
    // BATTERY HISTORY NOTIFICATION
    //
    // VARIABLE LENGTH.
    // ========================================================

    batteryBlockCharacteristic.setProperties(
        CHR_PROPS_NOTIFY
    );

    batteryBlockCharacteristic.setPermission(
        SECMODE_OPEN,
        SECMODE_NO_ACCESS
    );

    batteryBlockCharacteristic.setMaxLen(
        BATTERY_BLOCK_MAX
    );

    batteryBlockCharacteristic.begin();


    // Initial characteristic values

    updateMetadata();

    startAdvertising();
}



// ============================================================
// SETUP
// ============================================================

void setup()
{
    pinMode(
        LED_BUILTIN,
        OUTPUT
    );

    ledOff(
        LED_BUILTIN
    );


    // --------------------------------------------------------
    // I2C
    // --------------------------------------------------------

    Wire.begin();

    if (!sht4.begin())
    {
        // No Serial/debug in the low-power build.
        // Stay here if the sensor cannot be initialized.

        while (true)
        {
            ledOff(
                LED_BUILTIN
            );

            vTaskDelay(
                pdMS_TO_TICKS(1000)
            );
        }
    }


    // --------------------------------------------------------
    // SHT40 configuration
    // --------------------------------------------------------

    sht4.setPrecision(
        SHT4X_MED_PRECISION
    );

    sht4.setHeater(
        SHT4X_NO_HEATER
    );


    // --------------------------------------------------------
    // BLE
    // --------------------------------------------------------

    setupBLE();


    // --------------------------------------------------------
    // First measurements immediately
    // --------------------------------------------------------

    takeEnvironmentMeasurement();

    takeBatteryMeasurement();

    uint32_t now = millis();

    lastEnvironmentMeasurement =
        now;

    lastBatteryMeasurement =
        now;
}


// ============================================================
// LOOP
// ============================================================

void loop()
{
    // --------------------------------------------------------
    // Start requested streams
    // --------------------------------------------------------

    if (envStreamRequested)
    {
        envStreamRequested = false;

        beginEnvironmentStream();
    }


    if (batteryStreamRequested)
    {
        batteryStreamRequested = false;

        beginBatteryStream();
    }


    // --------------------------------------------------------
    // Stream data
    //
    // Environment gets priority.
    // --------------------------------------------------------

    if (envStreaming)
    {
        sendNextEnvironmentBlock();
    }
    else if (batteryStreaming)
    {
        sendNextBatteryBlock();
    }


    // --------------------------------------------------------
    // Measurements
    // --------------------------------------------------------

    uint32_t now = millis();


    if (
        (uint32_t)(
            now - lastEnvironmentMeasurement
        )
        >= ENV_INTERVAL_MS
    )
    {
        lastEnvironmentMeasurement +=
            ENV_INTERVAL_MS;

        takeEnvironmentMeasurement();
    }


    if (
        (uint32_t)(
            now - lastBatteryMeasurement
        )
        >= BATTERY_INTERVAL_MS
    )
    {
        lastBatteryMeasurement +=
            BATTERY_INTERVAL_MS;

        takeBatteryMeasurement();
    }


    // --------------------------------------------------------
    // Give FreeRTOS / SoftDevice an idle opportunity.
    // --------------------------------------------------------

    vTaskDelay(
        pdMS_TO_TICKS(1)
    );
}