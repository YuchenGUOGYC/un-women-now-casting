# UN Women Now-Casting

This repository downloads point-based weather forecasts, evaluates rainfall and tea-picking suitability, and can send WxPusher alerts after scheduled runs.

## Project Structure

```text
un-women-now-casting/
|-- common/        Shared logging, coordinate generation, and region annotation tools
|-- openmetero/    Open-Meteo single-point and batch download scripts
|-- caiyun/        Caiyun hourly and radar download scripts
|-- model/         Alert models for precipitation and tea-picking suitability
`-- wxpusher/      WxPusher notification client and config examples
```

## Environment

Create the conda environment from `environment.yml`:

```bash
conda env create -f environment.yml
conda activate model-unwomen
```

On the server, use the project Python executable directly, for example:

```bash
/home/ec2-user/rs_dataset/conda_envs/model-unwomen/bin/python
```

## Coordinate File

Most batch scripts require an Excel or CSV file with at least:

```text
latitude, longitude
```

To generate a regular grid:

```bash
python common/generate_lonlat_list.py --west 105.63 --south 28.03 --east 105.99 --north 28.41 --resolution 0.05 --output lonlat_list.xlsx
```

Region names are resolved from `study area/study_area_region_grid.geojson` by default. This GeoJSON grid takes priority over any existing `region_name` column in the coordinate file, so updating the GeoJSON updates precipitation and tea-picking region grouping consistently. If the GeoJSON file is missing, the code generates the same 8x8 fallback grid in memory, including `外围-南部`.

To pre-annotate regions for faster alert runs:

```bash
python common/annotate_lonlat_regions.py --input lonlat_list.xlsx --output lonlat_list_with_region.xlsx
```

## Open-Meteo

Single point:

```bash
python openmetero/openmetero.py --latitude 28.2 --longitude 105.8 --output output/openmeteo_point.xlsx
```

Batch run:

```bash
python openmetero/batch_run_openmetero.py --input lonlat_list.xlsx --output-dir output/openmeteo_batch --timezone Asia/Shanghai --max-workers 6
```

Default download window:

- The script checks the current `Asia/Shanghai` date.
- If today is Beijing time `2026-07-04`, it downloads `2026-07-03 00:00` through `2026-07-05 23:00`.
- This covers the previous 24 hours and the next 48 hours around the current Beijing date.

Default hourly variables include:

```text
precipitation_probability, precipitation, rain, showers, temperature_2m,
relative_humidity_2m, shortwave_radiation
```

## Caiyun

Hourly single point:

```bash
python caiyun/caiyun_hourly.py --token YOUR_TOKEN --longitude 105.8 --latitude 28.2 --hourlysteps 48
```

Hourly batch:

```bash
python caiyun/batch_run_caiyun_hourly.py --input lonlat_list.xlsx --token YOUR_TOKEN --output-dir output/caiyun_batch --max-workers 1
```

Caiyun hourly downloads a rolling forecast from the request time forward. The default is `48` hourly steps.

## Precipitation Alert

Run the precipitation alert directly:

```bash
python model/run_precipitation_alert.py \
  --caiyun-dir output/caiyun_batch \
  --openmeteo-dir output/openmeteo_batch \
  --coord-file lonlat_list_with_region.xlsx \
  --wxpusher-config wxpusher/wxpusher.config.json
```

Important time logic:

- `--date` selects the download folder. Default: current Beijing date.
- `--forecast-date-utc` selects the forecast UTC day to evaluate. Default: current UTC date.
- The model only evaluates `00:00-23:59` for that UTC forecast day, so Caiyun's future 48-hour window will not be fully counted into one alert.

Current precipitation rules:

- Caiyun uses `precipitation`.
- Open-Meteo uses `rain`.
- Values are rounded to 1 decimal place.
- Region-hour precipitation is averaged across Caiyun and Open-Meteo when both are available.
- Rain level uses accumulated regional rainfall: small rain `<10`, moderate `10-24.9`, heavy `25-49.9`, rainstorm `>=50` mm.

## Tea-Picking Suitability Alert

Run the picking-date alert directly:

```bash
python model/run_picking_date_alert.py \
  --caiyun-dir output/caiyun_batch \
  --openmeteo-dir output/openmeteo_batch \
  --coord-file lonlat_list_with_region.xlsx \
  --wxpusher-config wxpusher/wxpusher.config.json
