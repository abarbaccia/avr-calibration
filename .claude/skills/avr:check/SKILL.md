---
name: avr:check
version: 1.0.0
description: |
  Pre-flight system check. Verifies all hardware is connected and reachable.
allowed-tools:
  - Bash
  - Read
  - Write
  - Edit
  - Glob
  - Grep
  - Agent
  - AskUserQuestion
---

# /avr:check

Run a pre-flight system check to verify all hardware is connected and working.

## Arguments

None.

## Workflow

### Step 1 — Run check_system

Call the `check_system` MCP tool. This runs all pre-flight checks in parallel:
- **Config**: Valid config.yaml present
- **miniDSP**: USB device connected + minidspd daemon reachable
- **Denon AVR**: AVR reachable (with auto-discovery if host not configured)
- **Signal Path**: DSP source/preset matches config (if configured)

### Step 2 — Report results

For each check, report:
- Pass/fail status
- Detail (what was found)
- Error message and fix suggestion if failed

Format as a clear checklist.

### Step 3 — Troubleshoot failures

If any check failed, provide specific debugging guidance:

- **Config**: Suggest running `/avr:configure` to configure.
- **miniDSP USB**: Check USB cable, try different port, check `lsusb` output.
- **miniDSP daemon**: Check if minidspd is running (`systemctl status minidspd`).
- **Denon AVR**: Check power, network, try ping. Suggest `/avr:configure` to configure host.
- **Signal Path**: Explain the mismatch and what to do about it.

### Step 4 — Overall verdict

If all checks pass: "System is ready for calibration."
If some fail: "N of M checks failed. Fix the issues above before calibrating."

## Important rules

1. **Always run check_system first.** Don't try to manually check things.
2. **Be specific about fixes.** Don't just say "check the connection" — say which cable, which port.
3. **Suggest /avr:configure** if config is missing or incomplete.
