/* History aggregation, chart rendering, and chart interactions. */
let temperatureChart = null;
let humidityChart = null;
let cachedHistory = null;
let historyRequest = null;
// ============================================================
// Graph time formatting
// ============================================================
function formatGraphTime(milliseconds) {
  const date = new Date(milliseconds);
  if (historyHours !== "all" && Number(historyHours) <= 24) {
    return date.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  if (historyHours === 168) {
    return date.toLocaleString([], {
      weekday: "short",
      hour: "2-digit",
    });
  }
  const longRange = historyHours === "all" || Number(historyHours) >= 8760;
  return date.toLocaleDateString(
    [],
    longRange
      ? { month: "short", year: "2-digit" }
      : { month: "short", day: "numeric" },
  );
}
// ============================================================
// Find nearest point in a dataset
//
// Uses binary search because historical points are sorted by
// timestamp.
// ============================================================
function nearestPoint(points, targetX) {
  if (!points || points.length === 0) {
    return null;
  }
  if (targetX <= points[0].x) {
    return points[0];
  }
  const lastIndex = points.length - 1;
  if (targetX >= points[lastIndex].x) {
    return points[lastIndex];
  }
  let low = 0;
  let high = lastIndex;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    const x = points[mid].x;
    if (x === targetX) {
      return points[mid];
    }
    if (x < targetX) {
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  const before = points[high];
  const after = points[low];
  if (Math.abs(targetX - before.x) <= Math.abs(after.x - targetX)) {
    return before;
  }
  return after;
}
// ============================================================
// Dataset
// ============================================================
function makeDataset(sensor, field) {
  const points = sensor.points.map((point) => ({
    x: point.timestamp_ms,
    y: point[field],
  }));
  points.sort((a, b) => a.x - b.x);
  return {
    label: sensor.name,
    data: points,
    borderColor: sensor.color,
    backgroundColor: sensor.color,
    borderWidth: 2.2,
    pointRadius: points.length === 1 ? 3 : points.length <= 30 ? 2 : 0,
    pointHoverRadius: 0,
    pointHitRadius: 0,
    tension: 0.15,
    spanGaps: false,
  };
}
// ============================================================
// Desktop crosshair + combined tooltip plugin
// ============================================================
const desktopInspectorPlugin = {
  id: "desktopInspector",
  afterEvent(chart, args) {
    if (isTouchDevice()) {
      return;
    }
    const event = args.event;
    if (event.type === "mouseout") {
      if (chart.$inspectionX !== null) {
        chart.$inspectionX = null;
        chart.$inspectionTime = null;
        chart.draw();
      }
      return;
    }
    if (event.type !== "mousemove") {
      return;
    }
    const area = chart.chartArea;
    if (!area) {
      return;
    }
    const x = event.x;
    const y = event.y;
    if (x < area.left || x > area.right || y < area.top || y > area.bottom) {
      if (chart.$inspectionX !== null) {
        chart.$inspectionX = null;
        chart.$inspectionTime = null;
        chart.draw();
      }
      return;
    }
    chart.$inspectionX = x;
    chart.$inspectionTime = chart.scales.x.getValueForPixel(x);
    args.changed = true;
  },
  afterDraw(chart) {
    if (isTouchDevice()) {
      return;
    }
    if (chart.$inspectionX == null || chart.$inspectionTime == null) {
      return;
    }
    const ctx = chart.ctx;
    const area = chart.chartArea;
    const x = chart.$inspectionX;
    // -----------------------------------------------
    // Vertical guide line
    // -----------------------------------------------
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(210,225,242,0.28)";
    ctx.stroke();
    ctx.restore();
    // -----------------------------------------------
    // Collect nearest value from every sensor
    // -----------------------------------------------
    const rows = [];
    for (const dataset of chart.data.datasets) {
      const point = nearestPoint(dataset.data, chart.$inspectionTime);
      if (!point) {
        continue;
      }
      rows.push({
        label: dataset.label,
        color: dataset.borderColor,
        value: point.y,
        timestamp: point.x,
      });
    }
    if (rows.length === 0) {
      return;
    }
    // Use cursor time for the heading.
    const heading = new Date(chart.$inspectionTime).toLocaleString();
    // -----------------------------------------------
    // Tooltip geometry
    // -----------------------------------------------
    ctx.save();
    const titleFont =
      "600 11px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    const bodyFont =
      "600 11px system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
    ctx.font = titleFont;
    let width = ctx.measureText(heading).width;
    for (const row of rows) {
      ctx.font = bodyFont;
      const unit = chart.$measurementType === "temperature" ? " °C" : " %";
      const text = row.label + "   " + Number(row.value).toFixed(2) + unit;
      width = Math.max(width, ctx.measureText(text).width + 20);
    }
    const padding = 10;
    const titleHeight = 17;
    const rowHeight = 18;
    width += padding * 2;
    const height = padding * 2 + titleHeight + rows.length * rowHeight;
    let boxX = x + 12;
    let boxY = area.top + 8;
    if (boxX + width > area.right) {
      boxX = x - width - 12;
    }
    if (boxX < area.left) {
      boxX = area.left + 4;
    }
    // -----------------------------------------------
    // Rounded tooltip background
    // -----------------------------------------------
    const radius = 8;
    ctx.beginPath();
    ctx.roundRect(boxX, boxY, width, height, radius);
    ctx.fillStyle = "rgba(20,29,42,0.97)";
    ctx.fill();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(72,91,116,0.95)";
    ctx.stroke();
    // -----------------------------------------------
    // Tooltip title
    // -----------------------------------------------
    ctx.font = titleFont;
    ctx.fillStyle = "#aab7c7";
    ctx.textBaseline = "top";
    ctx.fillText(heading, boxX + padding, boxY + padding);
    // -----------------------------------------------
    // Sensor rows
    // -----------------------------------------------
    let rowY = boxY + padding + titleHeight;
    for (const row of rows) {
      const unit = chart.$measurementType === "temperature" ? " °C" : " %";
      // Sensor color dot
      ctx.beginPath();
      ctx.arc(boxX + padding + 4, rowY + 7, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = row.color;
      ctx.fill();
      // Sensor label
      ctx.font = bodyFont;
      ctx.fillStyle = "#eef3f8";
      const text = row.label + "   " + Number(row.value).toFixed(2) + unit;
      ctx.fillText(text, boxX + padding + 14, rowY);
      rowY += rowHeight;
    }
    ctx.restore();
  },
};
// ============================================================
// Chart options
// ============================================================
function alignedTimeTicks(scale) {
  const minute = 60000;
  const span = scale.max - scale.min;
  const target = window.innerWidth <= 700 ? 5 : 9;
  if (span > 60 * 86400000) {
    const months = span / (30.4375 * 86400000);
    const step =
      [1, 2, 3, 6, 12, 24, 60, 120].find((value) => months / value <= target) ||
      Math.ceil(months / target / 12) * 12;
    const minorStep = step >= 12 ? 12 : 1;
    const date = new Date(scale.min);
    date.setDate(1);
    date.setHours(0, 0, 0, 0);
    const monthIndex = date.getFullYear() * 12 + date.getMonth();
    date.setFullYear(Math.floor((Math.floor(monthIndex / step) * step) / 12));
    date.setMonth((Math.floor(monthIndex / step) * step) % 12);
    const ticks = [];
    while (date.getTime() <= scale.max) {
      const value = date.getTime();
      const index = date.getFullYear() * 12 + date.getMonth();
      if (value >= scale.min) {
        ticks.push({ value, major: index % step === 0 });
      }
      date.setMonth(date.getMonth() + minorStep);
    }
    scale.ticks = ticks;
    scale.options.ticks.maxTicksLimit = ticks.length;
    return;
  }
  const intervals = [10, 20, 30, 60, 120, 180, 360, 720, 1440, 2880, 10080];
  const spacing =
    intervals.find((value) => span / (value * minute) <= target) || 10080;
  const ticks = [];
  if (spacing >= 1440) {
    const date = new Date(scale.min);
    date.setHours(0, 0, 0, 0);
    if (date.getTime() < scale.min) date.setDate(date.getDate() + 1);
    // Include the preceding interval so minor ticks reach both edges.
    date.setDate(date.getDate() - spacing / 1440);
    const majorDays = spacing / 1440;
    const minorHours = majorDays === 1 ? 6 : 24;
    while (date.getTime() <= scale.max) {
      const start = new Date(date);
      date.setDate(date.getDate() + majorDays);
      // Advance through local calendar boundaries, including DST changes.
      const minor = new Date(start);
      while (minor.getTime() < date.getTime()) {
        const value = minor.getTime();
        if (value >= scale.min && value <= scale.max) {
          ticks.push({ value, major: value === start.getTime() });
        }
        minor.setHours(minor.getHours() + minorHours);
      }
    }
  } else {
    // Use clock intervals rather than dividing each label gap into quarters.
    // In particular, 09:00 -> 12:00 has only 10:00 and 11:00 between them.
    const majorStep = spacing * minute;
    const minorMinutes =
      spacing >= 720
        ? 180
        : spacing >= 60
          ? 60
          : spacing === 30
            ? 10
            : spacing / 2;
    const step = minorMinutes * minute;
    const offset = new Date(scale.min).getTimezoneOffset() * minute;
    let value = Math.ceil((scale.min - offset) / step) * step + offset;
    for (; value <= scale.max; value += step) {
      ticks.push({ value, major: (value - offset) % majorStep === 0 });
    }
  }
  scale.ticks = ticks;
  // Chart.js also uses this limit when drawing grid lines and tick marks.
  scale.options.ticks.maxTicksLimit = ticks.length;
}
// Subdivide Chart.js's existing numeric graduations without moving labels.
function intermediateValueTicks(scale) {
  const majors = scale.ticks;
  const ticks = [];
  majors.forEach((tick, index) => {
    ticks.push({ ...tick, major: true });
    if (index + 1 < majors.length) {
      const step = (majors[index + 1].value - tick.value) / 4;
      for (let part = 1; part < 4; part++) {
        ticks.push({ value: tick.value + step * part, major: false });
      }
    }
  });
  scale.ticks = ticks;
  // Chart.js also uses this limit when drawing grid lines and tick marks.
  scale.options.ticks.maxTicksLimit = ticks.length;
}
function chartOptions(type, datasets) {
  const now = Date.now();
  let first = Infinity;
  let last = -Infinity;
  for (const dataset of datasets) {
    for (const point of dataset.data) {
      if (Number.isFinite(point.x)) {
        first = Math.min(first, point.x);
        last = Math.max(last, point.x);
      }
    }
  }
  const axisEnd = historyHours === "all" && Number.isFinite(last) ? last : now;
  const axisStart =
    historyHours === "all"
      ? Number.isFinite(first)
        ? Math.min(first, axisEnd - 600000)
        : axisEnd - 86400000
      : axisEnd - Number(historyHours) * 3600000;
  const touch = isTouchDevice();
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    normalized: false,
    parsing: false,
    // We use our own desktop inspector instead of the
    // standard Chart.js tooltip.
    interaction: {
      mode: undefined,
      intersect: false,
    },
    events: touch ? [] : ["mousemove", "mouseout"],
    layout: {
      padding: {
        left: 2,
        right: 5,
        top: 0,
        bottom: 0,
      },
    },
    plugins: {
      legend: {
        display: true,
        position: "top",
        align: "end",
        labels: {
          color: "#9eabba",
          boxWidth: 18,
          boxHeight: 2,
          padding: 13,
          font: {
            size: 10,
            weight: "600",
          },
        },
      },
      // Disable normal Chart.js tooltip.
      // Our custom inspector displays all sensors.
      tooltip: {
        enabled: false,
      },
    },
    scales: {
      x: {
        type: "linear",
        min: axisStart,
        max: axisEnd,
        afterBuildTicks: alignedTimeTicks,
        border: {
          color: "#2a3545",
        },
        grid: {
          tickLength: 8,
          tickColor: (context) =>
            context.tick.major
              ? "rgba(190,210,235,0.85)"
              : "rgba(170,190,215,0.45)",
          tickWidth: (context) => (context.tick.major ? 2 : 1),
          color: (context) =>
            context.tick.major
              ? "rgba(170,190,215,0.24)"
              : "rgba(170,190,215,0.11)",
          drawTicks: true,
        },
        ticks: {
          color: window.innerWidth > 700 ? "#d5e1ef" : "#a5b3c5",
          padding: 7,
          maxTicksLimit: window.innerWidth <= 700 ? 5 : 9,
          maxRotation: 0,
          autoSkip: false,
          font: {
            size: window.innerWidth > 700 ? 13 : 10,
            weight: window.innerWidth > 700 ? "600" : "500",
          },
          callback: function (value, index, ticks) {
            if (!ticks[index].major) return "";
            return formatGraphTime(value);
          },
        },
      },
      y: {
        // Reset the label budget before generating the next set of ticks.
        beforeBuildTicks: (scale) => {
          scale.options.ticks.maxTicksLimit = 6;
        },
        afterBuildTicks: intermediateValueTicks,
        border: {
          display: false,
        },
        grid: {
          color: (context) =>
            context.tick.major
              ? "rgba(170,190,215,0.16)"
              : "rgba(170,190,215,0.06)",
          drawTicks: true,
        },
        ticks: {
          autoSkip: false,
          color: "#8795a7",
          padding: 8,
          maxTicksLimit: 6,
          font: {
            size: 10,
            weight: "500",
          },
          callback: function (value, index, ticks) {
            if (!ticks[index].major) return "";
            if (type === "temperature") {
              return Number(value).toFixed(1) + "°";
            }
            return Number(value).toFixed(0) + "%";
          },
        },
      },
    },
  };
}
// ============================================================
// Create chart
// ============================================================
function createChart(canvasId, type, datasets) {
  const chart = new Chart(document.getElementById(canvasId), {
    type: "line",
    data: {
      datasets: datasets,
    },
    options: chartOptions(type, datasets),
    plugins: [desktopInspectorPlugin],
  });
  chart.$measurementType = type;
  chart.$inspectionX = null;
  chart.$inspectionTime = null;
  return chart;
}
// ============================================================
// History signature
// ============================================================
// ============================================================
// Update graph history
// ============================================================
// Familiar short dashboard presets, followed by a logarithmic 1-2-5 year ladder.
// Inspiration: Grafana relative ranges and https://d3js.org/d3-array/ticks.
const CORE_HISTORY_RANGES = [
  { hours: 1, label: "1h" },
  { hours: 12, label: "12h" },
  { hours: 24, label: "1d" },
  { hours: 168, label: "7d" },
];
function availableHistoryRanges(history) {
  let first = Infinity;
  let last = -Infinity;
  for (const sensor of Object.values(history)) {
    for (const point of sensor.points) {
      if (!Number.isFinite(point.timestamp_ms)) continue;
      first = Math.min(first, point.timestamp_ms);
      last = Math.max(last, point.timestamp_ms);
    }
  }
  const spanHours = Math.max(0, (last - first) / 3600000);
  const ranges = [...CORE_HISTORY_RANGES];
  for (const days of [30, 90]) {
    if (days * 24 <= spanHours)
      ranges.push({ hours: days * 24, label: `${days}d` });
  }
  // Years are rolling 365-day windows, consistent with the graph's hour-based ranges.
  // Generate indefinitely as coverage grows: 1, 2, 5, 10, 20, 50, 100 ... years.
  for (let decade = 1; decade * 8760 <= spanHours; decade *= 10) {
    for (const multiple of [1, 2, 5]) {
      const years = multiple * decade;
      if (years * 8760 <= spanHours)
        ranges.push({ hours: years * 8760, label: `${years}y` });
    }
  }
  ranges.push({ hours: "all", label: "All" });
  return ranges;
}
function updateHistoryControls(history) {
  const ranges = availableHistoryRanges(history);
  if (!ranges.some((range) => range.hours === historyHours))
    historyHours = "all";
  const controls = document.querySelector(".history-controls");
  const signature = ranges.map((range) => range.hours).join(",");
  // Preserve focus on unchanged controls during background refreshes.
  if (controls.dataset.ranges !== signature) {
    controls.replaceChildren(
      ...ranges.map((range) => {
        const button = document.createElement("button");
        button.type = "button";
        button.dataset.hours = range.hours;
        button.textContent = range.label;
        return button;
      }),
    );
    controls.dataset.ranges = signature;
    controls.style.setProperty("--range-count", ranges.length);
    controls.style.setProperty(
      "--range-columns",
      Math.min(6, Math.ceil(ranges.length / 2)),
    );
  }
  for (const button of controls.querySelectorAll("button")) {
    const selected = button.dataset.hours === String(historyHours);
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
}
function uniformHistory(history, rangeHours, now = Date.now()) {
  const cutoff =
    rangeHours === "all" ? -Infinity : now - Number(rangeHours) * 3600000;
  const sensors = Object.values(history)
    .map((sensor) => ({
      ...sensor,
      points: sensor.points.filter(
        (point) => point.timestamp_ms >= cutoff && point.timestamp_ms <= now,
      ),
    }))
    .filter((sensor) => sensor.points.length > 0);
  let first = now;
  let sourcePeriod = 0;
  let largestSeries = 0;
  for (const sensor of sensors) {
    largestSeries = Math.max(largestSeries, sensor.points.length);
    for (const point of sensor.points) {
      first = Math.min(first, point.timestamp_ms);
      sourcePeriod = Math.max(sourcePeriod, point.period_seconds || 0);
    }
  }
  // Keep small raw series intact, including a newly added sensor sampling
  // faster than ten minutes. Otherwise dozens of readings can become one dot.
  if (sourcePeriod === 0 && largestSeries <= 800) {
    return { sensors, periodSeconds: 0 };
  }
  const spanSeconds =
    rangeHours === "all" ? (now - first) / 1000 : Number(rangeHours) * 3600;
  // Use one common interval for every sensor and both measurements. Never
  // pretend daily source averages have hourly detail. Cap long views near
  // 800 points per sensor using whole multiples of existing bucket periods.
  const minimumPeriod = Math.max(sourcePeriod, spanSeconds / 800);
  const periods = [
    1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 10800, 21600, 43200, 86400,
    259200, 604800, 1209600, 2592000, 7776000, 31536000,
  ];
  const periodSeconds =
    periods.find((period) => period >= minimumPeriod) ||
    Math.ceil(minimumPeriod / 31536000) * 31536000;
  const intervalMs = periodSeconds * 1000;
  const result = sensors.map((sensor) => {
    const buckets = new Map();
    for (const point of sensor.points) {
      const key = Math.floor(point.timestamp_ms / intervalMs);
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = { latest: point, count: 0, temperature: 0, humidity: 0 };
        buckets.set(key, bucket);
      }
      // An hourly average represents more readings than a single raw
      // sample. Weight by those counts when combining summaries.
      const count = point.sample_count || 1;
      bucket.temperature += point.temperature * count;
      bucket.humidity += point.humidity * count;
      bucket.count += count;
      if (point.timestamp_ms > bucket.latest.timestamp_ms)
        bucket.latest = point;
    }
    return {
      ...sensor,
      points: [...buckets.values()]
        .map((bucket) => ({
          ...bucket.latest,
          temperature: bucket.temperature / bucket.count,
          humidity: bucket.humidity / bucket.count,
          sample_count: bucket.count,
          period_seconds: periodSeconds,
        }))
        .sort((a, b) => a.timestamp_ms - b.timestamp_ms),
    };
  });
  return { sensors: result, periodSeconds };
}
function historyResolutionLabel(seconds) {
  if (seconds === 0) return "Recorded readings";
  if (seconds < 60) return `${seconds}-second averages`;
  if (seconds < 3600) return `${seconds / 60}-minute averages`;
  if (seconds === 3600) return "Hourly averages";
  if (seconds < 86400) return `${seconds / 3600}-hour averages`;
  if (seconds === 86400) return "Daily averages";
  return `${seconds / 86400}-day averages`;
}
function renderHistory(history) {
  updateHistoryControls(history);
  const { sensors, periodSeconds } = uniformHistory(history, historyHours);
  for (const unit of document.querySelectorAll(".chart-unit")) {
    if (!unit.dataset.unit) unit.dataset.unit = unit.textContent.trim();
    unit.textContent = `${unit.dataset.unit} · ${historyResolutionLabel(periodSeconds)}`;
  }
  function updateChart(chart, canvasId, type) {
    const datasets = sensors.map((sensor) => makeDataset(sensor, type));
    if (!chart) return createChart(canvasId, type, datasets);
    const visibility = new Map(
      chart.data.datasets.map((dataset, index) => [
        dataset.label,
        chart.isDatasetVisible(index),
      ]),
    );
    chart.data.datasets = datasets;
    chart.options = chartOptions(type, datasets);
    datasets.forEach((dataset, index) => {
      if (visibility.has(dataset.label)) {
        chart.setDatasetVisibility(index, visibility.get(dataset.label));
      }
    });
    chart.$inspectionX = null;
    chart.$inspectionTime = null;
    chart.update("none");
    return chart;
  }
  temperatureChart = updateChart(
    temperatureChart,
    "temperature-chart",
    "temperature",
  );
  humidityChart = updateChart(humidityChart, "humidity-chart", "humidity");
}
async function updateHistory(force = false) {
  // Range switches use local data; only initial load and the timer fetch history.
  if (force && cachedHistory !== null) {
    renderHistory(cachedHistory);
    return;
  }
  if (historyRequest) return historyRequest;
  historyRequest = (async () => {
    try {
      const response = await fetch("/api/history", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const history = await response.json();
      cachedHistory = history;
      // Render the currently selected range, even if it changed during fetch.
      renderHistory(history);
    } catch (error) {
      console.error("History update failed:", error);
    }
  })();
  try {
    await historyRequest;
  } finally {
    historyRequest = null;
  }
}
