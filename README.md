# Project Structure

This repository is organized into three functional groups:

- `wxpusher/`: notification client, config examples, and tests
- `openmetero/`: Open-Meteo single-point and batch download scripts
- `caiyun/`: Caiyun hourly and radar download scripts
- `common/`: shared helpers such as logging and coordinate list generation

## Directory Layout

```text
model-unwomen/
├─ common/
│  ├─ __init__.py
│  ├─ generate_lonlat_list.py
│  └─ logging_utils.py
├─ wxpusher/
│  ├─ send_notification.py
│  ├─ wxpusher.config.example.json
│  ├─ wxpusher.config.example.yaml
│  ├─ wxpusher_notify/
│  └─ tests/
├─ openmetero/
│  ├─ openmetero.py
│  └─ batch_run_openmetero.py
└─ caiyun/
   ├─ caiyun_hourly.py
   ├─ batch_run_caiyun_hourly.py
   └─ caiyun_radar_forecastp.py
```

## WxPusher

Copy `wxpusher/wxpusher.config.example.json` to `wxpusher/wxpusher.config.json`, fill in your real token and UIDs, then run:

```bash
python wxpusher/send_notification.py --title "天气提醒" --summary "现在下雨"
```

## OpenMeteo / Openmetero

Single point:

```bash
python openmetero/openmetero.py --latitude 39.2072 --longitude 101.6656
```

Batch run:

```bash
python openmetero/batch_run_openmetero.py --input points.xlsx --output-dir output/openmetero
```

Generate a coordinate list:

```bash
python common/generate_lonlat_list.py --west 100 --south 30 --east 101 --north 31 --resolution 0.1
```

## Caiyun

Hourly single point:

```bash
python caiyun/caiyun_hourly.py --token YOUR_TOKEN --longitude 101.6656 --latitude 39.2072 --hourlysteps 48
```

Hourly batch:

```bash
python caiyun/batch_run_caiyun_hourly.py --input points.xlsx --token YOUR_TOKEN --output-dir output/caiyun
```

Radar forecast:

```bash
python caiyun/caiyun_radar_forecastp.py --token YOUR_TOKEN --province-id 11 --output-dir output/caiyun_radar
```

## Logs

Shared logging helpers live in `common/`.
When any of the executable scripts runs, it writes trace logs to:

```text
logs/YYYY-MM-DD/
```

The log file name matches the script type, for example `openmeteo.log`, `openmeteo_batch.log`, `caiyun_hourly.log`, or `wxpusher_send.log`.

## Linux Deployment

For Linux + Miniconda environment setup and cron scheduling examples, see [LINUX_MINICONDA_CRON.md](/C:/Users/YuchenGuo/WRI/github/model-unwomen/LINUX_MINICONDA_CRON.md:1).
