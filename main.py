import os
import json
import requests
import numpy as np
import pandas as pd
import rasterio
import geopandas as gpd
from matplotlib.colors import LinearSegmentedColormap
from scipy import ndimage
from utils import (get_timezone, get_tmy, get_openmeteo_year, calculate_slope_aspect, rasterize_buildings,
                   calculate_horizon, get_solar_position, calculate_bin_irradiance, calculate_annual_irradiation,
                   building_stats, save_raster, save_png, save_web_buildings)

settings = {
    'tilt_step': 5,  # degrees
    'azimuth_step': 15,
    'directions': 16,  # horizon directions for shading
    'max_distance': 100,  # metres to look for shading
    'edge': 0.5,  # metres cut off the footprint edge
    'max_slope': 45,
    'north_slope': 10,  # north facing pixels steeper than this are left out
    'min_share': 0.7,  # pixel needs 70% of what an open flat roof gets
    'min_area': 10,  # m2 of usable roof for a building to count
    'packing': 0.65,  # share of usable roof covered by panels
    'efficiency': 0.20,  # kWp per m2 of panel
    'performance_ratio': 0.80,
    'grid_kg_per_kwh': 0.1944,  # eGRID2023 CAMX, 428.5 lb/MWh
}

vmin, vmax = 1300, 1750  # colour range for kWh/m2/yr
ramp_light = ['#ffedd5', '#fed7aa', '#fdba74', '#fb923c', '#f97316', '#ea580c', '#c2410c', '#9a3412']
ramp_dark = ['#7c2d12', '#9a3412', '#c2410c', '#ea580c', '#f97316', '#fb923c', '#fdba74', '#fed7aa']


def get_pvgis_reference(lat, lon):
    url = 'https://re.jrc.ec.europa.eu/api/v5_2/PVcalc'
    out = {}
    for name, params in [('flat', {'angle': 0, 'aspect': 0}), ('optimal', {'optimalangles': 1})]:
        params.update({'lat': lat, 'lon': lon, 'peakpower': 1, 'loss': 14, 'raddatabase': 'PVGIS-NSRDB', 'outputformat': 'json'})
        d = requests.get(url, params=params, timeout=120).json()
        totals = d['outputs']['totals']['fixed']
        mount = d['inputs']['mounting_system']['fixed']
        out[name] = {'kwh_kwp': totals['E_y'], 'kwh_m2': totals['H(i)_y'], 'tilt': mount['slope']['value'], 'azimuth': mount['azimuth']['value']}
    return out


def get_sunroof_reference(zip_code, cache_path):
    if not os.path.exists(cache_path):
        r = requests.get('https://storage.googleapis.com/project-sunroof/csv/latest/project-sunroof-postal_code.csv', timeout=300)
        with open(cache_path, 'wb') as f:
            f.write(r.content)
    df = pd.read_csv(cache_path, dtype={'region_name': str})
    row = df[df['region_name'] == zip_code].iloc[0]
    return {
        'zip': zip_code,
        'percent_qualified': round(row['percent_qualified'], 1),
        'count_qualified': int(row['count_qualified']),
        'kw_median': row['kw_median'],
        'kwh_per_kw': round(row['yearly_sunlight_kwh_total'] / row['kw_total']),
    }


