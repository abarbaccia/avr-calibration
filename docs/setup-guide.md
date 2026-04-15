# Setup Guide

This guide is for Claude Code to read and use when helping a user set up avr-calibration. It's a conversation, not a script — ask questions, discover what they have, and adapt.

## Phase 1 — Understand their setup

Ask the user about their equipment. One topic at a time, conversational:

1. **AVR**: What receiver? (Denon/Marantz supported today.) Don't assume they know the IP — offer to help find it.
2. **DSP**: Do they have a miniDSP 2x4 HD? It's the only supported DSP today, but don't assume they have one. If they don't, explain what it does and why they need one for sub calibration.
3. **Subwoofers**: How many? Ported or sealed? Any bass shakers?
4. **Microphone**: UMIK-1 or UMIK-2? If they don't have one, they need one — it's the measurement mic.
5. **Compute**: Where will the service run? Options:
   - **Raspberry Pi** (recommended for always-on) — Pi 5 preferred, Pi 4 works
   - **Their laptop/desktop** — works fine, just needs Docker and USB access to the mic and miniDSP
   - **Any Linux box on the network** — NAS, NUC, whatever can run Docker with USB passthrough
   - The only hard requirements are: Docker, USB access to UMIK and miniDSP, network access to the Denon

## Phase 2 — Deploy the service

Based on where they're running it:

### Raspberry Pi (or any remote Linux host)

The install script handles everything — Docker, config template, systemd service, auto-updates:

```bash
bash <(curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh)
```

Run this ON the target machine (SSH in first if remote). It:
- Installs Docker if missing
- Pulls the `ghcr.io/abarbaccia/avr-calibration:latest` image (arm64 or amd64)
- Creates `~/.avr-calibration/config.yaml` template
- Sets up a systemd service that auto-starts on boot
- Sets up a daily auto-update timer

### Local laptop/desktop

```bash
# Pull the image
docker pull ghcr.io/abarbaccia/avr-calibration:latest

# Run with USB device access
docker run --rm \
    --name avr-calibration \
    --network=host \
    --privileged \
    -v ~/.avr-calibration:/data/.avr-calibration \
    ghcr.io/abarbaccia/avr-calibration:latest
```

`--privileged` is needed for USB HID access to the miniDSP (libusb/hidraw). `--network=host` is needed so the container can reach the Denon on the LAN and expose the MCP server.

They'll need to create `~/.avr-calibration/config.yaml` — help them write it (see Phase 3).

## Phase 3 — Configure hardware

Write `~/.avr-calibration/config.yaml` with their specific hardware. Key fields:

```yaml
denon:
  host: "192.168.1.xxx"       # Denon/Marantz IP on the LAN

minidsp:
  host: "localhost"            # minidspd runs inside the container
  port: 5380
  output_slots:
    - index: 0
      type: sub
      label: "Sub 1"
    - index: 1
      type: sub
      label: "Sub 2"
    # index 2, 3 — shaker, unused, etc.

mic:
  name: "UMIK"                 # substring matched against audio device names

measurement:
  playback_route: hdmi         # hdmi (through AVR) or usb (direct to miniDSP)
  denon_sweep_input: "AUX1"    # Denon input where the compute host's HDMI is connected
  denon_sweep_volume: -25.0    # master volume during sweeps
  sweep_channel: lfe           # lfe | fl | fr | c | sl | sr
```

**Finding the Denon IP**: Check their router's DHCP list, or use the `discover_avr` MCP tool after the service is running.

**Finding the Denon input name**: Input names depend on the AVR model and any renaming done in the Denon setup menu. The MCP tool `discover_avr` returns available inputs. If the wrong name is configured, the system returns an error listing the valid options.

## Phase 4 — Connect Claude Code

The user needs to add the MCP server to their Claude Code config. The service exposes an MCP endpoint.

If they're running from this repo directory, update `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "avr-calibration": {
      "url": "http://<service-ip>:8765/mcp"
    }
  }
}
```

If the service runs on localhost, the IP is `localhost`. If it's on a Pi or remote host, use that machine's IP.

For global access (from any directory), add it to `~/.claude/mcp.json` instead.

## Phase 5 — Verify everything works

Once the MCP server is configured, use the `check_system` tool to verify all hardware is reachable. This checks:
- Denon AVR connectivity
- miniDSP daemon and USB device
- UMIK microphone presence
- Config validity

Help them debug any failures. Common issues:
- **Denon not found**: wrong IP, AVR not on the network, or firewall blocking
- **miniDSP not detected**: USB cable issue, miniDSP not powered (needs 12V 1A minimum), or Docker doesn't have USB access
- **UMIK not found**: USB cable, wrong device name substring in config
- **MCP connection refused**: container not running, wrong IP/port in mcp.json

## Phase 6 — First measurement

Once check_system passes, suggest they take a first measurement to verify the full signal chain:

```
> take a measurement and show me what the room looks like
```

This exercises the entire path: sweep generation, Denon input switching, miniDSP routing, UMIK recording, and analysis. If this works, they're ready to calibrate.

## Important notes

- **Don't assume Pi.** The user might run this on their laptop, a NUC, a NAS, or anything with Docker and USB.
- **Don't assume existing hardware.** They might not have a miniDSP yet. That's okay — explain what they need and why.
- **Be conversational.** This is a setup conversation, not a checklist. Adapt to what they know and what they have.
- **Verify at each step.** Don't rush to the end — confirm each piece works before moving on.
- **The install script is a convenience, not a requirement.** Manual Docker setup is fine. The script just automates the Pi-specific systemd/auto-update setup.
