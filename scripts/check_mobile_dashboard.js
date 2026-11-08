#!/usr/bin/env node
// Browser regression check with four simulated sensors; never touches BLE or CSVs.
// Requires Puppeteer. Set CHROME_BIN if Chromium is installed separately.
// Optional: CHART_JS_PATH caches the CDN dependency; SCREENSHOT_DIR saves previews.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const puppeteer = require("puppeteer");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "vmc_server.py"), "utf8");
const html = source
  .match(/PAGE = r"""([\s\S]*?)"""/)[1]
  .replace(/\{\{ url_for\('static', filename='([^']+)'\) \}\}/g, "/static/$1");
const now = Date.now();
const names = ["salon", "Parents", "buanderie", "Outside"];
const colors = ["#ff5a5f", "#35d07f", "#5da9ff", "#f5b942"];
const sensors = Object.fromEntries(
  names.map((name, index) => [
    index + 1,
    {
      sensor_id: index + 1,
      name,
      color: colors[index],
      protocol: 1,
      temperature: index === 3 ? 8.12 : 21.54 + index,
      humidity: 45.25 + index * 7,
      battery_mv: 3950,
      battery_voltage: 3.95,
      battery_percent: 75,
      sequence: 101 + index,
      rssi: -55 - index * 8,
      timestamp: new Date(now).toISOString(),
      last_full_charge: new Date(now - 5 * 86400000).toISOString(),
      days_since_full_charge: 5,
    },
  ]),
);
const history = Object.fromEntries(
  Object.entries(sensors).map(([id, sensor]) => [
    id,
    {
      name: sensor.name,
      color: sensor.color,
      points: Array.from({ length: 145 }, (_, index) => ({
        timestamp_ms: now - (144 - index) * 600000,
        temperature: sensor.temperature + Math.sin(index / 12),
        humidity: sensor.humidity + Math.sin(index / 10) * 5,
      })),
    },
  ]),
);
let fixtureSensors = sensors;
let failRequests = false;

const server = http.createServer((request, response) => {
  if (request.url.startsWith("/api/")) {
    response.setHeader("Content-Type", "application/json");
    if (failRequests) {
      response.writeHead(503).end("{}");
      return;
    }
    const data =
      request.url === "/api/sensors"
        ? fixtureSensors
        : request.url === "/api/status"
          ? { running: true }
          : history;
    response.end(JSON.stringify(data));
  } else if (request.url.startsWith("/static/")) {
    const filename = path.basename(request.url);
    response.setHeader(
      "Content-Type",
      filename.endsWith(".css") ? "text/css" : "text/javascript",
    );
    response.end(fs.readFileSync(path.join(root, "static", filename)));
  } else {
    response.setHeader("Content-Type", "text/html");
    response.end(html);
  }
});

async function check() {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_BIN || undefined,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const errors = [];
    const page = await browser.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    if (process.env.CHART_JS_PATH) {
      const chartJs = fs.readFileSync(process.env.CHART_JS_PATH, "utf8");
      await page.setRequestInterception(true);
      page.on("request", (request) => {
        if (request.url().includes("cdn.jsdelivr.net/npm/chart.js")) {
          request.respond({ contentType: "text/javascript", body: chartJs });
        } else request.continue();
      });
    }
    await page.setViewport({
      width: 390,
      height: 844,
      isMobile: true,
      hasTouch: true,
    });
    await page.goto(`http://127.0.0.1:${server.address().port}`, {
      waitUntil: "networkidle0",
    });
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".sensor-card").length === 4 &&
        Chart.getChart("temperature-chart") &&
        Chart.getChart("humidity-chart"),
    );

    for (const [width, height, compact] of [
      [320, 568, true],
      [360, 640, true],
      [390, 844, true],
      [430, 932, true],
      [667, 375, true],
      [844, 390, true],
      [700, 700, true],
      [701, 700, false],
      [768, 1024, false],
      [1024, 600, false],
      [1280, 500, false],
      [1280, 800, false],
    ]) {
      await page.setViewport({
        width,
        height,
        isMobile: width <= 844,
        hasTouch: width <= 844,
      });
      await page.waitForFunction(
        () =>
          document.querySelector(".dashboard").clientHeight ===
          window.innerHeight,
      );
      const layout = await page.evaluate(() => {
        const cards = [...document.querySelectorAll(".sensor-card")];
        const charts = [...document.querySelectorAll(".chart-container")];
        const boxes = cards.map((card) => card.getBoundingClientRect());
        return {
          rows: new Set(boxes.map((box) => Math.round(box.top))).size,
          overflow: document.documentElement.scrollWidth > innerWidth,
          namesFit: cards.every((card) => {
            const names = [...card.querySelectorAll(".sensor-name")].filter(
              (element) =>
                element.checkVisibility({ visibilityProperty: true }),
            );
            return names.every((name) => name.scrollWidth <= name.clientWidth);
          }),
          charts: charts.map((element) => {
            const box = element.getBoundingClientRect();
            return {
              top: box.top,
              bottom: box.bottom,
              height: box.height,
              width: box.width,
            };
          }),
          lastTileBottom: Math.max(...boxes.map((box) => box.bottom)),
          text: cards.map((card) => card.innerText),
          detailsButtons: cards.filter((card) =>
            card.querySelector(".sensor-summary").checkVisibility(),
          ).length,
        };
      });
      assert.equal(
        layout.rows,
        compact || width >= 1024 ? 1 : 2,
        `${width}px: compact row or full desktop grid`,
      );
      assert.equal(
        layout.overflow,
        false,
        `${width}px: no horizontal overflow`,
      );
      assert(layout.namesFit, `${width}px: sensor names fit`);
      assert.equal(
        layout.detailsButtons,
        compact ? 4 : 0,
        "Only compact tiles need a details button",
      );
      if (compact) {
        assert(
          !/battery|voltage|dBm|SEQ|charge/i.test(layout.text.join(" ")),
          "Mobile overview only contains key readings",
        );
      } else {
        layout.text.forEach((text, index) => {
          for (const value of [
            "Temperature",
            "Humidity",
            "75%",
            "3.950 V",
            "5 days since 100%",
            `${sensors[index + 1].rssi} dBm`,
            `SEQ ${sensors[index + 1].sequence}`,
            "Last update",
            sensors[index + 1].temperature.toFixed(2),
            sensors[index + 1].humidity.toFixed(2),
          ]) {
            assert(
              text.toLowerCase().includes(value.toLowerCase()),
              `${width}px: ${names[index]} shows ${value} without a tap`,
            );
          }
        });
      }
      assert(
        layout.charts.every(
          (chart) =>
            chart.top >= layout.lastTileBottom &&
            chart.bottom <= height &&
            chart.height >= 100 &&
            chart.width > 100,
        ),
        `${width}×${height}: both charts must remain visible below the tiles: ${JSON.stringify(layout.charts)}`,
      );
    }

    await page.setViewport({
      width: 390,
      height: 844,
      isMobile: true,
      hasTouch: true,
    });
    for (let id = 1; id <= 4; id++) {
      await page.click(`[data-sensor-id="${id}"]`);
      assert(await page.$eval("#sensor-details", (dialog) => dialog.open));
      assert.equal(
        await page.$eval(
          "#sensor-details-title",
          (element) => element.textContent,
        ),
        names[id - 1],
      );
      const details = await page.$eval(
        "#sensor-details-body",
        (element) => element.textContent,
      );
      for (const field of [
        "Temperature",
        "Humidity",
        "Battery estimate",
        "75%",
        "3.950 V",
        "Signal strength",
        "Measurement sequence",
        "Last seen",
        "Last full charge",
        "Protocol version",
      ]) {
        assert(
          details.includes(field),
          `${names[id - 1]} details include ${field}`,
        );
      }
      await page.keyboard.press("Escape");
      await page.waitForFunction(
        () => !document.getElementById("sensor-details").open,
      );
    }

    await page.click('[data-sensor-id="4"]');
    fixtureSensors = {
      ...sensors,
      4: { ...sensors[4], temperature: 9.87, sequence: 105 },
    };
    await page.evaluate(() => updateSensors());
    assert(
      await page.$eval("#sensor-details", (dialog) => dialog.open),
      "Polling keeps dialog open",
    );
    assert(
      (
        await page.$eval(
          "#sensor-details-body",
          (element) => element.textContent,
        )
      ).includes("9.87 °C"),
    );
    await page.click("#sensor-details-close");
    await page.waitForFunction(
      () => document.activeElement?.dataset.sensorId === "4",
    );
    await page.keyboard.press("Enter");
    assert(
      await page.$eval("#sensor-details", (dialog) => dialog.open),
      "Keyboard can open a tile",
    );
    await page.mouse.click(2, 2);
    await page.waitForFunction(
      () => !document.getElementById("sensor-details").open,
    );

    for (const range of ["1", "12", "24", "168", "all"]) {
      await page.click(`.history-controls [data-hours="${range}"]`);
      assert.equal(
        await page.evaluate(() => localStorage.getItem("vmc.historyHours")),
        range,
      );
      assert.equal(
        await page.$eval(
          ".history-controls .active",
          (element) => element.dataset.hours,
        ),
        range,
      );
    }

    failRequests = true;
    await page.evaluate(() => updateSensors());
    assert.equal(
      await page.$eval("#status", (element) => element.textContent),
      "Server connection error",
    );
    assert.equal(await page.$$eval(".sensor-card", (cards) => cards.length), 4);
    failRequests = false;
    fixtureSensors = {};
    await page.evaluate(() => updateSensors());
    assert.equal(await page.$$eval(".sensor-card", (cards) => cards.length), 0);
    assert(
      (
        await page.$eval("#sensor-grid", (element) => element.textContent)
      ).includes("Waiting"),
    );
    fixtureSensors = sensors;
    await page.evaluate(() => updateSensors());

    // Long names must disappear completely instead of being clipped or shortened.
    const longName = "Outside temperature and humidity sensor by the garden";
    fixtureSensors = { ...sensors, 4: { ...sensors[4], name: longName } };
    await page.evaluate(() => updateSensors());
    for (const [width, height] of [
      [320, 568],
      [1280, 800],
    ]) {
      await page.setViewport({
        width,
        height,
        isMobile: width < 700,
        hasTouch: width < 700,
      });
      await page.waitForFunction(() => {
        const card = document.querySelectorAll(".sensor-card")[3];
        return [...card.querySelectorAll(".sensor-name")].every(
          (name) => !name.checkVisibility({ visibilityProperty: true }),
        );
      });
      assert.equal(
        await page.$$eval(".sensor-card", (cards) =>
          cards[3].getAttribute("aria-label"),
        ),
        longName,
      );
    }
    await page.setViewport({
      width: 390,
      height: 844,
      isMobile: true,
      hasTouch: true,
    });
    await page.click('[data-sensor-id="4"]');
    assert.equal(
      await page.$eval(
        "#sensor-details-title",
        (element) => element.textContent,
      ),
      longName,
    );
    await page.click("#sensor-details-close");
    // A subsequent reading with a short name restores the whole label.
    fixtureSensors = sensors;
    await page.evaluate(() => updateSensors());
    assert.equal(
      await page.$$eval(".sensor-card", (cards) => {
        const name = cards[3].querySelector(".sensor-summary .sensor-name");
        return name.checkVisibility({ visibilityProperty: true })
          ? name.textContent
          : "";
      }),
      "Outside",
    );

    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, { recursive: true });
      await page.screenshot({
        path: path.join(process.env.SCREENSHOT_DIR, "mobile-dashboard.png"),
      });
      await page.click('[data-sensor-id="4"]');
      await page.screenshot({
        path: path.join(process.env.SCREENSHOT_DIR, "sensor-details.png"),
      });
      await page.click("#sensor-details-close");
      await page.setViewport({
        width: 1280,
        height: 800,
        isMobile: false,
        hasTouch: false,
      });
      await page.waitForFunction(() =>
        document.querySelectorAll(".sensor-card").length === 4 &&
        Chart.getChart("temperature-chart")?.data.datasets.length === 4 &&
        Chart.getChart("humidity-chart")?.data.datasets.length === 4);
      await page.screenshot({
        path: path.join(process.env.SCREENSHOT_DIR, "desktop-dashboard.png"),
      });
    }
    assert.deepEqual(errors, [], "No browser errors");
    console.log(
      "PASS: progressive cards across 12 screen sizes, full-or-hidden names, visible charts, all sensor details, live updates, keyboard/Escape/backdrop, saved ranges, empty and error states.",
    );
  } finally {
    await browser.close();
  }
}

check()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(() => server.close());
