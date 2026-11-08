#!/usr/bin/env node
// Exercise the dashboard's actual resolution code without server dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'charts.js'), 'utf8');
const functions = source.slice(source.indexOf('function uniformHistory('), source.indexOf('function renderHistory('));
const context = {};
vm.createContext(context);
vm.runInContext(functions, context);
const now = Date.UTC(2026, 10, 1, 12);
const day = 86400000;
const original = [];
// Match the server response: raw last day, hourly last 30 days, daily older.
for (let index = 0; index <= 90 * 144; index++) {
    const timestamp_ms = now - index * 600000;
    const age = (now - timestamp_ms) / day;
    if (age <= 1) original.push({timestamp_ms, temperature: 20, humidity: 50});
    else if (age <= 30 && index % 6 === 0) original.push({timestamp_ms, temperature: 20, humidity: 50, period_seconds: 3600, sample_count: 6});
    else if (age > 30 && index % 144 === 0) original.push({timestamp_ms, temperature: 20, humidity: 50, period_seconds: 86400, sample_count: 144});
}
const history = {1: {name: 'Red', points: original}, 2: {name: 'Green', points: original}};
const before = JSON.stringify(history);
const ninety = context.uniformHistory(history, 90 * 24, now);
assert.equal(ninety.periodSeconds, 86400);
for (const sensor of ninety.sensors) {
    assert(sensor.points.every(point => point.period_seconds === 86400));
    const count = (low, high) => sensor.points.filter(point => point.timestamp_ms >= now - high * day && point.timestamp_ms < now - low * day).length;
    assert.equal(count(5, 25), count(45, 65));
    assert(sensor.points.length <= 91);
}
assert.equal(context.uniformHistory(history, 7 * 24, now).periodSeconds, 3600);
assert.equal(context.uniformHistory(history, 30 * 24, now).periodSeconds, 3600);
assert.equal(context.uniformHistory(history, 12, now).periodSeconds, 0);
assert.equal(JSON.stringify(history), before, 'Cached source points must remain untouched');
// Sample counts prevent a raw sample and an hourly average getting equal weight.
const weighted = context.uniformHistory({1: {points: [
    {timestamp_ms: now - 600000, temperature: 10, humidity: 30, sample_count: 6, period_seconds: 3600},
    {timestamp_ms: now - 1200000, temperature: 24, humidity: 58}
]}}, 90 * 24, now);
assert.equal(weighted.sensors[0].points[0].temperature, 12);
assert.equal(weighted.sensors[0].points[0].humidity, 34);
assert.equal(weighted.sensors[0].points[0].sample_count, 7);
// Missing days stay missing; long histories stay bounded without filling gaps.
const long = {1: {points: Array.from({length: 3650}, (_, i) => ({timestamp_ms: now - i * day, temperature: 20, humidity: 50, period_seconds: 86400, sample_count: 144}))}};
const tenYears = context.uniformHistory(long, 'all', now);
assert.equal(tenYears.periodSeconds, 7 * 86400);
assert(tenYears.sensors[0].points.length <= 801);
assert.equal(context.uniformHistory({}, 'all', now).sensors.length, 0);
// A new sensor can collect many readings inside the former ten-minute bucket.
// Keep those readings so both graphs have a line to draw immediately.
const fastPoints = Array.from({length: 60}, (_, index) => ({
    timestamp_ms: now - (59 - index) * 6000,
    temperature: 18 + index / 10,
    humidity: 45 + index / 5,
}));
const sparse = {
    1: {name: 'salon', color: '#ff5a5f', points: original.filter(point => !point.period_seconds)},
    2: {name: 'Parents', color: '#35d07f', points: [fastPoints[0]]},
    3: {name: 'outside', color: '#5da9ff', points: fastPoints},
};
const sparseBefore = JSON.stringify(sparse);
vm.runInContext(source.slice(source.indexOf('function makeDataset('), source.indexOf('const desktopInspectorPlugin')), context);
for (const range of [1, 12, 24, 'all']) {
    const result = context.uniformHistory(sparse, range, now);
    assert.equal(result.periodSeconds, 0);
    assert.equal(context.historyResolutionLabel(result.periodSeconds), 'Recorded readings');
    const outside = result.sensors.find(sensor => sensor.name === 'outside');
    assert.equal(outside.points.length, 60, 'Sub-ten-minute readings must not collapse to one dot');
    for (const field of ['temperature', 'humidity']) {
        const dataset = context.makeDataset(outside, field);
        assert.equal(dataset.data.length, 60);
        assert.equal(dataset.data[0].y, fastPoints[0][field]);
        assert.equal(dataset.data[59].y, fastPoints[59][field]);
        assert.equal(dataset.borderColor, sparse[3].color);
    }
}
assert.equal(JSON.stringify(sparse), sparseBefore);
const few = context.makeDataset({...sparse[3], points: fastPoints.slice(0, 3)}, 'temperature');
assert(few.pointRadius > 0, 'Short curves need visible sample markers');
// Dense raw histories still stay bounded and use sub-minute buckets when appropriate.
const dense = {3: {...sparse[3], points: Array.from({length: 3601}, (_, index) => ({
    timestamp_ms: now - index * 1000, temperature: 20, humidity: 50,
}))}};
const reduced = context.uniformHistory(dense, 1, now);
assert.equal(reduced.periodSeconds, 5);
assert(reduced.sensors[0].points.length <= 721);
assert.equal(context.historyResolutionLabel(5), '5-second averages');
console.log('PASS: long-range density and weighted averages, immutable data, bounded dense histories, six-second sensor readings retained in both graphs, visible short curves');