def main(input_id, weather_source='pvgis', year=2023):
    dsm_path = f'data/{input_id}/{input_id}.tif'
    geojson_path = f'data/{input_id}/{input_id}.geojson'
    output_path = f'data/preds/{input_id}'
    web_path = 'docs/data'
    os.makedirs(output_path, exist_ok=True)
    os.makedirs(web_path, exist_ok=True)

    with rasterio.open(dsm_path) as src:
        elevation = src.read(1)
        profile = src.profile
        res = src.res[0]

    buildings = gpd.read_file(geojson_path).to_crs(profile['crs'])
    buildings['id'] = buildings['sf16_bldgid']

    centre = buildings.to_crs('EPSG:4326').union_all().centroid
    lat, lon = round(centre.y, 4), round(centre.x, 4)
    timezone = get_timezone(lat, lon)
    print(f'{len(buildings)} buildings around {lat}, {lon} ({timezone})')

    # weather for a typical year
    if weather_source == 'pvgis':
        weather, site_elevation = get_tmy(lat, lon, cache_path=f'data/raw/{input_id}_tmy.json')
    else:
        weather, site_elevation = get_openmeteo_year(lat, lon, year)
    weather = weather.tz_convert(timezone)

    # slope and aspect of every pixel, then the roof pixels
    elevation, slope, aspect = calculate_slope_aspect(elevation, res)
    building_id = rasterize_buildings(buildings, profile, settings['edge'])
    rows, cols = np.nonzero(building_id)
    print(len(rows), 'roof pixels')

    horizon = calculate_horizon(elevation, res, rows, cols, settings['directions'], settings['max_distance'])

    # irradiance for each tilt / azimuth bin
    solar_position, dni_extra, airmass = get_solar_position(weather, lat, lon, site_elevation, timezone)
    tilts = np.arange(settings['tilt_step'] / 2, 90, settings['tilt_step'])
    azimuths = np.arange(0, 360, settings['azimuth_step'])
    total, sky, blocked = calculate_bin_irradiance(weather, solar_position, dni_extra, airmass, tilts, azimuths, settings['directions'])

    flat_total, _, _ = calculate_bin_irradiance(weather, solar_position, dni_extra, airmass, [0], [180], settings['directions'])
    open_flat = float(flat_total[0, 0])
    settings['min_kwh_m2'] = round(settings['min_share'] * open_flat)
    print(f'Open flat roof {open_flat:.0f} kWh/m2, usable pixels need {settings["min_kwh_m2"]} kWh/m2')

    annual, unshaded = calculate_annual_irradiation(slope[rows, cols], aspect[rows, cols], horizon, tilts, azimuths,
                                                    total, sky, blocked, settings['tilt_step'], settings['azimuth_step'])
    print(f'Shading takes {100 * (1 - annual.sum() / unshaded.sum()):.1f}% off the roof pixels')

    # per building numbers
    results, usable = building_stats(buildings, building_id[rows, cols], annual, slope[rows, cols], aspect[rows, cols], res, settings)

    dsm_median = ndimage.median(elevation, labels=building_id, index=np.arange(1, len(buildings) + 1))
    results['dsm_median_m'] = dsm_median
    results.to_parquet(f'{output_path}/{input_id}_buildings.parquet')
    results.to_crs('EPSG:4326').to_file(f'{output_path}/{input_id}_buildings.geojson', driver='GeoJSON')

    # rasters
    kwh = np.full(elevation.shape, np.nan, dtype='float32')
    kwh[rows, cols] = annual
    save_raster(f'{output_path}/{input_id}_kwh_m2.tif', kwh, profile, cog=True)
    roof_only = np.where(slope <= 60, kwh, np.nan)
    for name, ramp in [('light', ramp_light), ('dark', ramp_dark)]:
        cmap = LinearSegmentedColormap.from_list(name, ramp)
        corners = save_png(f'{web_path}/irradiance_{name}.png', roof_only, profile, vmin, vmax, cmap)

    save_web_buildings(results, f'{web_path}/buildings.geojson')

    # headline numbers
    viable = results[results['viable']]
    reference = get_pvgis_reference(lat, lon)
    sunroof = get_sunroof_reference('94122', 'data/raw/sunroof_postal_code.csv')
    height_diff = (results['dsm_median_m'] - results['median_1st_m']).dropna()
    stats = {
        'area': input_id,
        'centre': [lon, lat],
        'timezone': timezone,
        'weather': weather_source,
        'buildings': len(results),
        'viable': len(viable),
        'viable_share': round(len(viable) / len(results), 3),
        'usable_m2': round(viable['usable_m2'].sum()),
        'kwp': round(viable['kwp'].sum()),
        'mwh_year': round(viable['kwh_year'].sum() / 1000),
        'co2_t_year': round(viable['co2_kg'].sum() / 1000),
        'median_kwh_m2': round(viable['kwh_m2'].median()),
        'median_kwp': round(viable['kwp'].median(), 1),
        'median_kwh_year': round(viable['kwh_year'].median()),
        'kwh_per_kwp': round(viable['kwh_year'].sum() / viable['kwp'].sum()),
        'annual_ghi': round(weather['ghi'].sum() / 1000),
        'open_flat_kwh_m2': round(open_flat),
        'shading_loss': round(1 - annual.sum() / unshaded.sum(), 3),
        'usable_pixel_share': round(float(usable.mean()), 3),
        'dsm_vs_2010_lidar_median_m': round(float(height_diff.median()), 2),
        'pvgis': reference,
        'sunroof': sunroof,
        'settings': settings,
        'colour_range': [vmin, vmax],
        'ramp_light': ramp_light,
        'ramp_dark': ramp_dark,
        'image_corners': corners,
    }
    with open(f'{web_path}/stats.json', 'w') as f:
        json.dump(stats, f, indent=1)
    print(json.dumps({k: v for k, v in stats.items() if k not in ['settings', 'image_corners']}, indent=1))
    return results


if __name__ == '__main__':
    main('sunset')
