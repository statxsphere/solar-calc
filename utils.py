import os
import json
import requests
import numpy as np
import pandas as pd
import pvlib
import rasterio
import geopandas as gpd
from PIL import Image
from rasterio import features
from rasterio.warp import calculate_default_transform, reproject, Resampling
from scipy.ndimage import sobel, median_filter
from timezonefinder import TimezoneFinder


def get_timezone(lat, lon):
    tf = TimezoneFinder()
    return tf.timezone_at(lat=lat, lng=lon)


def get_tmy(lat, lon, database='PVGIS-NSRDB', cache_path=None):
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            data = json.load(f)
    else:
        url = 'https://re.jrc.ec.europa.eu/api/v5_2/tmy'
        params = {'lat': lat, 'lon': lon, 'raddatabase': database, 'usehorizon': 1, 'outputformat': 'json'}
        r = requests.get(url, params=params, timeout=120)
        r.raise_for_status()
        data = r.json()
        if cache_path:
            with open(cache_path, 'w') as f:
                json.dump(data, f)

    weather = pd.DataFrame(data['outputs']['tmy_hourly'])
    weather.index = pd.to_datetime(weather['time(UTC)'], format='%Y%m%d:%H%M', utc=True)
    weather = weather.rename(columns={'G(h)': 'ghi', 'Gb(n)': 'dni', 'Gd(h)': 'dhi', 'T2m': 'temp_air'})

    # irradiance is for the middle of the hour for some databases
    offset = data['inputs']['location'].get('irradiance_time_offset', 0)
    weather.index = weather.index + pd.Timedelta(hours=offset)

    print(f"TMY from {data['inputs']['meteo_data']['radiation_db']}, elevation {data['inputs']['location']['elevation']} m")
    print(f"Annual GHI {weather['ghi'].sum() / 1000:.0f} kWh/m2")
    return weather[['ghi', 'dni', 'dhi', 'temp_air']], data['inputs']['location']['elevation']


def get_openmeteo_year(lat, lon, year=2023):
    url = 'https://archive-api.open-meteo.com/v1/archive'
    params = {
        'latitude': lat,
        'longitude': lon,
        'hourly': 'shortwave_radiation,direct_normal_irradiance,diffuse_radiation,temperature_2m',
        'timezone': 'GMT',
        'start_date': f'{year}-01-01',
        'end_date': f'{year}-12-31'
    }
    data = requests.get(url, params=params, timeout=120).json()
    hourly = data['hourly']
    weather = pd.DataFrame({'ghi': hourly['shortwave_radiation'], 'dni': hourly['direct_normal_irradiance'],
                            'dhi': hourly['diffuse_radiation'], 'temp_air': hourly['temperature_2m']})
    # open-meteo radiation is the mean of the hour before the timestamp
    weather.index = pd.to_datetime(hourly['time'], utc=True) - pd.Timedelta(minutes=30)
    weather = weather.fillna(0)
    print(f"Open-Meteo {year}, annual GHI {weather['ghi'].sum() / 1000:.0f} kWh/m2")
    return weather, data['elevation']


def calculate_slope_aspect(elevation, res):
    # light smoothing so single noisy points dont make fake slopes
    elevation = median_filter(elevation, size=3)
    # sobel is 8x the gradient, so divide by 8 * pixel size
    dx = sobel(elevation, axis=1) / (8 * res)
    dy = sobel(elevation, axis=0) / (8 * res)
    slope = np.degrees(np.arctan(np.sqrt(dx**2 + dy**2)))
    # 0 = north, 90 = east, same as pvlib
    aspect = np.degrees(np.arctan2(-dx, dy)) % 360
    return elevation, slope, aspect


def rasterize_buildings(buildings, profile, edge=0.5):
    # shrink footprints a bit so wall pixels on the edge are left out
    shapes = buildings.geometry.buffer(-edge)
    shapes = [(geom, i + 1) for i, geom in enumerate(shapes) if not geom.is_empty]
    building_id = features.rasterize(shapes, out_shape=(profile['height'], profile['width']),
                                     transform=profile['transform'], fill=0, dtype='int32')
    return building_id


def shift(array, row, col):
    out = np.full(array.shape, -np.inf, dtype=array.dtype)
    h, w = array.shape
    out[max(0, -row):min(h, h - row), max(0, -col):min(w, w - col)] = \
        array[max(0, row):min(h, h + row), max(0, col):min(w, w + col)]
    return out


