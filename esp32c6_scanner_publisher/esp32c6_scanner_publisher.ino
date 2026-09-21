#include <Arduino.h>
#include <NimBLEDevice.h>

static const char* NRF_ADDR = "1f:bf:bc:24:db:52";
static const char* S3_ADDR  = "28:84:85:6d:bb:71";

volatile uint32_t totalReports = 0;
volatile uint32_t nrfReports   = 0;
volatile uint32_t s3Reports    = 0;

class ScanCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {

    totalReports++;

    std::string addr = dev->getAddress().toString();

    bool isNRF = (addr == NRF_ADDR);
    bool isS3  = (addr == S3_ADDR);

    if (!isNRF && !isS3) {
      return;
    }

    if (isNRF) {
      nrfReports++;
    }

    if (isS3) {
      s3Reports++;
    }

    Serial.println();

    if (isNRF) {
      Serial.println("========== ZEPHYR NRF FOUND ==========");
    } else {
      Serial.println("========== ESP32-S3 FOUND =============");
    }

    Serial.print("Address : ");
    Serial.println(addr.c_str());

    Serial.print("RSSI    : ");
    Serial.print(dev->getRSSI());
    Serial.println(" dBm");

    Serial.print("RAW     : ");

    const std::vector<uint8_t>& payload = dev->getPayload();

    for (uint8_t b : payload) {
      if (b < 0x10) {
        Serial.print('0');
      }

      Serial.print(b, HEX);
      Serial.print(' ');
    }

    Serial.println();

    if (isNRF) {
      Serial.print("nRF count : ");
      Serial.println(nrfReports);
    }

    if (isS3) {
      Serial.print("S3 count  : ");
      Serial.println(s3Reports);
    }

    Serial.println("=======================================");
  }
};

NimBLEScan* scan;

void setup() {

  Serial.begin(115200);
  delay(1500);

  Serial.println();
  Serial.println("====================================");
  Serial.println("ESP32-C6 BLE A/B RECEIVER TEST");
  Serial.println("====================================");

  Serial.print("Zephyr nRF : ");
  Serial.println(NRF_ADDR);

  Serial.print("ESP32-S3   : ");
  Serial.println(S3_ADDR);

  Serial.print("NimBLE     : ");
  Serial.println(NIMBLE_CPP_VERSION_STR);

  NimBLEDevice::init("");

  scan = NimBLEDevice::getScan();

  scan->setScanCallbacks(new ScanCallbacks(), false);

  // Passive scan
  scan->setActiveScan(false);

  // Continuous 100 ms scan window.
  scan->setInterval(100);
  scan->setWindow(100);

  // Important: report repeated advertisements.
  scan->setDuplicateFilter(false);

  // Don't accumulate scan results in RAM.
  scan->setMaxResults(0);

  bool ok = scan->start(0, false, true);

  Serial.print("Scan start : ");
  Serial.println(ok ? "OK" : "FAILED");

  Serial.println();
  Serial.println("Both transmitters should now be running.");
  Serial.println();
}

void loop() {

  static uint32_t last = 0;

  if (millis() - last >= 5000) {

    last = millis();

    Serial.println();
    Serial.println("============== STATUS ==============");

    Serial.print("All BLE reports : ");
    Serial.println(totalReports);

    Serial.print("Zephyr nRF      : ");
    Serial.println(nrfReports);

    Serial.print("ESP32-S3        : ");
    Serial.println(s3Reports);

    Serial.print("Scan running    : ");
    Serial.println(scan->isScanning() ? "YES" : "NO");

    Serial.println("====================================");
  }

  delay(10);
}