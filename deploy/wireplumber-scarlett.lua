-- WirePlumber rules for the Scarlett 18i20 + CamillaDSP setup.
-- Installs to: /home/pi/.config/wireplumber/main.lua.d/50-scarlett.lua
--
-- 1. Scarlett card is pinned to the ACP "multichannel" profile, which exposes
--    all 20 channels as a single sink + source named:
--      alsa_output.usb-Focusrite_..._-00.multichannel-output  (AUX0..AUX19)
--      alsa_input.usb-Focusrite_..._-00.multichannel-input    (AUX0..AUX19)
--    ⚠ These exact node names are load-bearing: camilladsp scarlett.yml
--    (autoconnect_to), audio-mode (input_3 wiring), and avr-calibration
--    config.yaml all reference them. Do NOT switch to "pro-audio" — that
--    renames the nodes to pro-output-0/pro-input-0 and silently breaks every
--    consumer (verified the hard way 2026-06-12: a WP restart re-applied a
--    stale pro-audio pin and took down the whole capture chain).
-- 2. Scarlett nodes are locked to 48 kHz / S32_LE / 256-frame periods
--    so the live signal path matches CamillaDSP's chunksize and there
--    are no resampler insertions in the bass path.
-- 3. session.suspend-timeout-seconds = 0 keeps the device hot — without
--    this, idle USB negotiation between PW and the Scarlett can stall
--    CamillaDSP startup for 1–2 s.
-- 4. PipeWire graph default quantum is pinned to 256 / 48 kHz so any
--    other PW client (measurement engine) shares the same scheduling
--    grid as CamillaDSP.

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "matches", "alsa_card.usb-Focusrite*" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = true,
    ["device.profile"] = "output:multichannel-output+input:multichannel-input",
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "node.name", "matches", "alsa_*usb-Focusrite*" },
    },
  },
  apply_properties = {
    ["audio.rate"] = 48000,
    ["audio.format"] = "S32_LE",
    ["api.alsa.headroom"] = 0,
    ["api.alsa.period-size"] = 256,
    ["session.suspend-timeout-seconds"] = 0,
  },
})

alsa_monitor.properties["default.clock.rate"] = 48000
alsa_monitor.properties["default.clock.quantum"] = 256
alsa_monitor.properties["default.clock.min-quantum"] = 256
alsa_monitor.properties["default.clock.max-quantum"] = 256