def calculate_horizon(elevation, res, rows, cols, directions=16, max_distance=100):
    # horizon angle in each direction, only kept for the roof pixels
    distances = list(np.arange(1, 10, 0.5))
    while distances[-1] < max_distance:
        distances.append(distances[-1] * 1.1)

    horizon = np.zeros((len(rows), directions), dtype='uint8')
    for k in range(directions):
        azimuth = np.radians(k * 360 / directions)
        best = np.zeros(elevation.shape, dtype='float32')
        steps = []
        for d in distances:
            step = (int(round(-d * np.cos(azimuth) / res)), int(round(d * np.sin(azimuth) / res)))
            if step not in steps:
                steps.append(step)
                dist = np.hypot(step[0], step[1]) * res
                best = np.maximum(best, (shift(elevation, step[0], step[1]) - elevation) / dist)
        horizon[:, k] = np.round(np.degrees(np.arctan(best[rows, cols])))
        print(f'horizon direction {k * 360 / directions:.1f} done')
    return horizon


def get_solar_position(weather, lat, lon, elevation, timezone):
    site_location = pvlib.location.Location(lat, lon, altitude=elevation, tz=timezone)
    solar_position = site_location.get_solarposition(weather.index)
    dni_extra = pvlib.irradiance.get_extra_radiation(weather.index)
    airmass = pvlib.atmosphere.get_relative_airmass(solar_position['apparent_zenith'])
    pressure = pvlib.atmosphere.alt2pres(elevation)
    airmass_absolute = pvlib.atmosphere.get_absolute_airmass(airmass, pressure)
    return solar_position, dni_extra, airmass_absolute


def calculate_bin_irradiance(weather, solar_position, dni_extra, airmass, tilts, azimuths, directions=16):
    sun_up = (solar_position['apparent_elevation'] > 0).values
    sun_elevation = np.clip(np.floor(solar_position['apparent_elevation'].values[sun_up]), 0, 90).astype(int)
    sun_sector = np.round(solar_position['azimuth'].values[sun_up] / (360 / directions)).astype(int) % directions

    total = np.zeros((len(tilts), len(azimuths)), dtype='float32')
    sky = np.zeros((len(tilts), len(azimuths)), dtype='float32')
    blocked = np.zeros((len(tilts), len(azimuths), directions, 92), dtype='float32')

    for i, tilt in enumerate(tilts):
        for j, azimuth in enumerate(azimuths):
            poa = pvlib.irradiance.get_total_irradiance(
                surface_tilt=tilt,
                surface_azimuth=azimuth,
                solar_zenith=solar_position['apparent_zenith'],
                solar_azimuth=solar_position['azimuth'],
                dni=weather['dni'],
                ghi=weather['ghi'],
                dhi=weather['dhi'],
                dni_extra=dni_extra,
                airmass=airmass,
                model='perez'
            ).fillna(0)
            total[i, j] = poa['poa_global'].sum() / 1000  # kWh/m2
            sky[i, j] = poa['poa_sky_diffuse'].sum() / 1000

            # direct energy lost if the horizon in that direction is above the sun
            direct = poa['poa_direct'].values[sun_up] / 1000
            table = np.zeros((directions, 92), dtype='float32')
            np.add.at(table, (sun_sector, sun_elevation + 1), direct)
            blocked[i, j] = np.cumsum(table, axis=1)

    return total, sky, blocked


