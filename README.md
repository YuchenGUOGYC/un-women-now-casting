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
--picking-start-hour 8
--picking-end-hour 17
--rain-lookback-hours 12
--max-hourly-rain 0.1
--max-lookback-rain 0.5
--min-temperature 10
--max-temperature 30
--max-relative-humidity 90
--min-solar-radiation 20
--min-suitable-hours 3
```

The current picking suitability logic is a configurable first version. It checks rainfall, previous rain accumulation, temperature, relative humidity, and shortwave radiation during the picking window.

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
