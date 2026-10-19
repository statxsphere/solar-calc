import os
import json
import requests
import numpy as np
import laspy
import rasterio
import geopandas as gpd
from shapely.geometry import box
from pyproj import Transformer
from scipy import ndimage
from rasterio.transform import from_origin
from concurrent.futures import ThreadPoolExecutor

# outer sunset, san francisco (lon/lat)
area_id = 'sunset'
bounds = [-122.5011, 37.7500, -122.4897, 37.7635]
buffer = 100  # extra metres of dsm around the area so edge buildings get shading
res = 0.5
crs = 'EPSG:32610'
max_depth = 9  # ept depth 9 is ~0.25 m point spacing

ept_url = 'https://s3-us-west-2.amazonaws.com/usgs-lidar-public/CA_SanFrancisco_1_B23/'
footprints_url = 'https://data.sf.gov/resource/ynuv-fyni.geojson'


def get_utm_box(bounds, buffer, res):
    to_utm = Transformer.from_crs('EPSG:4326', crs, always_xy=True)
    xs, ys = to_utm.transform([bounds[0], bounds[0], bounds[2], bounds[2]], [bounds[1], bounds[3], bounds[1], bounds[3]])
    x0 = np.floor((min(xs) - buffer) / res) * res
    y0 = np.floor((min(ys) - buffer) / res) * res
    x1 = np.ceil((max(xs) + buffer) / res) * res
    y1 = np.ceil((max(ys) + buffer) / res) * res
    return [x0, y0, x1, y1]


def get_ept_nodes(utm_box, max_depth):
    ept = requests.get(ept_url + 'ept.json').json()
    b = ept['bounds']

    to_merc = Transformer.from_crs(crs, 'EPSG:3857', always_xy=True)
    xs, ys = to_merc.transform([utm_box[0], utm_box[0], utm_box[2], utm_box[2]], [utm_box[1], utm_box[3], utm_box[1], utm_box[3]])
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)

    def overlaps(key):
        d, x, y, z = [int(i) for i in key.split('-')]
        w = (b[3] - b[0]) / 2**d
        return b[0] + x * w < x1 and b[0] + (x + 1) * w > x0 and b[1] + y * w < y1 and b[1] + (y + 1) * w > y0

    nodes = {}
    pages = ['0-0-0-0']
    while pages:
        page = requests.get(ept_url + 'ept-hierarchy/' + pages.pop() + '.json').json()
        for key, count in page.items():
            if int(key.split('-')[0]) > max_depth or not overlaps(key):
                continue
            if count == -1:
                pages.append(key)
            elif count > 0:
                nodes[key] = count

    print(len(nodes), 'ept nodes,', sum(nodes.values()), 'points')
    return nodes


def download_node(key):
    path = f'data/raw/ept/{key}.laz'
    if not os.path.exists(path):
        r = requests.get(ept_url + 'ept-data/' + key + '.laz', timeout=120)
        r.raise_for_status()
        with open(path + '.part', 'wb') as f:
            f.write(r.content)
        os.replace(path + '.part', path)
    return path


def download_points(nodes):
    os.makedirs('data/raw/ept', exist_ok=True)
    with ThreadPoolExecutor(16) as ex:
        files = list(ex.map(download_node, sorted(nodes)))
    return files


def make_dsm(files, utm_box, res):
    width = int(round((utm_box[2] - utm_box[0]) / res))
    height = int(round((utm_box[3] - utm_box[1]) / res))
    dsm = np.full(height * width, -9999, dtype='float32')
    to_utm = Transformer.from_crs('EPSG:3857', crs, always_xy=True)
    classes = {}

    for i, f in enumerate(files):
        las = laspy.read(f)
        cls = np.asarray(las.classification)
        for c, n in zip(*np.unique(cls, return_counts=True)):
            classes[int(c)] = classes.get(int(c), 0) + int(n)

        # drop noise points
        keep = ~np.isin(cls, [7, 18])
        x, y = to_utm.transform(np.asarray(las.x)[keep], np.asarray(las.y)[keep])
        z = np.asarray(las.z)[keep].astype('float32')

        col = np.floor((x - utm_box[0]) / res).astype(int)
        row = np.floor((utm_box[3] - y) / res).astype(int)
        ok = (col >= 0) & (col < width) & (row >= 0) & (row < height)
        np.maximum.at(dsm, row[ok] * width + col[ok], z[ok])

        if i % 200 == 0:
            print(i, 'of', len(files), 'files gridded')

    print('point classes:', classes)
    dsm = dsm.reshape(height, width)

    # fill empty cells with the nearest value
    empty = dsm == -9999
    print(round(empty.mean() * 100, 2), '% empty cells filled')
    idx = ndimage.distance_transform_edt(empty, return_distances=False, return_indices=True)
    dsm = dsm[idx[0], idx[1]]

    transform = from_origin(utm_box[0], utm_box[3], res, res)
    return dsm, transform


def save_dsm(dsm, transform, path):
    profile = {'driver': 'GTiff', 'dtype': 'float32', 'count': 1, 'height': dsm.shape[0], 'width': dsm.shape[1],
               'crs': crs, 'transform': transform, 'compress': 'deflate', 'tiled': True, 'predictor': 3}
    with rasterio.open(path, 'w', **profile) as dst:
        dst.write(dsm, 1)


def get_buildings(bounds, path):
    params = {'$where': f'within_box(shape, {bounds[3]}, {bounds[0]}, {bounds[1]}, {bounds[2]})', '$limit': 50000}
    r = requests.get(footprints_url, params=params, timeout=120)
    r.raise_for_status()
    gdf = gpd.GeoDataFrame.from_features(r.json()['features'], crs='EPSG:4326')

    # only keep buildings fully inside the area
    gdf = gdf[gdf.within(box(*bounds))]
    gdf = gdf[['sf16_bldgid', 'hgt_median_m', 'median_1st_m', 'geometry']].copy()
    gdf['hgt_median_m'] = gdf['hgt_median_m'].astype(float)
    gdf['median_1st_m'] = gdf['median_1st_m'].astype(float)
    gdf = gdf.reset_index(drop=True)
    gdf.to_file(path, driver='GeoJSON')
    print(len(gdf), 'buildings saved')
    return gdf


if __name__ == '__main__':
    os.makedirs(f'data/{area_id}', exist_ok=True)

    get_buildings(bounds, f'data/{area_id}/{area_id}.geojson')

    utm_box = get_utm_box(bounds, buffer, res)
    nodes = get_ept_nodes(utm_box, max_depth)
    files = download_points(nodes)
    dsm, transform = make_dsm(files, utm_box, res)
    save_dsm(dsm, transform, f'data/{area_id}/{area_id}.tif')

    with open(f'data/{area_id}/{area_id}.json', 'w') as f:
        json.dump({'area_id': area_id, 'bounds': bounds, 'crs': crs, 'res': res, 'dsm_box': utm_box}, f)
    print('done')
