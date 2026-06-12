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
