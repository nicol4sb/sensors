#!/usr/bin/env node
// Exercise the dashboard's actual time-axis callbacks without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'charts.js'), 'utf8');
const start = source.indexOf('function alignedTimeTicks(');
const end = source.indexOf('// Create chart', start);
const context = {window: {innerWidth: 1200}, historyHours: 24, isTouchDevice: () => false};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const hour = 3600000;
const day = 24 * hour;

function ticks(min, max, width = 1200) {
    context.window.innerWidth = width;
    const scale = {min: +min, max: +max, options: {ticks: {}}};
    context.alignedTimeTicks(scale);
    const values = Array.from(scale.ticks, tick => tick.value);
    assert.equal(new Set(values).size, values.length, 'Ticks must be unique');
    assert(values.every((value, index) => value >= +min && value <= +max
        && (!index || value > values[index - 1])), 'Ticks must increase within the axis limits');
    assert.equal(scale.options.ticks.maxTicksLimit, values.length);
    assert(values.length > 0 && values.length < 150, 'Tick density must stay bounded');
    return Array.from(scale.ticks);
}

const originalZone = process.env.TZ;
try {
    for (const zone of ['UTC', 'Europe/Zurich', 'Asia/Kolkata']) {
        process.env.TZ = zone;
        const end = new Date(2026, 10, 8, 13, 17);
        // Desktop day and mobile half-day views both label 09:00 and 12:00.
        for (const [hours, width] of [[24, 1200], [12, 390]]) {
            const result = ticks(+end - hours * hour, end, width);
            const nine = +new Date(2026, 10, 8, 9);
            const noon = +new Date(2026, 10, 8, 12);
            assert(result.some(tick => tick.value === nine && tick.major));
            assert(result.some(tick => tick.value === noon && tick.major));
            const between = result.filter(tick => tick.value > nine && tick.value < noon);
            assert.deepEqual(between.map(tick => new Date(tick.value).getHours()), [10, 11]);
            assert(result.every(tick => new Date(tick.value).getMinutes() === 0));
        }
        for (const width of [390, 1200]) {
            // Short ranges retain natural five/ten-minute graduations.
            const short = ticks(+end - hour, end, width);
            assert(short.every(tick => new Date(tick.value).getMinutes() % 5 === 0));
            // Day/week views use local midnight or six-hour boundaries.
            for (const days of [7, 30, 60]) {
                const result = ticks(+end - days * day, end, width);
                assert(result.every(tick => new Date(tick.value).getMinutes() === 0));
                assert(result.every(tick => new Date(tick.value).getHours() % 6 === 0));
            }
            // Month/year views use actual calendar boundaries, not fractional months.
            for (const days of [90, 365, 5 * 365]) {
                const result = ticks(+end - days * day, end, width);
                assert(result.every(tick => {
                    const date = new Date(tick.value);
                    return date.getDate() === 1 && date.getHours() === 0 && date.getMinutes() === 0;
                }));
            }
        }
    }
    process.env.TZ = 'Europe/Zurich';
    // Calendar ticks stay at local midnight across both daylight-saving changes.
    for (const [month, date] of [[2, 31], [9, 27]]) {
        const end = new Date(2026, month, date, 12);
        const result = ticks(+end - 7 * day, end, 390);
        assert(result.every(tick => new Date(tick.value).getHours() === 0));
        assert(result.some((tick, index) => index && tick.value - result[index - 1].value !== day));
    }
    for (const type of ['temperature', 'humidity']) {
        const options = context.chartOptions(type, []);
        assert.equal(options.scales.x.afterBuildTicks, context.alignedTimeTicks);
        assert.equal(options.scales.x.ticks.autoSkip, false);
    }
    console.log('PASS: hourly ticks between 09:00 and 12:00, mobile/desktop ranges, calendar boundaries, time zones and DST; both graphs use the corrected axis.');
} finally {
    if (originalZone === undefined) delete process.env.TZ;
    else process.env.TZ = originalZone;
}
