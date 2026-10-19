var map, stats, buildings, selected = null, mode = 'buildings';
var styles = { light: 'https://tiles.openfreemap.org/styles/positron', dark: 'https://tiles.openfreemap.org/styles/dark' };

function isDark() {
  var t = document.documentElement.getAttribute('data-theme');
  if (t) return t === 'dark';
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function fmt(n, d) {
  if (n === null || n === undefined || isNaN(n)) return '-';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: d || 0, minimumFractionDigits: d || 0 });
}

function ramp() {
  return isDark() ? stats.ramp_dark : stats.ramp_light;
}

function rampStops() {
  var r = ramp(), lo = stats.colour_range[0], hi = stats.colour_range[1], out = [];
  for (var i = 0; i < r.length; i++) out.push(lo + (hi - lo) * i / (r.length - 1), r[i]);
  return out;
}

function noneColour() {
  return isDark() ? '#4a4a4a' : '#d4d4d4';
}

function fillColour() {
  return ['case', ['==', ['get', 'viable'], true],
    ['interpolate', ['linear'], ['get', 'kwh_m2']].concat(rampStops()),
    noneColour()];
}

function drawLegend() {
  var r = ramp();
  document.getElementById('legend-bar').style.background = 'linear-gradient(90deg,' + r.join(',') + ')';
  var lo = stats.colour_range[0], hi = stats.colour_range[1], ticks = '';
  for (var i = 0; i < 4; i++) ticks += '<span>' + (i === 0 ? '&le;' : '') + fmt(lo + (hi - lo) * i / 3) + (i === 3 ? '+' : '') + '</span>';
  document.getElementById('legend-ticks').innerHTML = ticks;
}

function fillStats() {
  var s = {
    buildings: fmt(stats.buildings),
    viable: fmt(stats.viable),
    viable_pct: fmt(stats.viable_share * 100) + '%',
    mw: fmt(stats.kwp / 1000, 1),
    median_kwp: fmt(stats.median_kwp, 1),
    gwh: fmt(stats.mwh_year / 1000, 1),
    kwh_per_kwp: fmt(stats.kwh_per_kwp),
    co2: fmt(stats.co2_t_year),
    min_kwh_m2: fmt(stats.settings.min_kwh_m2),
    grid: fmt(stats.settings.grid_kg_per_kwh, 3)
  };
  document.querySelectorAll('[data-stat]').forEach(function (el) {
    var k = el.getAttribute('data-stat');
    if (s[k] !== undefined) el.textContent = s[k];
  });

  var p = stats.pvgis, sr = stats.sunroof;
  var rows = [
    ['Sunlight on an open flat roof', fmt(stats.open_flat_kwh_m2) + ' kWh/m&sup2;', fmt(p.flat.kwh_m2) + ' kWh/m&sup2;<small>PVGIS-NSRDB, same spot</small>'],
    ['Yield of a flat system', fmt(stats.open_flat_kwh_m2 * stats.settings.performance_ratio) + ' kWh/kWp', fmt(p.flat.kwh_kwp) + ' kWh/kWp<small>PVGIS, 14% system loss</small>'],
    ['Yield across all suitable roofs', fmt(stats.kwh_per_kwp) + ' kWh/kWp', fmt(sr.kwh_per_kw) + ' kWh/kW<small>Project Sunroof, ZIP ' + sr.zip + '</small>'],
    ['Buildings suitable for solar', fmt(stats.viable_share * 100) + '%', fmt(sr.percent_qualified) + '%<small>Project Sunroof, ZIP ' + sr.zip + '</small>'],
    ['Median system size', fmt(stats.median_kwp, 1) + ' kWp', fmt(sr.kw_median, 2) + ' kW<small>Project Sunroof, ZIP ' + sr.zip + '</small>'],
    ['Roof height vs 2010 LiDAR', fmt(stats.dsm_vs_2010_lidar_median_m, 2) + ' m', '0 m<small>DataSF footprint heights</small>']
  ];
  document.getElementById('checks-body').innerHTML = rows.map(function (r) {
    return '<tr><td>' + r[0] + '</td><td class="num" data-label="This model">' + r[1] + '</td><td class="num" data-label="Reference">' + r[2] + '</td></tr>';
  }).join('');
}

function fillTop() {
  var top = buildings.features.filter(function (f) { return f.properties.viable; })
    .sort(function (a, b) { return b.properties.kwh_year - a.properties.kwh_year; }).slice(0, 10);
  var body = document.getElementById('top-body');
  body.innerHTML = top.map(function (f) {
    var p = f.properties;
    return '<tr data-id="' + p.id + '"><td>' + p.id + '</td><td class="num">' + fmt(p.usable_m2) + '</td><td class="num">' + fmt(p.kwp, 1) + '</td><td class="num">' + fmt(p.kwh_year) + '</td></tr>';
  }).join('');
  body.querySelectorAll('tr').forEach(function (tr) {
    tr.addEventListener('click', function () {
      var f = buildings.features.find(function (x) { return x.properties.id === tr.getAttribute('data-id'); });
      select(f, true);
      document.getElementById('map').scrollIntoView({ behavior: 'smooth' });
    });
  });
}

function bbox(f) {
  var b = [180, 90, -180, -90];
  JSON.stringify(f.geometry.coordinates).replace(/\[(-?[\d.]+),(-?[\d.]+)\]/g, function (m, x, y) {
    x = +x; y = +y;
    b[0] = Math.min(b[0], x); b[1] = Math.min(b[1], y); b[2] = Math.max(b[2], x); b[3] = Math.max(b[3], y);
  });
  return b;
}

