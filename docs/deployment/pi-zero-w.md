# Deploying on Raspberry Pi Zero W

The Pi Zero W lives permanently in your rack. miniDSP is always connected. When
calibrating, you plug the UMIK into your laptop and open a browser — no software
install on the laptop required.

## Hardware

```
[Pi Zero W — rack, permanent]
  └── USB OTG hub
       └── miniDSP 2x4 HD (always connected)
  └── WiFi → Denon AVR on local network
  └── WiFi → your laptop (just a browser)

[Your laptop — when calibrating]
  └── USB → UMIK-1 microphone
  └── Browser → https://<pi-ip>:8000  (captures UMIK audio via Web Audio API)
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
  ✓  miniDSP       minidspd reachable at localhost:5380
  ✓  Denon AVR     Denon AVR-X3800H online at 192.168.x.x
```

> **Note:** UMIK mic is not checked here — it's on your laptop, accessed by the browser via Web Audio API.

## Access the web UI

From any device on your network:

```
https://avr-cal.local:8000
```

Or by IP: `https://<pi-ip>:8000`

> **Note:** The server uses a self-signed TLS certificate (generated on first boot).
> Your browser will show a security warning — click **Advanced → Proceed** to accept it.
> This is required for microphone access (`getUserMedia` only works over HTTPS).

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

### From main branch (stable)

```bash
sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest
sudo systemctl restart avr-calibration
```

### From a feature branch (testing / hotfix)

GitHub Actions builds and pushes an image for every branch push, tagged with
the branch name. To validate a branch on the Pi before merging to main:

```bash
# Replace <branch-name> with the branch (e.g. docker-pipeline)
sudo docker pull ghcr.io/abarbaccia/avr-calibration:<branch-name>
sudo docker stop avr-calibration
sudo docker run -d --name avr-calibration-test \
  --restart unless-stopped \
  -p 8000:8000 \
  -v ~/.avr-calibration:/root/.avr-calibration \
  ghcr.io/abarbaccia/avr-calibration:<branch-name>
```

To revert back to the stable image:

```bash
sudo docker stop avr-calibration-test && sudo docker rm avr-calibration-test
sudo systemctl start avr-calibration
```

### SSH hotfix (fastest — no rebuild)

For a one-file fix when you don't want to wait for the Docker build:

```bash
# From your dev machine — copy the changed file into the running container
sshpass -f ~/.ssh/pi_password ssh pi@avr-cal.local \
  "docker cp - avr-calibration:/app/calibrate/web.py" < calibrate/web.py

# Restart the container to pick up the change
ssh pi@avr-cal.local "sudo systemctl restart avr-calibration"
```

> **Note:** This change is ephemeral — it will be lost on the next `docker pull`.
> Always follow up with a proper image push once the fix is confirmed.

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
