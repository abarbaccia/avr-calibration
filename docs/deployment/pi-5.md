# Deploying on Raspberry Pi 5

The Pi 5 is the recommended target. It has enough USB ports for miniDSP + UMIK-1 simultaneously, no dwc_otg USB bug, and enough CPU headroom for real-time sweep capture.

## Hardware

```
[Pi 5 — rack, permanent]
  ├── USB-A  →  miniDSP 2x4 HD (always connected)
  ├── USB-A  →  UMIK-1 microphone (always connected)
  └── HDMI0  →  Denon X3800H (sweep playback via HDMI LFE)
```

## OS Setup

1. Flash **Raspberry Pi OS Bookworm Lite (64-bit)** using Raspberry Pi Imager
2. In Imager settings, configure:
   - Hostname: `avr-cal` (or your choice)
   - SSH: enabled
   - WiFi: your network SSID and password
   - Username: `pi`
3. Boot and confirm SSH: `ssh pi@avr-cal.local`

## Installation

```bash
curl -sL https://raw.githubusercontent.com/abarbaccia/avr-calibration/main/deploy/install.sh | bash
```

## Pi 5 — HDMI Multichannel Audio Fix

> **You must apply this fix before calibration.** Without it, the HDMI device only
> exposes 2 channels, making it impossible to route a dedicated LFE signal to the
> Denon's sub pre-out.

### Background

On Pi 5 with the `vc4-kms-v3d` kernel driver, the HDMI audio device reports only
`FORMAT: IEC958_SUBFRAME_LE` with `CHANNELS: 2`. This happens because some AVRs
(including the Denon X3800H) advertise **no Short Audio Descriptors (SADs)** in their
HDMI EDID on certain inputs — the CEA extension block has no Audio Data Block and
`audio=0` in the flags byte.

Without SADs, the vc4-hdmi driver falls back to 2-channel stereo IEC958. No amount
of ALSA configuration or `plughw` wrapping will fix this — it is a driver-level
negotiation issue.

**Why it worked on Pi Zero W:** The Pi Zero W uses a different video driver stack
(`bcm2835-audio`) that presents HDMI audio as a standard PCM device and does not
depend on the EDID audio capabilities negotiated with the sink.

### Diagnosis

Check your HDMI EDID for missing audio SADs:

```bash
python3 << 'EOF'
data = open('/sys/class/drm/card1-HDMI-A-1/edid', 'rb').read()
cea = data[128:]
dtd = cea[2]
flags = cea[3]
print('audio flag:', bool(flags & 0x40))
offset = 4
while offset < dtd:
    hdr = cea[offset]; bt = (hdr >> 5) & 7; bl = hdr & 0x1f
    if bt == 1:
        print('Audio Data Block found — no fix needed')
        break
    offset += 1 + bl
else:
    print('NO Audio Data Block — apply the fix below')
EOF
```

Check the ALSA device format:

```bash
aplay --dump-hw-params -D hw:0,0 /dev/zero 2>&1 | grep -E 'FORMAT|CHANNELS'
```

If you see `FORMAT: IEC958_SUBFRAME_LE` and `CHANNELS: 2`, apply the fix.

### Fix — Patch the EDID at the GPU Firmware Level

The fix injects a proper 8-channel LPCM Audio Data Block into the EDID and tells
the Pi GPU firmware to use the patched EDID instead of reading it from the sink.
This happens at firmware level (before the kernel starts), so the vc4-hdmi driver
sees the correct capabilities from the start.

**Step 1 — Generate the patched EDID:**

```bash
python3 << 'PYEOF'
edid = bytearray(open('/sys/class/drm/card1-HDMI-A-1/edid', 'rb').read())
cea = edid[128:]

# 8ch LPCM SAD: all sample rates, 16/20/24-bit
audio_sad = bytes([(1 << 3) | 7, 0x7f, 0x07])
adb = bytes([(1 << 5) | 3]) + audio_sad           # type=1, len=3
speaker_alloc_data = bytes([0xff, 0x07, 0x00])    # FL/FR/LFE/FC/RL/RR/FLC/FRC

dtd_offset = cea[2]
new_cea_data = bytearray()
offset = 4
while offset < dtd_offset:
    hdr = cea[offset]; bt = (hdr >> 5) & 7; bl = hdr & 0x1f
    block = cea[offset:offset + 1 + bl]
    if bt == 2:                                   # after Video block
        new_cea_data += bytes(block)
        new_cea_data += adb
    elif bt == 4:                                 # update Speaker Alloc
        new_cea_data += bytes([(4 << 5) | 3]) + speaker_alloc_data
    else:
        new_cea_data += bytes(block)
    offset += 1 + bl

new_dtd = 4 + len(new_cea_data)
dtd_data = cea[dtd_offset:127]
new_cea = bytearray(128)
new_cea[0] = 0x02; new_cea[1] = 0x03; new_cea[2] = new_dtd
new_cea[3] = cea[3] | 0x40                       # set audio flag
new_cea[4:4 + len(new_cea_data)] = new_cea_data
dtd_len = min(len(dtd_data), 127 - new_dtd)
new_cea[new_dtd:new_dtd + dtd_len] = dtd_data[:dtd_len]
new_cea[127] = (256 - sum(new_cea[:127]) % 256) % 256

patched = bytearray(edid[:128]) + new_cea
patched[127] = (256 - sum(patched[:127]) % 256) % 256

open('/tmp/denon-patched.edid', 'wb').write(patched)
print('Written to /tmp/denon-patched.edid')
PYEOF
```