function select(f, fly) {
  var el = document.getElementById('detail');
  if (!f) {
    selected = null;
    map.setFilter('selected', ['==', ['get', 'id'], '']);
    el.innerHTML = '<p class="muted">No building selected.</p>';
    return;
  }
  var p = f.properties;
  selected = p.id;
  map.setFilter('selected', ['==', ['get', 'id'], p.id]);
  if (fly) map.fitBounds(bbox(f), { padding: 120, maxZoom: 19, duration: 800 });

  var badge = p.viable ? '<span class="badge">Suitable</span>' : '<span class="badge off">Not enough usable roof</span>';
  el.innerHTML = '<div class="detail-head"><b>Building ' + p.id + '</b>' + badge + '</div>' +
    '<div class="facts">' +
    fact('Roof area', fmt(p.roof_m2) + ' m&sup2;') +
    fact('Usable roof', fmt(p.usable_m2) + ' m&sup2;') +
    fact('Sunlight', p.kwh_m2 ? fmt(p.kwh_m2) + ' kWh/m&sup2;' : '-') +
    fact('System size', p.viable ? fmt(p.kwp, 1) + ' kWp' : '-') +
    fact('Generation', p.viable ? fmt(p.kwh_year) + ' kWh/yr' : '-') +
    fact('CO&#8322; avoided', p.viable ? fmt(p.co2_kg) + ' kg/yr' : '-') +
    '</div>';
}

function fact(label, value) {
  return '<div class="fact"><span>' + label + '</span><b>' + value + '</b></div>';
}

function setMode(m) {
  mode = m;
  document.querySelectorAll('.seg button').forEach(function (b) { b.classList.toggle('on', b.getAttribute('data-layer') === m); });
  if (!map || !map.getLayer('pixels')) return;
  map.setLayoutProperty('pixels', 'visibility', m === 'pixels' ? 'visible' : 'none');
  map.setPaintProperty('buildings-fill', 'fill-opacity', m === 'pixels' ? 0 : 0.88);
  map.setPaintProperty('buildings-line', 'line-opacity', m === 'pixels' ? 0.5 : 0.35);
}

function applyTheme() {
  map.setStyle(isDark() ? styles.dark : styles.light, { diff: false });
  drawLegend();
}

function addOverlay() {
  var dark = isDark();
  // put the roofs above the basemap buildings but under the street labels
  var layers = map.getStyle().layers, lastFill = 0, before;
  layers.forEach(function (l, i) { if (l.type === 'fill') lastFill = i; });
  before = (layers.slice(lastFill).find(function (l) { return l.type === 'symbol'; }) || {}).id;
  map.addSource('buildings', { type: 'geojson', data: buildings, promoteId: 'id' });
  map.addSource('pixels', { type: 'image', url: dark ? 'data/irradiance_dark.png' : 'data/irradiance_light.png', coordinates: stats.image_corners });
  map.addLayer({ id: 'pixels', type: 'raster', source: 'pixels', layout: { visibility: 'none' }, paint: { 'raster-resampling': 'nearest', 'raster-fade-duration': 0 } }, before);
  map.addLayer({ id: 'buildings-fill', type: 'fill', source: 'buildings', paint: { 'fill-color': fillColour(), 'fill-opacity': 0.88 } }, before);
  map.addLayer({ id: 'buildings-line', type: 'line', source: 'buildings', minzoom: 15.5, paint: { 'line-color': dark ? '#0a0a0a' : '#ffffff', 'line-width': 0.6, 'line-opacity': 0.35 } }, before);
  map.addLayer({ id: 'selected', type: 'line', source: 'buildings', filter: ['==', ['get', 'id'], selected || ''], paint: { 'line-color': dark ? '#fafafa' : '#0a0a0a', 'line-width': 2.5 } });
  setMode(mode);
}

function makeMap() {
  map = new maplibregl.Map({
    container: 'mapview',
    center: stats.centre,
    zoom: 15,
    maxZoom: 19.5,
    attributionControl: false,
    style: isDark() ? styles.dark : styles.light
  });
  map.on('style.load', addOverlay);
  map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

  map.on('load', function () {
    var b = buildings.features.map(bbox).reduce(function (a, c) {
      return [Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.max(a[2], c[2]), Math.max(a[3], c[3])];
    });
    map.fitBounds(b, { padding: 20, duration: 0 });
    document.documentElement.setAttribute('data-map', 'ready');
  });
  map.on('click', 'buildings-fill', function (e) {
    var id = e.features[0].properties.id;
    select(buildings.features.find(function (f) { return f.properties.id === id; }), false);
  });
  map.on('mouseenter', 'buildings-fill', function () { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', 'buildings-fill', function () { map.getCanvas().style.cursor = ''; });
}

document.getElementById('theme').addEventListener('click', function () {
  var next = isDark() ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  try { localStorage.setItem('theme', next); } catch (e) {}
  if (map) applyTheme();
});

window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () {
  if (map) applyTheme();
});

document.querySelectorAll('.seg button').forEach(function (b) {
  b.addEventListener('click', function () { setMode(b.getAttribute('data-layer')); });
});

Promise.all([
  fetch('data/stats.json').then(function (r) { return r.json(); }),
  fetch('data/buildings.geojson').then(function (r) { return r.json(); })
]).then(function (res) {
  stats = res[0];
  buildings = res[1];
  fillStats();
  drawLegend();
  fillTop();
  makeMap();
});
