#!/usr/bin/env python3
"""
wrf_santiago.py

Descarga el pronóstico determinista WRF-SMN (4 km) desde el bucket público
s3://smn-ar-wrf, extrae la precipitación horaria para puntos de interés y
genera capas PNG alineadas a Web Mercator para las próximas 72 horas.

Salida (en --out, que es también la carpeta web donde vive la PWA):
    latest.json
    runs/AAAAMMDDHH/h001.png ... h072.png   (lluvia en la hora, mm/h)
    runs/AAAAMMDDHH/a001.png ... a072.png   (lluvia acumulada desde el inicio, mm)

Uso:
    python wrf_santiago.py --out /var/www/lluvia          # última corrida completa
    python wrf_santiago.py --out ./web --demo              # datos sintéticos, sin AWS
    python wrf_santiago.py --out /var/www/lluvia --force   # reprocesa aunque ya exista
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.spatial import cKDTree

# ----------------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------------
BUCKET = "smn-ar-wrf"
REGION = os.environ.get("SMN_AWS_REGION", "us-west-2")  # verificar si falla el listado
PREFIX = "DATA/WRF/DET"
# Archivos horarios. Si en el bucket el patrón es otro, ajustar aquí.
FILE_RE = re.compile(r"WRFDETAR_01H_(\d{8})_(\d{2})_(\d{3})\.nc$")
LEADS = list(range(1, 73))  # horas de pronóstico a procesar

POINTS = [
    {"id": "santiago", "name": "Santiago centro", "lat": -33.4489, "lon": -70.6693},
    # Agrega más puntos aquí; la app muestra un selector si hay más de uno.
    # {"id": "nunoa", "name": "Ñuñoa", "lat": -33.4569, "lon": -70.5975},
]

# Recorte del mapa: zona central, de la costa a la cordillera.
BBOX = {"south": -35.0, "north": -32.0, "west": -72.3, "east": -69.5}
IMG_WIDTH = 720       # ancho en píxeles de cada capa PNG
MAX_GAP_DEG = 0.06    # distancia máxima al punto de grilla más cercano
KEEP_RUNS = 4         # corridas que se conservan en disco

PRECIP_VARS = ("PP", "PREC", "PRECIP", "RAIN")
LAT_VARS = ("lat", "XLAT", "latitude", "LAT")
LON_VARS = ("lon", "XLONG", "longitude", "LON")

COLORS = ["#a9d6f5", "#5fa8e8", "#2e74c9", "#1f9e4a", "#83cc3c",
          "#f2df38", "#f59f1c", "#e65a1e", "#c21f2c", "#8a1a6b"]
PALETTES = {
    "hourly": {"units": "mm/h", "levels": [0.2, 0.5, 1, 2, 4, 6, 10, 15, 20, 30], "colors": COLORS},
    "accum":  {"units": "mm",   "levels": [1, 5, 10, 20, 30, 50, 75, 100, 150, 200], "colors": COLORS},
}


# ----------------------------------------------------------------------------
# Utilidades geográficas
# ----------------------------------------------------------------------------
def merc_y(lat_deg):
    return np.log(np.tan(np.pi / 4 + np.radians(lat_deg) / 2))


def inv_merc_y(y):
    return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


class Regridder:
    """Lleva la grilla curvilínea (Lambert) del WRF a una imagen regular en
    Web Mercator, usando vecino más cercano. Se calcula una sola vez."""

    def __init__(self, lat2d: np.ndarray, lon2d: np.ndarray, bbox: dict, width: int):
        pad = 0.2
        inside = ((lat2d >= bbox["south"] - pad) & (lat2d <= bbox["north"] + pad) &
                  (lon2d >= bbox["west"] - pad) & (lon2d <= bbox["east"] + pad))
        if not inside.any():
            raise SystemExit("El recorte BBOX no cae dentro del dominio del modelo.")
        rows = np.where(inside.any(axis=1))[0]
        cols = np.where(inside.any(axis=0))[0]
        self.rs = slice(rows[0], rows[-1] + 1)
        self.cs = slice(cols[0], cols[-1] + 1)
        lat_c, lon_c = lat2d[self.rs, self.cs], lon2d[self.rs, self.cs]

        k = math.cos(math.radians((bbox["north"] + bbox["south"]) / 2))
        tree = cKDTree(np.column_stack([lon_c.ravel() * k, lat_c.ravel()]))

        y_n, y_s = merc_y(bbox["north"]), merc_y(bbox["south"])
        span = math.radians(bbox["east"] - bbox["west"])
        height = max(1, round(width * (y_n - y_s) / span))
        xs = bbox["west"] + (np.arange(width) + 0.5) / width * (bbox["east"] - bbox["west"])
        ys = y_n - (np.arange(height) + 0.5) / height * (y_n - y_s)
        lon_g, lat_g = np.meshgrid(xs, inv_merc_y(ys))
        dist, idx = tree.query(np.column_stack([lon_g.ravel() * k, lat_g.ravel()]))
        self.idx = idx.reshape(lon_g.shape)
        self.valid = (dist < MAX_GAP_DEG).reshape(lon_g.shape)

    def apply(self, field: np.ndarray) -> np.ndarray:
        return field[self.rs, self.cs].ravel()[self.idx]


def nearest_index(lat2d, lon2d, lat, lon):
    k = math.cos(math.radians(lat))
    d2 = (lat2d - lat) ** 2 + ((lon2d - lon) * k) ** 2
    return np.unravel_index(np.argmin(d2), d2.shape)


def hex_to_rgb(h):
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def write_png(values: np.ndarray, valid: np.ndarray, palette: dict, path: Path):
    levels = np.asarray(palette["levels"], dtype="f4")
    rgb = np.asarray([hex_to_rgb(c) for c in palette["colors"]], dtype=np.uint8)
    idx = np.digitize(values, levels) - 1
    mask = (idx >= 0) & valid
    rgba = np.zeros(values.shape + (4,), dtype=np.uint8)
    rgba[mask, :3] = rgb[idx[mask]]
    rgba[mask, 3] = 210
    Image.fromarray(rgba, "RGBA").save(path, optimize=True)


# ----------------------------------------------------------------------------
# Lectura de datos
# ----------------------------------------------------------------------------
def _pick(ds, names, what):
    for n in names:
        if n in ds.variables:
            return n
    raise SystemExit(f"No encontré la variable de {what}. Variables disponibles: {list(ds.variables)}")


def read_nc(path: Path):
    import xarray as xr
    with xr.open_dataset(path, engine="netcdf4", decode_times=False) as ds:
        pv = _pick(ds, PRECIP_VARS, "precipitación")
        pp = np.squeeze(ds[pv].values).astype("f4")
        lat = np.squeeze(ds[_pick(ds, LAT_VARS, "latitud")].values).astype("f8")
        lon = np.squeeze(ds[_pick(ds, LON_VARS, "longitud")].values).astype("f8")
    if lat.ndim == 1 and lon.ndim == 1:
        lon, lat = np.meshgrid(lon, lat)
    pp = np.clip(np.nan_to_num(pp, nan=0.0), 0, None)
    return pp, lat, lon


def s3_client():
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    return boto3.client("s3", region_name=REGION, config=Config(signature_version=UNSIGNED))


def run_keys(s3, run: dt.datetime) -> dict[int, str]:
    prefix = f"{PREFIX}/{run:%Y/%m/%d/%H}/"
    keys: dict[int, str] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            m = FILE_RE.search(obj["Key"])
            if m:
                keys[int(m.group(3))] = obj["Key"]
    return keys


def find_latest_run(s3, now: dt.datetime):
    base = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 6) * 6)
    for i in range(12):  # hasta 3 días hacia atrás, en pasos de 6 h
        run = base - dt.timedelta(hours=6 * i)
        keys = run_keys(s3, run)
        if all(lead in keys for lead in LEADS):
            return run, keys
        if keys:
            print(f"Corrida {run:%Y-%m-%d %H}Z incompleta ({len(keys)} archivos), sigo buscando.")
    return None, None


def real_fields(run, keys, pp_mode):
    s3 = s3_client()
    prev_cum = None
    with tempfile.TemporaryDirectory() as tmp:
        for lead in LEADS:
            local = Path(tmp) / Path(keys[lead]).name
            s3.download_file(BUCKET, keys[lead], str(local))
            pp, lat, lon = read_nc(local)
            local.unlink()
            if pp_mode == "cumulative":
                hourly = pp if prev_cum is None else np.clip(pp - prev_cum, 0, None)
                prev_cum = pp
            else:
                hourly = pp
            print(f"  +{lead:03d} h listo")
            yield lead, hourly, lat, lon


def demo_fields(run):
    """Grilla curvilínea sintética y un sistema que cruza desde el oeste,
    con refuerzo orográfico hacia la cordillera."""
    ny, nx = 160, 120
    j, i = np.mgrid[0:ny, 0:nx]
    rot = math.radians(8)
    x_km, y_km = (i - nx / 2) * 4.0, (j - ny / 2) * 4.0
    xr_ = x_km * math.cos(rot) - y_km * math.sin(rot)
    yr_ = x_km * math.sin(rot) + y_km * math.cos(rot)
    lat = -33.5 + yr_ / 111.0
    lon = -70.9 + xr_ / (111.0 * math.cos(math.radians(33.5)))
    rng = np.random.default_rng(int(run.timestamp()) // 3600)
    texture = rng.random((ny, nx))
    oro = 1 + 2.5 * np.clip((lon + 70.6) / 0.8, 0, 1)
    for lead in LEADS:
        t = lead / 72
        cx, cy = -73.0 + 5.0 * t, -33.2 - 1.0 * t
        core = np.exp(-(((lon - cx) / 0.9) ** 2 + ((lat - cy) / 1.4) ** 2))
        pulse = 0.5 + 0.5 * math.sin(lead / 5)
        hourly = 4.0 * core * oro * pulse * (0.7 + 0.6 * texture)
        hourly[hourly < 0.05] = 0
        yield lead, hourly.astype("f4"), lat, lon


# ----------------------------------------------------------------------------
# Proceso principal
# ----------------------------------------------------------------------------
def process(out: Path, run: dt.datetime, fields, model_name: str):
    run_id = f"{run:%Y%m%d%H}"
    run_dir = out / "runs" / run_id
    tmp_dir = out / "runs" / f".{run_id}.tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True)

    regrid = None
    point_idx = None
    accum_grid = None
    series = {p["id"]: [] for p in POINTS}
    times, files_h, files_a = [], [], []

    for lead, hourly, lat, lon in fields:
        if regrid is None:
            regrid = Regridder(lat, lon, BBOX, IMG_WIDTH)
            point_idx = {p["id"]: nearest_index(lat, lon, p["lat"], p["lon"]) for p in POINTS}
            accum_grid = np.zeros_like(hourly)
        accum_grid += hourly

        name_h, name_a = f"h{lead:03d}.png", f"a{lead:03d}.png"
        write_png(regrid.apply(hourly), regrid.valid, PALETTES["hourly"], tmp_dir / name_h)
        write_png(regrid.apply(accum_grid), regrid.valid, PALETTES["accum"], tmp_dir / name_a)
        files_h.append(f"runs/{run_id}/{name_h}")
        files_a.append(f"runs/{run_id}/{name_a}")
        times.append((run + dt.timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        for pid, ij in point_idx.items():
            series[pid].append(round(float(hourly[ij]), 1))

    shutil.rmtree(run_dir, ignore_errors=True)
    tmp_dir.rename(run_dir)

    data = {
        "model": model_name,
        "run": run.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bounds": [[BBOX["south"], BBOX["west"]], [BBOX["north"], BBOX["east"]]],
        "times": times,
        "points": [{**p, "hourly": series[p["id"]]} for p in POINTS],
        "layers": {
            "hourly": {**PALETTES["hourly"], "files": files_h},
            "accum": {**PALETTES["accum"], "files": files_a},
        },
    }
    tmp_json = out / ".latest.json.tmp"
    tmp_json.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_json, out / "latest.json")
    prune(out)
    print(f"Listo: corrida {run_id}, {len(times)} horas.")


def prune(out: Path):
    runs = sorted(p for p in (out / "runs").iterdir() if p.is_dir() and not p.name.startswith("."))
    for old in runs[:-KEEP_RUNS]:
        shutil.rmtree(old, ignore_errors=True)


def already_done(out: Path, run: dt.datetime) -> bool:
    try:
        data = json.loads((out / "latest.json").read_text(encoding="utf-8"))
        return data.get("run") == run.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OSError, ValueError):
        return False


def gh_output(updated: bool):
    """En GitHub Actions, avisa al flujo si hay algo nuevo que publicar."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"updated={'true' if updated else 'false'}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, type=Path, help="Carpeta web de salida")
    ap.add_argument("--demo", action="store_true", help="Usa datos sintéticos (no requiere AWS)")
    ap.add_argument("--force", action="store_true", help="Reprocesa aunque la corrida ya esté publicada")
    ap.add_argument("--pp-mode", choices=["hourly", "cumulative"], default="hourly",
                    help="Si la variable de precipitación viene por hora o acumulada desde el inicio")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if args.demo:
        run = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // 12) * 12)
        process(args.out, run, demo_fields(run), "Demo (datos sintéticos)")
        gh_output(True)
        return

    run, keys = find_latest_run(s3_client(), now)
    if run is None:
        print("No encontré corridas completas recientes en el bucket.")
        gh_output(False)
        return
    if already_done(args.out, run) and not args.force:
        print(f"La corrida {run:%Y-%m-%d %H}Z ya está publicada. Nada que hacer.")
        gh_output(False)
        return
    print(f"Procesando corrida {run:%Y-%m-%d %H}Z")
    process(args.out, run, real_fields(run, keys, args.pp_mode), "WRF-SMN 4 km")
    gh_output(True)


if __name__ == "__main__":
    sys.exit(main())
