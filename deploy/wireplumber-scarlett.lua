-- WirePlumber rules for the Scarlett 18i20 + CamillaDSP setup.
-- Installs to: /home/pi/.config/wireplumber/main.lua.d/50-scarlett.lua
--
-- 1. Scarlett card uses the "pro-audio" profile so all 20 channels are
--    exposed as a single multichannel sink + source (instead of the
--    fragmented Front/Surround/etc. profiles).
-- 2. Scarlett nodes are locked to 48 kHz / S32_LE / 256-frame periods
--    so the live signal path matches CamillaDSP's chunksize and there
--    are no resampler insertions in the bass path.
-- 3. session.suspend-timeout-seconds = 0 keeps the device hot — without
--    this, idle USB negotiation between PW and the Scarlett can stall
--    CamillaDSP startup for 1–2 s.
-- 4. PipeWire graph default quantum is pinned to 256 / 48 kHz so any
--    other PW client (measurement engine, pw-loopback for the
--    Scarlett-ch3 → snd-aloop bridge) shares the same scheduling grid
--    as CamillaDSP.

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "matches", "alsa_card.usb-Focusrite*" },
    },
  },
  apply_properties = {
    ["api.alsa.use-acp"] = true,
    ["device.profile"] = "pro-audio",
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
