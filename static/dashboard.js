/* Live sensor tiles, full sensor details, and dashboard controls.
 * charts.js handles history and plotting and is loaded before this file.
 */
const HISTORY_RANGE_STORAGE_KEY = "vmc.historyHours";
let historyHours = readHistoryRange();
let liveSensors = new Map();
let selectedSensorId = null;
const sensorGrid = document.getElementById("sensor-grid");
const sensorDialog = document.getElementById("sensor-details");

function readHistoryRange() {
  try {
    const saved = window.localStorage.getItem(HISTORY_RANGE_STORAGE_KEY);
    if (saved === "all") return "all";
    const hours = Number(saved);
    if (Number.isFinite(hours) && hours > 0) return hours;
  } catch {
    // Keep the dashboard usable when browser storage is unavailable.
  }
  return 24;
}

function isTouchDevice() {
  return (
    window.matchMedia("(hover: none)").matches ||
    window.matchMedia("(pointer: coarse)").matches
  );
}

function relativeTime(timestamp) {
  const elapsed = Math.floor(
    (Date.now() - new Date(timestamp).getTime()) / 1000,
  );
  if (!Number.isFinite(elapsed)) return "unknown";
  const seconds = Math.max(0, elapsed);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds} sec ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ${hours === 1 ? "hour" : "hours"} ago`;
  const days = Math.floor(hours / 24);
  return `${days} ${days === 1 ? "day" : "days"} ago`;
}

function escapeHtml(value) {
  return String(value).replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );
}

function formatNumber(value, digits = 0) {
  return Number.isFinite(value) ? value.toFixed(digits) : "—";
}

function formatTimestamp(timestamp) {
  const date = new Date(timestamp);
  return timestamp && Number.isFinite(date.getTime())
    ? date.toLocaleString()
    : "Unknown";
}

function rssiBars(rssi) {
  if (!Number.isFinite(rssi)) return 0;
  if (rssi >= -55) return 5;
  if (rssi >= -65) return 4;
  if (rssi >= -75) return 3;
  if (rssi >= -85) return 2;
  return 1;
}

function signalBars(count) {
  return Array.from(
    { length: 5 },
    (_, index) =>
      `<span class="signal-bar ${index < count ? "active" : ""}"></span>`,
  ).join("");
}

function batteryColor(percent) {
  if (percent <= 15) return "var(--battery-critical)";
  if (percent <= 30) return "var(--battery-low)";
  return "var(--battery)";
}

function sensorCard(sensor) {
  const name = escapeHtml(sensor.name);
  const bars = rssiBars(sensor.rssi);
  const battery = Number.isFinite(sensor.battery_percent)
    ? Math.max(0, Math.min(100, sensor.battery_percent))
    : null;
  const batteryFill = battery ? Math.max(5, battery) : 0;
  const days = sensor.days_since_full_charge;
  const chargeText =
    days == null
      ? "Last charge unknown"
      : `${days} ${days === 1 ? "day" : "days"} since 100%`;

  // CSS selects the compact button or the full card as space allows.
  return `<article class="sensor-card" aria-label="${name}" style="--sensor-color: ${escapeHtml(sensor.color)}">
      <button type="button" class="sensor-summary"
            data-sensor-id="${escapeHtml(sensor.sensor_id)}"
            aria-haspopup="dialog" aria-controls="sensor-details"
            aria-label="${name}: ${formatNumber(sensor.temperature, 1)} °C, ${formatNumber(sensor.humidity, 1)}% humidity. Open full sensor details">
        <span class="sensor-name">${name}</span>
        <span class="tile-reading">${formatNumber(sensor.temperature, 1)}<span class="tile-unit"> °C</span></span>
        <span class="tile-reading tile-humidity">${formatNumber(sensor.humidity, 1)}<span class="tile-unit"> %</span></span>
      </button>
      <div class="sensor-overview">
        <div class="sensor-top">
          <div class="sensor-identity">
            <span class="sensor-dot" aria-hidden="true"></span>
            <span class="sensor-name">${name}</span>
          </div>
          <span class="sequence">#${escapeHtml(sensor.sensor_id)} · SEQ ${escapeHtml(sensor.sequence ?? "Unknown")}</span>
        </div>
        <div class="main-values">
          <div>
            <div class="value-number">${formatNumber(sensor.temperature, 2)} <span class="value-unit">°C</span></div>
            <div class="value-label">Temperature</div>
          </div>
          <div>
            <div class="value-number">${formatNumber(sensor.humidity, 2)} <span class="value-unit">%</span></div>
            <div class="value-label">Humidity</div>
          </div>
        </div>
        <dl class="sensor-stats">
          <div>
            <dt>Battery</dt>
            <dd class="battery-info" title="Estimated battery charge">
              <span class="battery-shell" aria-hidden="true">
                <span class="battery-fill" style="width: ${batteryFill}%; background: ${batteryColor(battery)}"></span>
              </span>
              <span class="battery-percent">${battery == null ? "Unknown" : `${battery}%`}</span>
              <span class="battery-voltage">${formatNumber(sensor.battery_voltage, 3)} V</span>
            </dd>
          </div>
          <div>
            <dt>Signal</dt>
            <dd class="signal-info">
              <span class="signal-bars" aria-hidden="true">${signalBars(bars)}</span>
              <span class="signal-rssi">${formatNumber(sensor.rssi)} dBm</span>
              <span class="signal-count">${bars}/5</span>
            </dd>
          </div>
          <div>
            <dt>Last update</dt>
            <dd class="last-seen" title="${escapeHtml(formatTimestamp(sensor.timestamp))}">
              <span class="last-seen-age" data-timestamp="${escapeHtml(sensor.timestamp)}">${relativeTime(sensor.timestamp)}</span>
            </dd>
          </div>
          <div>
            <dt>Last full charge</dt>
            <dd class="charge-age" title="${escapeHtml(formatTimestamp(sensor.last_full_charge))}">${escapeHtml(chargeText)}</dd>
          </div>
        </dl>
      </div>
    </article>`;
}

// Hide the whole visual label when it cannot fit; preserve the full accessible name.
function updateSensorNameVisibility() {
  for (const name of sensorGrid.querySelectorAll(".sensor-name")) {
    name.classList.toggle(
      "is-overflowing",
      name.clientWidth > 0 && name.scrollWidth > name.clientWidth,
    );
  }
}
const sensorNameObserver = new ResizeObserver(updateSensorNameVisibility);
sensorNameObserver.observe(sensorGrid);
window.addEventListener("resize", updateSensorNameVisibility);
document.fonts.ready.then(updateSensorNameVisibility);

function renderSensorDetails() {
  const sensor = liveSensors.get(selectedSensorId);
  if (!sensor) {
    sensorDialog.close();
    return;
  }
  const bars = rssiBars(sensor.rssi);
  const battery = Number.isFinite(sensor.battery_percent)
    ? Math.max(0, Math.min(100, sensor.battery_percent))
    : null;
  const chargeAge = sensor.days_since_full_charge;
  const chargeText =
    chargeAge == null
      ? "Unknown"
      : `${chargeAge} ${chargeAge === 1 ? "day" : "days"} since 100%`;
  sensorDialog.style.setProperty("--sensor-color", sensor.color);
  document.getElementById("sensor-details-title").textContent = sensor.name;
  document.getElementById("sensor-details-id").textContent =
    `Sensor ${sensor.sensor_id}`;
  document.getElementById("sensor-details-body").innerHTML = `
        <dl class="sensor-detail-grid">
            <div><dt>Temperature</dt><dd class="detail-metric">${formatNumber(sensor.temperature, 2)} °C</dd></div>
            <div><dt>Humidity</dt><dd class="detail-metric">${formatNumber(sensor.humidity, 2)} %</dd></div>
            <div><dt>Battery estimate</dt><dd style="color: ${battery == null ? "inherit" : batteryColor(battery)}">${battery == null ? "Unknown" : `${battery}%`}</dd></div>
            <div><dt>Battery voltage</dt><dd>${formatNumber(sensor.battery_voltage, 3)} V</dd></div>
            <div><dt>Signal strength</dt><dd class="signal-info">
                <span class="signal-bars" aria-hidden="true">${signalBars(bars)}</span>
                <span>${formatNumber(sensor.rssi)} dBm · ${bars}/5</span>
            </dd></div>
            <div><dt>Measurement sequence</dt><dd>${escapeHtml(sensor.sequence ?? "Unknown")}</dd></div>
            <div class="detail-wide"><dt>Last seen</dt><dd>
                <span class="last-seen-age" data-timestamp="${escapeHtml(sensor.timestamp)}">${relativeTime(sensor.timestamp)}</span>
                <time class="detail-timestamp" datetime="${escapeHtml(sensor.timestamp)}">${escapeHtml(formatTimestamp(sensor.timestamp))}</time>
            </dd></div>
            <div class="detail-wide"><dt>Last full charge</dt><dd>${escapeHtml(chargeText)}
                <span class="detail-timestamp">${escapeHtml(formatTimestamp(sensor.last_full_charge))}</span>
            </dd></div>
            <div><dt>Protocol version</dt><dd>${escapeHtml(sensor.protocol ?? "Unknown")}</dd></div>
        </dl>`;
}

sensorGrid.addEventListener("click", (event) => {
  const tile = event.target.closest("[data-sensor-id]");
  if (!tile || !sensorGrid.contains(tile)) return;
  selectedSensorId = tile.dataset.sensorId;
  if (!liveSensors.has(selectedSensorId)) return;
  renderSensorDetails();
  sensorDialog.showModal();
});

document
  .getElementById("sensor-details-close")
  .addEventListener("click", () => sensorDialog.close());
sensorDialog.addEventListener("click", (event) => {
  if (event.target !== sensorDialog) return;
  const bounds = sensorDialog.getBoundingClientRect();
  if (
    event.clientX < bounds.left ||
    event.clientX > bounds.right ||
    event.clientY < bounds.top ||
    event.clientY > bounds.bottom
  ) {
    sensorDialog.close();
  }
});
sensorDialog.addEventListener("close", () => {
  // Polling may have replaced the original tile while the dialog was open.
  const tile = [...sensorGrid.querySelectorAll("[data-sensor-id]")].find(
    (element) => element.dataset.sensorId === selectedSensorId,
  );
  tile?.focus({ preventScroll: true });
  selectedSensorId = null;
});

function updateLastSeenLabels() {
  document.querySelectorAll(".last-seen-age").forEach((element) => {
    element.textContent = relativeTime(element.dataset.timestamp);
  });
}

async function updateSensors() {
  try {
    const response = await fetch("/api/sensors", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const sensors = Object.values(await response.json()).sort(
      (a, b) => a.sensor_id - b.sensor_id,
    );
    liveSensors = new Map(
      sensors.map((sensor) => [String(sensor.sensor_id), sensor]),
    );
    const focusedId = document.activeElement?.dataset.sensorId;
    sensorGrid.style.setProperty(
      "--sensor-columns",
      Math.max(1, Math.min(4, sensors.length)),
    );
    sensorGrid.innerHTML = sensors.length
      ? sensors.map(sensorCard).join("")
      : '<p class="sensor-grid-empty">Waiting for sensor readings…</p>';
    updateSensorNameVisibility();
    if (sensorDialog.open) renderSensorDetails();
    else if (focusedId) {
      [...sensorGrid.querySelectorAll("[data-sensor-id]")]
        .find((tile) => tile.dataset.sensorId === focusedId)
        ?.focus({ preventScroll: true });
    }

    const statusResponse = await fetch("/api/status", { cache: "no-store" });
    const scannerStatus = statusResponse.ok
      ? await statusResponse.json()
      : null;
    document.getElementById("status").textContent = scannerStatus?.running
      ? sensors.length
        ? "BLE scanner active"
        : "BLE scanner active - waiting for advertisements"
      : "BLE scanner restarting";
  } catch (error) {
    document.getElementById("status").textContent = "Server connection error";
  }
}

// Changing the range reuses the cached history and persists the preference.
document
  .querySelector(".history-controls")
  .addEventListener("click", (event) => {
    const button = event.target.closest("button[data-hours]");
    if (!button) return;
    historyHours =
      button.dataset.hours === "all" ? "all" : Number(button.dataset.hours);
    try {
      window.localStorage.setItem(
        HISTORY_RANGE_STORAGE_KEY,
        String(historyHours),
      );
    } catch {
      // The range can still be changed when browser storage is unavailable.
    }
    for (const other of document.querySelectorAll(".history-controls button")) {
      const selected = other === button;
      other.classList.toggle("active", selected);
      other.setAttribute("aria-pressed", String(selected));
    }
    updateHistory(true);
  });

updateSensors();
updateHistory(true);
setInterval(updateSensors, 10000);
setInterval(updateLastSeenLabels, 1000);
setInterval(updateHistory, 60000);
