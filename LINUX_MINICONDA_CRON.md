# Linux Deployment With Miniconda

This project can run on Linux with a Miniconda-managed Python environment and `cron` scheduled tasks.

## 1. Install Miniconda

Download and install Miniconda:

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh
```

After installation, reload your shell:

```bash
source ~/.bashrc
```

## 2. Clone The Project

```bash
git clone <your-repo-url> model-unwomen
cd model-unwomen
```

## 3. Create The Conda Environment

Create the environment from [environment.yml](/C:/Users/YuchenGuo/WRI/github/model-unwomen/environment.yml:1):

```bash
conda env create -f environment.yml
conda activate model-unwomen
```

If you later update dependencies:

```bash
conda env update -f environment.yml --prune
```

## 4. Verify The Environment

Run one quick check:

```bash
python -m py_compile common/generate_lonlat_list.py openmetero/openmetero.py caiyun/caiyun_hourly.py wxpusher/send_notification.py
```

## 5. Prepare Runtime Files

If you use WxPusher, create a real config file:

```bash
cp wxpusher/wxpusher.config.example.json wxpusher/wxpusher.config.json
```

Then edit `wxpusher/wxpusher.config.json` with your real token and UID.

For Caiyun tasks, keep your token in the command line or put it in a wrapper shell script with restricted permissions.

## 6. Manual Run Examples

Generate coordinates:

```bash
python common/generate_lonlat_list.py --west 100 --south 30 --east 101 --north 31 --resolution 0.1 --output /data/lonlat_list.xlsx
```

Run Open-Meteo batch:

```bash
python openmetero/batch_run_openmetero.py --input /data/lonlat_list.xlsx --output-dir /data/openmeteo_batch --timezone Asia/Shanghai --max-workers 6
```

Run Caiyun batch:

```bash
python caiyun/batch_run_caiyun_hourly.py --input /data/lonlat_list.xlsx --token YOUR_CAIYUN_TOKEN --output-dir /data/caiyun_batch --max-workers 1
```

## 7. Logs

Application logs are written to:

```text
logs/YYYY-MM-DD/
```

Examples:

- `logs/2026-05-12/openmeteo_batch.log`
- `logs/2026-05-12/caiyun_batch.log`
- `logs/2026-05-12/wxpusher_send.log`

You can also redirect cron stdout/stderr to separate files if you want scheduler-level logs.

## 8. Cron Scheduled Tasks

Open your crontab:

```bash
crontab -e
```

### Option A: Use `conda run`

This is the most stable option for cron.

Run Open-Meteo every day at 06:10:

```cron
10 6 * * * cd /opt/model-unwomen && /home/your_user/miniconda3/bin/conda run -n model-unwomen python openmetero/batch_run_openmetero.py --input /data/lonlat_list.xlsx --output-dir /data/openmeteo_batch --timezone Asia/Shanghai --max-workers 6 >> /var/log/model-unwomen/openmeteo_cron.log 2>&1
```

Run Caiyun every day at 06:20:

```cron
20 6 * * * cd /opt/model-unwomen && /home/your_user/miniconda3/bin/conda run -n model-unwomen python caiyun/batch_run_caiyun_hourly.py --input /data/lonlat_list.xlsx --token YOUR_CAIYUN_TOKEN --output-dir /data/caiyun_batch --max-workers 1 >> /var/log/model-unwomen/caiyun_cron.log 2>&1
```

### Option B: Use A Wrapper Script

Create `run_openmeteo.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

cd /opt/model-unwomen
source /home/your_user/miniconda3/etc/profile.d/conda.sh
conda activate model-unwomen

python openmetero/batch_run_openmetero.py \
  --input /data/lonlat_list.xlsx \
  --output-dir /data/openmeteo_batch \
  --timezone Asia/Shanghai \
  --max-workers 6
```

Make it executable:

```bash
chmod +x run_openmeteo.sh
```

Then schedule it:

```cron
10 6 * * * /opt/model-unwomen/run_openmeteo.sh >> /var/log/model-unwomen/openmeteo_cron.log 2>&1
```

## 9. Recommended Linux Directory Setup

Example layout:

```text
/opt/model-unwomen/           project code
/data/lonlat_list.xlsx        input coordinate file
/data/openmeteo_batch/        Open-Meteo outputs
/data/caiyun_batch/           Caiyun outputs
/var/log/model-unwomen/       cron stdout/stderr logs
```

## 10. Operational Notes

- Prefer `conda run -n model-unwomen ...` for cron because cron does not automatically load your interactive shell environment.
- Keep API tokens out of shared shell history when possible.
- The project itself writes structured run logs under `logs/YYYY-MM-DD/`.
- Cron redirection like `>> /var/log/... 2>&1` is still useful for capturing scheduler-level failures.
- If you use YAML config for WxPusher, `PyYAML` is already included in the environment file.
