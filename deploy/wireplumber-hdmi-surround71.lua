-- WirePlumber rules for the Pi → Denon AVR HDMI output (vc4-hdmi, card1,
-- platform-107c701400.hdmi). Installs to:
--   /home/pi/.config/wireplumber/main.lua.d/53-hdmi-surround71.lua
--
-- Cal-mode mains calibration injects discrete 6/8-ch LPCM sweeps into the AVR
-- over this card, so the 8-ch hdmi-surround71 sink must exist AND stay live:
--
-- 1. Pin the HDMI card to the surround71 profile so the 8-ch sink is created.
--    A 6-ch sweep into a stereo sink is silently downmixed and the discrete
--    channel never reaches its speaker ("sweep not detected"). `audio-mode`
--    (cal) also pins this at runtime via pactl with a WP-restart fallback; this
--    rule is the boot-time best-effort hint.
-- 2. session.suspend-timeout-seconds = 0 + node.pause-on-idle = false keep the
--    HDMI sink open. Without this the node auto-suspends after ~5 s idle and the
--    WP suspend loop races pw-cat, which then HANGS (exit 124) on the suspended
--    node — every mains sweep failed "timed out — HDMI sink may be unplugged"
--    (2026-06-22). Same pattern as the Scarlett in 50-scarlett.lua.
--
-- ⚠ Lua syntax: every match value and property key MUST be a quoted string. An
--    unquoted card name (the dots parse as a number literal) is a compile error
--    that drops WirePlumber into a restart-rate-limited `failed` state and takes
--    down the entire audio graph (learned the hard way 2026-06-22).

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "device.name", "matches", "alsa_card.platform-107c701400.hdmi" },
    },
  },
  apply_properties = {
    ["device.profile"] = "output:hdmi-surround71",
  },
})

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "node.name", "matches", "alsa_output.platform-107c701400.hdmi*" },
    },
  },
  apply_properties = {
    ["node.pause-on-idle"] = false,
    ["session.suspend-timeout-seconds"] = 0,
  },
})