def calculate_annual_irradiation(slope, aspect, horizon, tilts, azimuths, total, sky, blocked, tilt_step, azimuth_step):
    ti = np.clip((slope // tilt_step).astype(int), 0, len(tilts) - 1)
    ai = np.round(aspect / azimuth_step).astype(int) % len(azimuths)
    directions = horizon.shape[1]

    lost = np.zeros(len(slope), dtype='float32')
    for k in range(directions):
        lost += blocked[ti, ai, k, horizon[:, k]]

    # sky view factor from the horizon, compared to an open roof with the same tilt
    svf = np.mean(np.cos(np.radians(horizon)) ** 2, axis=1)
    open_svf = (1 + np.cos(np.radians(slope))) / 2
    diffuse_factor = np.clip(svf / open_svf, 0, 1)

    annual = total[ti, ai] - lost - sky[ti, ai] * (1 - diffuse_factor)
    return annual, total[ti, ai]


def building_stats(buildings, building_id, annual, slope, aspect, res, settings):
    pixel_area = res**2 / np.cos(np.radians(slope))  # sloped roof area of each pixel
    north = ((aspect > 315) | (aspect < 45)) & (slope > settings['north_slope'])
    usable = (slope <= settings['max_slope']) & ~north & (annual >= settings['min_kwh_m2'])

    n = len(buildings) + 1
    roof_area = np.bincount(building_id, weights=pixel_area, minlength=n)
    usable_area = np.bincount(building_id, weights=pixel_area * usable, minlength=n)
    usable_kwh = np.bincount(building_id, weights=pixel_area * annual * usable, minlength=n)
    roof_kwh = np.bincount(building_id, weights=pixel_area * annual, minlength=n)
    pixels = np.bincount(building_id, minlength=n)
    flat = np.bincount(building_id, weights=(slope < 10), minlength=n)

    out = buildings.copy()
    out['roof_m2'] = roof_area[1:]
    out['roof_kwh_m2'] = np.where(roof_area[1:] > 0, roof_kwh[1:] / np.maximum(roof_area[1:], 1e-9), np.nan)
    out['flat_share'] = np.where(pixels[1:] > 0, flat[1:] / np.maximum(pixels[1:], 1), np.nan)
    out['usable_m2'] = usable_area[1:]
    out['kwh_m2'] = np.where(usable_area[1:] > 0, usable_kwh[1:] / np.maximum(usable_area[1:], 1e-9), np.nan)

    viable = out['usable_m2'] >= settings['min_area']
    out['viable'] = viable
    panel_area = np.where(viable, out['usable_m2'] * settings['packing'], 0)
    out['kwp'] = panel_area * settings['efficiency']
    out['kwh_year'] = np.where(viable, usable_kwh[1:] * settings['packing'] * settings['efficiency'] * settings['performance_ratio'], 0)
    out['co2_kg'] = out['kwh_year'] * settings['grid_kg_per_kwh']
    return out, usable


def save_raster(path, data, profile, cog=False):
    profile = profile.copy()
    profile.update(dtype='float32', count=1, nodata=np.nan, compress='deflate')
    if cog:
        profile.update(driver='COG')
        profile.pop('tiled', None)
        profile.pop('predictor', None)
        profile.pop('blockxsize', None)
        profile.pop('blockysize', None)
    with rasterio.open(path, 'w', **profile) as dst:
        dst.write(data.astype('float32'), 1)


def save_png(path, data, profile, vmin, vmax, cmap, res=1.0):
    # reproject to lat/lon so the web map can lay it over the basemap
    src_transform = profile['transform']
    h, w = data.shape
    left, top = src_transform * (0, 0)
    right, bottom = src_transform * (w, h)
    transform, width, height = calculate_default_transform(profile['crs'], 'EPSG:4326', w, h, left, bottom, right, top,
                                                           resolution=res / 111320)
    out = np.full((height, width), np.nan, dtype='float32')
    reproject(data.astype('float32'), out, src_transform=src_transform, src_crs=profile['crs'], src_nodata=np.nan,
              dst_transform=transform, dst_crs='EPSG:4326', dst_nodata=np.nan, resampling=Resampling.average)

    colours = cmap(np.clip((out - vmin) / (vmax - vmin), 0, 1))
    colours[np.isnan(out), 3] = 0
    # 64 colours is plenty for the web and keeps the file small
    image = Image.fromarray((colours * 255).astype('uint8'), 'RGBA')
    image.quantize(64, method=Image.Quantize.FASTOCTREE).save(path, optimize=True)

    lon0, lat1 = transform * (0, 0)
    lon1, lat0 = transform * (width, height)
    return [[lon0, lat1], [lon1, lat1], [lon1, lat0], [lon0, lat0]]


def save_web_buildings(buildings, path):
    web = buildings.copy()
    web['geometry'] = web.geometry.simplify(0.3)
    web = web.to_crs('EPSG:4326')
    cols = ['id', 'roof_m2', 'usable_m2', 'kwh_m2', 'kwp', 'kwh_year', 'co2_kg', 'viable', 'geometry']
    web = web[cols]
    for c in ['roof_m2', 'usable_m2', 'kwh_year', 'co2_kg']:
        web[c] = web[c].round(0)
    web['kwh_m2'] = web['kwh_m2'].round(0)
    web['kwp'] = web['kwp'].round(1)
    web.to_file(path, driver='GeoJSON', COORDINATE_PRECISION=6, WRITE_BBOX='NO')
