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
- Raspberry Pi OS **Bookworm Lite** (32-bit)
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

The script:
1. Installs Docker (if not already present)
2. Installs `minidspd` (miniDSP USB daemon)
3. Sets up the udev rule for the miniDSP USB device
4. Creates `~/.avr-calibration/config.yaml` with defaults
5. Pulls the pre-built Docker image from GHCR (`ghcr.io/abarbaccia/avr-calibration:latest`)
6. Installs and starts the `avr-calibration` systemd service

> **Note:** The Docker image is pre-built for `linux/arm/v6` via GitHub Actions CI.
> No source compilation happens on the Pi — the install takes only a few minutes.

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
docker exec avr-calibration calibrate check
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
sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest
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

**Pull fails / image not found:** Check that Docker is running and the Pi has internet access:
```bash
sudo systemctl status docker
ping -c 1 ghcr.io
```
