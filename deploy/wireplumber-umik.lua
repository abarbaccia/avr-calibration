-- WirePlumber rule: prevent auto-linking of UMIK microphones into CamillaDSP.
-- Installs to: /home/pi/.config/wireplumber/main.lua.d/51-umik.lua
--
-- BACKGROUND — why the previous rule failed
-- ──────────────────────────────────────────
-- The previous version set  node.autoconnect = false  on the UMIK *device node*
-- via alsa_monitor.rules.  This is ineffective because WirePlumber's
-- link-new-nodes policy links *stream* nodes (media.class = Stream/Input/Audio),
-- not device nodes.  When pw-record, pw-jack, or any other PipeWire client opens
-- an audio stream, the session manager treats that stream as the autoconnect
-- target, not the hardware node. Setting autoconnect on the device node therefore
-- has no effect on session-manager-initiated port links.
--
-- The concrete failure mode:
--   alsa_input.usb-miniDSP_Umik-1…:capture_FL  →  camilladsp_capture:input_1
--   alsa_input.usb-miniDSP_Umik-1…:capture_FR  →  camilladsp_capture:input_2
-- Channel 0 of CamillaDSP's lfe_source mixer routes toward the subs, creating
-- a mic→DSP→subs→room→mic feedback loop.  Confirmed live 2026-06-12: flipping one
-- sub's polarity changed the captured level by 21 dB (loop phase shift), polarity
-- signs flapped randomly, and SPL drifted between identical sweeps.
--
-- THE CORRECT APPROACH (WirePlumber 0.4.x)
-- ─────────────────────────────────────────
-- Use stream.rules (a.k.a. monitor.rules on the session manager side) to match
-- any *stream* that targets the UMIK as its source and force
-- node.dont-reconnect = true + node.autoconnect = false on it.
-- Additionally, set node.autoconnect = false on the UMIK device node itself
-- (belt-and-suspenders: prevents any WP policy from auto-linking the device
-- ports directly even if the stream rule is missed).
--
-- Listening / karaoke mode implications
-- ──────────────────────────────────────
-- Both modes use CamillaDSP to process audio through the Scarlett 18i20.
-- The Scarlett capture stream (alsa_input.usb-Focusrite…) IS linked into
-- camilladsp_capture by WirePlumber for the AVR sub pre-out path.  We must NOT
-- apply a blanket autoconnect-disable to camilladsp_capture itself — that would
-- break the Scarlett→CamillaDSP path.
--
-- The UMIK's node name uniquely identifies it:
--   alsa_input.usb-miniDSP_Umik*
-- The rule is therefore scoped to UMIK-matching nodes only.  The Scarlett node
-- (alsa_input.usb-Focusrite…) is unaffected.
--
-- Manual pw-link calls (avr-cal-sweep-link.sh) are not governed by WirePlumber's
-- autoconnect policy and continue to work as before.

-- 1. Device-node rule (belt-and-suspenders):
--    Matches the UMIK hardware node and disables autoconnect.
table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "node.name", "matches", "alsa_input.usb-miniDSP_Umik*" },
    },
  },
  apply_properties = {
    ["node.autoconnect"] = false,
  },
})

-- 2. Stream rule: prevent WirePlumber from auto-linking any source stream whose
--    node name matches the UMIK into anything (including camilladsp_capture).
--    stream.rules fire on PipeWire Stream objects (media.class Stream/Input/Audio),
--    which is where the actual auto-link decision is made in WP 0.4.x.
if type(stream) ~= "nil" and type(stream.rules) == "table" then
  table.insert(stream.rules, {
    matches = {
      {
        { "node.name", "matches", "alsa_input.usb-miniDSP_Umik*" },
      },
    },
    apply_properties = {
      ["node.autoconnect"]    = false,
      ["node.dont-reconnect"] = true,
    },
  })
end