```

By default, it evaluates the current Beijing date from `00:00` to `23:00`. For example, if the task starts at Beijing time `2026-07-01 01:30`, it evaluates `2026-07-01 00:00-23:00`.

To send a WxPusher message, add:

```bash
--send
```

Useful tuning parameters:

```bash
--rain-lookback-hours 12
--rain-threshold 0.1
--max-relative-humidity 85
--current-solar-threshold 80
--drying-solar-threshold 120
--min-drying-sun-hours 4
--min-drying-solar-energy 1.5
--area-suitable-fraction 0.5
--good-picking-hours 6
--partial-picking-hours 3
```

Current picking suitability follows the formal tea-picking algorithm in `model/tea_picking_analysis.py` and `model/采茶适宜日期计算说明.md`:

- Point-hour suitability requires daylight, no current rain, relative humidity at or below 85%, shortwave radiation at or above 80 W/m2, and enough post-rain drying.
- If rain occurred in the previous 12 hours, post-rain drying requires at least 4 effective drying-sun hours and at least 1.5 MJ/m2 accumulated shortwave energy after the last rain hour.
- A region-hour is suitable when at least 50% of its grid points are suitable.
- A region day is `Good picking day` with at least 6 suitable hours, `Partial picking day` with 3-5 suitable hours, and `Not suitable` below 3 suitable hours.
- Temperature is included in the alert statistics, but it is not used as a direct suitability filter in the formal algorithm.

## WxPusher

Copy the example config and fill in your real token and UIDs:

```bash
cp wxpusher/wxpusher.config.example.json wxpusher/wxpusher.config.json
```

Test notification:

```bash
python wxpusher/send_notification.py --title "Weather Alert" --summary "Rain detected"
```

## Cron

The download scripts can trigger alert scripts after a successful batch run with:

```bash
--run-alert-after-download
```

Example Caiyun batch with precipitation alert:

```bash
python caiyun/batch_run_caiyun_hourly.py \
  --input /home/ec2-user/rs_dataset/lonlat_list_with_region.xlsx \
  --token YOUR_TOKEN \
  --output-dir /home/ec2-user/rs_dataset/nowcasting_output/caiyun_batch \
  --max-workers 1 \
  --log-dir /home/ec2-user/rs_dataset/nowcasting_log \
  --run-alert-after-download \
  --alert-openmeteo-dir /home/ec2-user/rs_dataset/nowcasting_output/openmeteo_batch \
  --alert-wxpusher-config /home/ec2-user/rs_dataset/github/un-women-now-casting/wxpusher/wxpusher.config.json \
  --alert-state-file /home/ec2-user/rs_dataset/nowcasting_output/model/precipitation_alert_state.json
```

For Linux + Miniconda setup and cron examples, see [LINUX_MINICONDA_CRON.md](LINUX_MINICONDA_CRON.md).

## Logs

All executable scripts use dated log folders:

```text
logs/YYYY-MM-DD/
```

On the server, pass `--log-dir /home/ec2-user/rs_dataset/nowcasting_log` to keep logs in the shared runtime log directory.

## Observation-Masked Open-Meteo Pipeline

The new pipeline is independent of the existing download and alert entry points. It runs the existing Open-Meteo batch first, calculates radar hourly precipitation for the previous Beijing date and the current partial date, applies a binary observation mask, uploads both forecast products to separate S3 prefixes, and then runs the history-aware picking alert.

Time rules:

- A timezone-aware `observation_cutoff` is captured once in `Asia/Shanghai` after the Open-Meteo forecast is downloaded and uploaded, immediately before radar processing starts.
- Radar filename timestamps are UTC. `classify_hourly_radar_weather.py` converts them to Beijing hours.
- The observation window is `[previous-day 00:00, observation_cutoff)`. Radar CSV filenames are filtered at second precision, so a partial current hour uses only images strictly before the cutoff.
- Future hours and missing radar point-hours are not masked.
- The alert loads 24 hours of prior history for rain lookback, but reports only `--target-date`.
- If the Open-Meteo stage crosses Beijing midnight, the run fails and must be retried instead of mixing two date partitions.

Default mask policy is isolated in `nowcast/observation_mask.py::binary_precipitation_mask`: radar hourly precipitation greater than `--mask-threshold-mm` maps to 1; otherwise it maps to 0. The mask gates `precipitation`, `rain`, `showers`, and `precipitation_probability`. Original values and radar audit columns remain in every merged workbook.

EC2 command replacing the current Open-Meteo-plus-alert chain:

```bash
/home/ec2-user/rs_dataset/conda_envs/model-unwomen/bin/python \
  nowcast/run_observation_masked_alert.py \
  --input /home/ec2-user/rs_dataset/lonlat_list.xlsx \
  --openmeteo-output-dir /home/ec2-user/rs_dataset/nowcasting_output/openmeteo_forecast_runs \
  --merged-output-dir /home/ec2-user/rs_dataset/nowcasting_output/openmeteo_observation_masked_runs \
  --radar-work-dir /home/ec2-user/rs_dataset/nowcasting_output/radar_observation_runs \
  --radar-classifier-script /home/ec2-user/rs_dataset/github/download_radar/processing/classify_hourly_radar_weather.py \
  --caiyun-dir /home/ec2-user/rs_dataset/nowcasting_output/caiyun_batch \
  --timezone Asia/Shanghai \
  --max-workers 6 \
  --wxpusher-config /home/ec2-user/rs_dataset/github/un-women-now-casting/wxpusher/wxpusher.config.json \
  --send \
  --log-dir /home/ec2-user/rs_dataset/nowcasting_log \
  >> /home/ec2-user/rs_dataset/nowcasting_log/openmeteo_tea_picking_observed_cron.log 2>&1
```

The AWS CLI must be authenticated on EC2. Defaults are derived from the radar workflow:

- Radar input: `s3://china-data-team-bucket-public/China_radar/roi_pipeline/csv/date=YYYY-MM-DD/`
- Radar lookup: `s3://china-data-team-bucket-public/China_radar/roi_pipeline/lookup/radar_highres_to_lonlat_lookup.xlsx`
- Original forecast: `s3://china-data-team-bucket-public/China_radar/nowcasting/openmeteo_forecast/date=YYYY-MM-DD/run=RUN_ID/`
- Observation-masked forecast: `s3://china-data-team-bucket-public/China_radar/nowcasting/openmeteo_observation_masked/date=YYYY-MM-DD/run=RUN_ID/`

Each merged S3 run includes `run_manifest.json` with the exact timezone-aware cutoff, observation window, mask configuration, counts, and both S3 URIs. Use `--skip-s3-upload` only for local tests. The default is strict: a missing radar date or unmatched point aborts before alerting; `--allow-missing-current-radar` relaxes only the current-date download.
