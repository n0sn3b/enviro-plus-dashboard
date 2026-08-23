# Enviro + Air Quality Dashboard

Environmental monitoring dashboard for the **Pimoroni Enviro + Air Quality (PIM458)** Raspberry Pi board.

## Features

- **On-device display** — 4 screens on the 0.96" 160×80 LCD showing temp, humidity, pressure, light, and air quality
- **Light sensor tap** — cover/uncover the light sensor to switch screens
- **Double-tap** — toggle °C/°F
- **Web dashboard** — live readings + historical charts at `http://<pi-ip>:5000`
- **Data logging** — SQLite database, 1 reading per minute
- **NAS sync** — automatic SCP backup of the database
- **Calibration** — adjustable offsets for all sensors

<img width="2874" height="1330" alt="enviro1" src="https://github.com/user-attachments/assets/e8827ddb-3195-4008-9d5b-713c0981de1e" />
<img width="2872" height="1328" alt="enviro2" src="https://github.com/user-attachments/assets/d7025ecf-dde6-483c-a410-e616daa7b861" />
<img width="2842" height="1320" alt="enviro3" src="https://github.com/user-attachments/assets/ea68f046-c5e1-4a1b-a000-08c785fa0900" />

## Setup

### 1. Copy this project to your Pi


### 2. Enable I2C and SPI on the Pi

```bash
sudo raspi-config
#   -> Interface Options -> I2C (enable)
#   -> Interface Options -> SPI (enable)

# Or non-interactively:
# sudo raspi-config nonint do_i2c 0
# sudo raspi-config nonint do_spi 0
```

### 3. Create a virtual environment (inside the project folder)

On Debian-based systems (including Raspberry Pi OS), you first need the `python3-venv` package, otherwise the venv is created without `pip`:

```bash
sudo apt update && sudo apt install python3-venv
```

Then create and activate the venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

> If you already created a `.venv` before installing `python3-venv`, it has no `pip` — delete it and recreate it (`rm -rf .venv`).

### 4. Install the dependencies (in the venv)

```bash
pip install -r requirements.txt
```

This installs the Pimoroni sensor and display libraries (`pimoroni-bme280`, `ltr559`, `ads1015`, `st7735`) plus `flask`, `pillow`, and `gpiozero`.

### 5. Configure

Edit `settings.ini`:

```ini
[sensors]
unit = C                          ; C or F
temp_offset = 0.0                 ; calibration offset
humidity_offset = 0.0
pressure_offset = 0.0
light_offset = 0.0

[sync]
enabled = false                   ; set to true when NAS is ready
remote = pi@192.168.1.100:/backup/enviro.db
```

### 6. Run

```bash
cd /home/pi/enviro_dashboard
source .venv/bin/activate
python3 main.py
```

### 7. Access the web dashboard

Open `http://<pi-ip>:5000` in your browser.

### 8. Run as a systemd service (starts on boot)

```bash
sudo cp enviro.service.example /etc/systemd/system/enviro.service
sudo nano /etc/systemd/system/enviro.service   # set YOUR_USER and paths
sudo systemctl daemon-reload
sudo systemctl enable --now enviro.service
```

Logs: `journalctl -u enviro.service -f`

The service file is a template — copy `settings.example.ini` to `settings.ini` on first setup. Device-specific files (`settings.ini`, `gas_calibration.json`, `enviro.service`, the SQLite DB) are git-ignored so your calibration values stay private.

## Screen Controls

| Action | How |
|--------|------|
| Switch screen | Cover/uncover the light sensor (LTR-559) |
| Toggle °C/°F | Double-tap (two covers within 0.8s) |

Screens only change when you tap — the default screen is Temperature + Humidity.

## Calibration

1. Place an accurate reference thermometer/hygrometer next to the Enviro
2. Wait 10 minutes for readings to stabilize
3. Note the difference (e.g., Enviro reads 22.5°C but reference shows 21.0°C → offset = -1.5)
4. Edit `settings.ini` or use the web dashboard Settings page

## NAS Sync (optional)

For automatic backup to a NAS:

1. Set up SSH key authentication from the Pi to your NAS
2. Edit `settings.ini`:
   ```ini
   [sync]
   enabled = true
   remote = user@nas-ip:/path/to/backup/enviro.db
   ```
3. Restart the app

## Sensors

| Sensor | What it measures | Library |
|--------|-----------------|---------|
| BME280 | Temperature, Humidity, Pressure | pimoroni-bme280 |
| LTR-559 | Ambient light (lux) + Proximity | ltr559 |
| ADS1015 + MICS-6814 | NO₂, CO, NH₃ gas levels | ads1015 |
| LDR | Onboard light-dependent resistor | via ads1015 |

## Troubleshooting

**Display not working?** The ST7735 driver may need different pin config. Check `display.py` and adjust the GPIO pins to match your board.

**Gas sensor readings stuck at N/A?** The MICS-6814 needs ~10 seconds of heater warmup. Wait for the "Warming Up..." screen to complete.

**Import errors?** The Pimoroni libraries install as `pimoroni_bme280`, `ltr559`, `ads1015`, and `st7735`. If you see `ModuleNotFoundError`, run `pip install -r requirements.txt` inside the activated venv.

**Display shows `libopenblas.so.0: cannot open shared object file`?** The piwheels NumPy build needs the OpenBLAS runtime library, which isn't installed by default on Raspberry Pi OS:

```bash
sudo apt install libopenblas0
```

## File Structure

```
enviro_dashboard/
├── main.py              # Entry point — display loop + web server
├── config.py            # Settings loader
├── sensors.py           # Sensor drivers (BME280, LTR-559, MICS-6814)
├── display.py           # 160×80 LCD rendering
├── database.py          # SQLite storage + daily H/L
├── sync.py              # NAS sync via SCP
├── web/
│   ├── app.py           # Flask web app
│   ├── templates/       # HTML templates
│   └── static/          # CSS
├── settings.ini         # Configuration
├── requirements.txt     # Python dependencies
└── README.md            # This file
```
