# AVR State — 2026-05-21 post-LFE-fix

## What this is

Known-good AVR state after diagnosing and fixing the LFE-blockage issue (sub pre-out
not producing audio during real content playback, though bass-management still worked).

## Root cause

AssignBin byte 46 had degraded from 0x0C → 0x00.  This corrupted the Audyssey processing
state for the SW1 channel, causing the pushed FIR to actively block the LFE signal path.

## Fix applied

1. `push_avr_speaker_layout` with `STATE_20260510-205727.ady` — restored byte 46 to 0x0C.
2. `apply_avr_fir` (cache_key `restore-2026-05-21`) — pushed clean FIRs to all 10 channels:
   - FL / FR / C / SLA / SRA — recovery-2 mains corrective curves (modal cuts + HF lift)
   - SW1 — passthrough (center-tap delta, no EQ)
   - TFL / TFR / TRL / TRR — passthrough

## Verification

Post-push sweep (session 190) in DOLBY SURROUND mode confirmed:
- Coherence 1.0 at 31–80 Hz on SW1 channel
- Subs fire on real content in all sound modes

## Channels

10 detected: FL, C, FR, SLA, SRA, TFL, TFR, TRL, TRR, SW1

## ampAssignInfo

`000403020001000002000000080000000000000000000000000000000202000202020001020304060A08000000010C0000`

Byte 46 = 0x0C (12) — this is the correct value. 0x00 here = degraded/broken state.
