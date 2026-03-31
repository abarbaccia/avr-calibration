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

- Raspberry Pi Zero 2 W
- **Micro-USB OTG adapter** (micro-USB male → USB-A female) — required to connect USB peripherals
  - ⚠️ A plain USB-A to micro-USB cable will **not** work. The Pi Zero's USB port is OTG;
    it needs the OTG adapter's ID pin to switch into host mode.
- miniDSP 2x4 HD (connected via the OTG adapter)
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

> **Note:** The Docker image is pre-built for `linux/arm/v7` via GitHub Actions CI.
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

## Deployment workflow

The recommended workflow for any code change:

```
1. SSH hotfix  →  validate on Pi (seconds, no rebuild)
2. git push    →  CI builds branch image (~30-60 min, arm/v7 QEMU)
3. Pi pulls branch image  →  re-validate the built container
4. PR merged to main  →  :latest image published
5. Pi pulls :latest  →  done
```

### Step 1 — SSH hotfix (immediate validation)

From your dev machine, run the hotfix script. It SCPs changed files to the Pi
and starts the container with those files bind-mounted over the installed
package — no Docker rebuild required.

```bash
# Auto-detects git-modified calibrate/ files:
./deploy/hotfix.sh

# Or target specific files:
./deploy/hotfix.sh calibrate/web.py

# Override Pi address:
PI_HOST=192.168.1.50 ./deploy/hotfix.sh
```

The script:
1. SCPs changed files to the Pi
2. Stops the systemd service
3. Starts the container with the hotfix files bind-mounted
4. Follows the logs live
5. On Ctrl+C, offers to restore the stable image

> **Note:** The hotfix is ephemeral — bind mounts override the installed package
> in the running container only. The underlying image is unchanged.

### Step 2 — CI build (confirm the real image works)

GitHub Actions builds and pushes an image for every branch push, tagged with
the branch name:

```bash
# After git push origin <branch>:
sudo docker pull ghcr.io/abarbaccia/avr-calibration:<branch-name>
sudo docker stop avr-calibration && sudo docker rm avr-calibration
sudo systemctl start avr-calibration  # starts with :latest — swap image first if needed
```

To test a specific branch image via systemd, temporarily edit the service:

```bash
sudo sed -i 's|:latest|:<branch-name>|' /etc/systemd/system/avr-calibration.service
sudo systemctl daemon-reload && sudo systemctl restart avr-calibration
# After validation, restore:
sudo sed -i 's|:<branch-name>|:latest|' /etc/systemd/system/avr-calibration.service
sudo systemctl daemon-reload
```

### Step 3 — Update to stable (after PR merged)

```bash
sudo docker pull ghcr.io/abarbaccia/avr-calibration:latest
sudo systemctl restart avr-calibration
```

## Troubleshooting

**miniDSP goes offline after ~20 minutes (DeviceNotReady / 502 errors):**

This is a kernel-level bug in the `dwc_otg` USB OTG driver used by the Pi Zero 2 W.
After sustained USB activity, the FIQ (Fast Interrupt Request) handler in the driver
corrupts an internal register offset, causing the USB bus to hang. Symptom in `dmesg`:

```
dwc_otg 3f980000.usb: Invalid offset (0xffffffff)
```

After this, minidspd reports `DeviceNotReady` on all requests and the web UI returns
502 errors for any miniDSP operation.

**Workaround:** Restart the service to re-initialize the USB connection:

```bash
sudo systemctl restart avr-calibration
```

The `dwc_otg.fiq_enable=0` kernel cmdline parameter is **not effective** on Pi Zero 2 W —
the Pi firmware ignores it at boot and forces FIQ on regardless. This is a known
limitation of the closed-source VideoCore firmware bundled with Raspberry Pi OS.

**Long-term fix:** If this becomes disruptive, add a cron job to watch for 502s and
auto-restart:

```bash
# /etc/cron.d/avr-watchdog — restart if miniDSP unreachable
* * * * * root curl -fsk https://localhost:8000/api/preflight/minidsp-combined | grep -q '"passed":true' || systemctl restart avr-calibration
```

**miniDSP not detected / `calibrate check` shows "miniDSP USB … not found":**

The most common cause on Pi Zero 2 W is using the wrong cable. You **must** use a
micro-USB OTG adapter (not a plain USB-A → micro-USB cable):

```
miniDSP  →  [USB-A cable]  →  [micro-USB OTG adapter]  →  Pi Zero 2 W USB port
```

Then check USB enumeration and udev rule:
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