**Step 2 — Deploy the patched EDID:**

```bash
sudo cp /tmp/denon-patched.edid /boot/firmware/edid.dat
```

**Step 3 — Enable GPU firmware EDID override in `config.txt`:**

Add the following to `/boot/firmware/config.txt` inside the `[all]` section:

```ini
[all]
hdmi_edid_file:0=1
```

The `:0` suffix targets HDMI port 0 (the smaller HDMI connector on Pi 5).
Use `:1` for the second HDMI port.

**Step 4 — Reboot:**

```bash
sudo reboot
```

**Step 5 — Verify:**

```bash
aplay --dump-hw-params -D hw:0,0 /dev/zero 2>&1 | grep -E 'FORMAT|CHANNELS'
```

Expected output:
```
FORMAT:  IEC958_SUBFRAME_LE
CHANNELS: [2 8]
```

`CHANNELS: [2 8]` confirms multichannel is now available. Inside the container:

```bash
sudo docker exec avr-calibration python3 -c \
  "import sounddevice as sd; print(sd.query_devices(0)['max_output_channels'])"
# Expected: 8
```

### Why not `drm.edid_firmware`?

The standard Linux `drm.edid_firmware=HDMI-A-1:edid/denon.bin` kernel parameter
also works in principle, but requires the EDID file to be in the initramfs at
`lib/firmware/edid/` (not `usr/lib/firmware/` — the symlink is resolved on the
host but the kernel's early firmware loader sees the raw directory in the initramfs
cpio archive). Getting this right requires manually repacking the initramfs. The
GPU firmware approach (`hdmi_edid_file:0=1`) is simpler and works at a lower level,
before the kernel even starts.

### HDMI Channel Mapping (5.1 LPCM)

After applying the fix, the device exposes 6 channels. The CEA-861 5.1 channel
order used by ALSA/vc4-hdmi:

| ALSA channel | 0-indexed | Audio role |
|---|---|---|
| 1 | 0 | FL (Front Left) |
| 2 | 1 | FR (Front Right) |
| 3 | 2 | FC (Front Center) |
| 4 | 3 | LFE |
| 5 | 4 | RL (Rear Left) |
| 6 | 5 | RR (Rear Right) |

Set `output_channel: 4` in `config.yaml` to route the sweep to the LFE channel.
Verify with `tests/hdmi_channel_test.py` (see below).

### Verifying LFE Channel Routing

Use the channel test script with DSP outputs muted so only room speakers play:

```bash
# Mute sub outputs via MCP, then:
sudo docker cp tests/hdmi_channel_test.py avr-calibration:/tmp/
sudo docker exec avr-calibration python /tmp/hdmi_channel_test.py
```

Expected when LFE channel is correct:
- **DSP input meter shows signal** (Denon routing LFE to sub pre-out)
- **Mic reads noise floor** (sub is muted at DSP, no room speaker plays LFE)

If all channels show mic signal, check Denon speaker configuration — speakers set
to "Large" bypass bass management and play full-range through room speakers.

## Troubleshooting

**`CHANNELS: 2` after applying fix:**
- Confirm `hdmi_edid_file:0=1` is in the `[all]` section of `config.txt` (not inside `[cm4]` or `[cm5]`)
- Confirm `/boot/firmware/edid.dat` exists and is 256 bytes
- Check `dmesg | grep -i edid` for any firmware errors

**`drm.edid_firmware` errors in dmesg:**
These can be safely ignored if `hdmi_edid_file:0=1` is working. Remove
`drm.edid_firmware=...` from `cmdline.txt` to suppress the errors.

**HDMI audio not working at all / `FORMAT: IEC958_SUBFRAME_LE` not listed:**
The HDMI Jack may be off. Check:
```bash
amixer -c 0 contents | grep -A1 'HDMI Jack'
```
If `values=off`, the HDMI cable isn't connected or the Denon isn't powered.

**Sweep not audible / correlation too low:**
Verify `output_channel` in `config.yaml` matches the LFE channel (4 for standard
5.1). Check Denon input is set to the HDMI port the Pi is connected to.
