# Deploying on Raspberry Pi Zero W

The Pi Zero W lives permanently in your rack. miniDSP is always connected. When
calibrating, you plug the UMIK into your laptop and open a browser — no software
install on the laptop required.

## Hardware

```
[Pi Zero W — rack, permanent]
  └── USB OTG hub
       └── miniDSP 2x4 HD (always connected)
       └── UMIK-1 (plug in when calibrating)
  └── WiFi → Denon AVR on local network
  └── WiFi → your laptop (just a browser)
```

## Requirements

- Raspberry Pi Zero W
- Micro USB OTG hub (the Pi Zero W has one OTG USB port)
- miniDSP 2x4 HD
- UMIK-1 or UMIK-2 microphone (plugged into your **laptop**, not the Pi)
- Raspberry Pi OS **Bookworm Lite** (32-bit) — Python 3.11 required
- microSD card (8GB minimum)

## OS Setup

1. Flash **Raspberry Pi OS Bookworm Lite (32-bit)** using Raspberry Pi Imager
2. In Imager settings, configure:
   - Hostname: `avr-cal` (or your choice)
   - SSH: enabled
   - WiFi: your network SSID and password
   - Username: `pi`
3. Boot the Pi and confirm SSH access: `ssh pi@avr-cal.local`

## Installation

SSH into the Pi, then:

```bash
curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh | bash
```

Or clone and run locally:

```bash
git clone https://github.com/abarbaccia/avr-calibration
bash avr-calibration/deploy/install.sh
```

> **Note on numpy:** The Pi Zero W is ARMv6. numpy 1.26+ has no ARMv6 wheel,
> so the install script pins numpy to 1.24.x and compiles from source.
> This takes ~20 minutes on first install. Subsequent installs are cached.

## Configuration

After install, edit `~/.avr-calibration/config.yaml`:

```yaml
denon:
  host: "192.168.x.x"   # your Denon AVR IP address

minidsp:
  host: "localhost"
  port: 5380
```

Find your Denon IP on your router's device list, or check the AVR's
network settings menu.

## Verify hardware

```bash
uv run calibrate check
```

Expected output:
```
  ✓  Microphone    UMIK-1 detected
  ✓  miniDSP       minidspd reachable at localhost:5380
  ✓  Denon AVR     Denon AVR-X3800H online at 192.168.x.x
```

## Access the web UI

From any device on your network:

```
http://avr-cal.local:8000
```

Or by IP: `http://<pi-ip>:8000`

## Service management

```bash
# Status
sudo systemctl status avr-calibration

# Logs (live)
sudo journalctl -u avr-calibration -f

# Restart
sudo systemctl restart avr-calibration

# Stop
sudo systemctl stop avr-calibration
```

## Updates

```bash
cd ~/avr-calibration
git pull
uv sync --extra dev
sudo systemctl restart avr-calibration
```

## Troubleshooting

**miniDSP not detected:** Check USB connection and udev rule:
```bash
lsusb | grep -i minidsp
cat /etc/udev/rules.d/99-minidsp.rules
```

**minidspd not running:** Start it manually to see errors:
```bash
minidspd
```

**Web UI not reachable:** Check the service is running:
```bash
sudo systemctl status avr-calibration
sudo journalctl -u avr-calibration -n 50
```

**numpy compile fails:** Ensure you're on Python 3.11 (not 3.12):
```bash
python3 --version
```
