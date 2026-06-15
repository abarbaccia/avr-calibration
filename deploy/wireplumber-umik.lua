-- WirePlumber rule: prevent UMIK from being auto-linked by WP's device-node policy.
--
-- Root-cause history (2026-06-12):
--   camilladsp_capture had autoconnect_to=Scarlett, so WP set target.object=Scarlett.
--   The Scarlett link failed (avr_cal_sweep already occupied input_3 from ExecStartPost).
--   WP fell back to findUndefinedTarget → used UMIK as the default audio source →
--   linked UMIK:FL/FR into camilladsp_capture:input_1/2 → mic feedback loop.
--
-- Fix: scarlett.yml now sets autoconnect_to=null for capture, which causes
-- CamillaDSP to create camilladsp_capture with node.autoconnect=false. WP's
-- handleLinkable() returns early and never touches the node. All camilladsp_capture
-- links are managed explicitly by audio-mode and camilladsp.service ExecStartPost.
--
-- This rule is now belt-and-suspenders only: if WP somehow tries to autolink the
-- UMIK device node anywhere, this prevents it.
--
-- resample.quality (2026-06-13):
--   The UMIK-1 (own USB clock) and the Scarlett/PipeWire graph (own clock) are
--   bridged by PipeWire's adaptive resampler. At the default resample.quality=4
--   (32-tap sinc) the clock-domain noise caps sub-cal coherence at ~0.72.
--   Raising it to 14 (128-tap sinc) takes coherence to 0.995-0.999 across
--   20-200 Hz (verified 2026-06-08). This property was lost when 51-umik.lua
--   was rewritten for the feedback-loop fix (f8b8dc1, 2026-06-12) — re-added
--   here so it survives deploys. It MUST coexist with node.autoconnect=false.

table.insert(alsa_monitor.rules, {
  matches = {
    {
      { "node.name", "matches", "alsa_input.usb-miniDSP_Umik*" },
    },
  },
  apply_properties = {
    ["node.autoconnect"] = false,
    ["resample.quality"] = 14,
  },
})
