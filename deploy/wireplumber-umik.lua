-- WirePlumber rule: disable auto-linking for UMIK microphones.
-- Installs to: /home/pi/.config/wireplumber/main.lua.d/51-umik.lua
--
-- Without this rule, WirePlumber's default link-new-nodes policy connects
-- the UMIK's two capture channels to the first available unconnected input
-- ports on camilladsp_capture — contaminating the sweep signal path with mic
-- audio (confirmed on live rig: UMIK:capture_FR → camilladsp_capture:input_2,
-- summed with avr_cal_sweep:monitor).
--
-- The UMIK is captured directly by the bare-metal measurement service via
-- PortAudio (sounddevice). It must NOT be linked to CamillaDSP.
-- node.autoconnect = false prevents WirePlumber from creating any automatic
-- links for this node.

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
