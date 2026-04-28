"""Hardware pre-flight checks — verify mic, DSP, and AVR reachability.

The DSP check is driver-agnostic: it goes through ``DSPDriver.get_state()``
so swapping ``dsp_driver: camilladsp`` in config.yaml automatically swaps
the check to target the CamillaDSP daemon instead of minidspd. Only the
user-facing display name and the error-recovery hints vary by driver.
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Optional

from .config import Config

HIDRAW_DEVICE: str = "/dev/hidraw0"
"""Expected HID device node for the miniDSP 2x4 HD."""


# Display names + daemon-start hints per DSP driver. Used by the preflight check
# to produce readable output regardless of which DSP is configured.
_DSP_DISPLAY_NAMES: dict[str, str] = {
    "minidsp": "miniDSP 2x4 HD",
    "camilladsp": "CamillaDSP",
}
_DSP_DAEMON_NAMES: dict[str, str] = {
    "minidsp": "minidspd",
    "camilladsp": "camilladsp",
}
_DSP_START_HINTS: dict[str, str] = {
    "minidsp": "Start the daemon: run 'minidspd' in a separate terminal",
    "camilladsp": (
        "Start the CamillaDSP daemon on the configured host:port "
        "(e.g. `camilladsp -p 1234 /path/to/config.yml` as a systemd unit)"
    ),
}


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    error: Optional[str] = None


class PreflightChecker:
    """
    Runs three hardware checks in parallel before the calibration loop starts.

        [mic check]  [minidspd check]  [denon check]
              \\             |               /
               ---- asyncio.gather ---------
                             |
                   list[CheckResult]

    sounddevice is imported lazily inside check_mic() so that the module can be
    imported and tested in environments without PortAudio (e.g. CI).
    """

    def __init__(self, config: Config) -> None:
        self.config = config

    async def run_all(self) -> list[CheckResult]:
        """Run all hardware checks concurrently. Never raises — errors become failed results.

        Equipment checks:
            [Config]  [Microphone]  [miniDSP (USB+daemon)]  [Denon AVR + Playback]  [Signal Path]
        """
        dsp_label = _DSP_DISPLAY_NAMES.get(
            self.config.dsp_driver_name, self.config.dsp_driver_name
        )
        checks = [
            ("Config", self.check_config()),
            ("Microphone", self.check_mic()),
            (dsp_label, self.check_minidsp_combined()),
            ("Denon AVR", self.check_denon_and_playback()),
            ("Signal Path", self.check_signal_path_sync()),
            ("Output routing", self.check_output_routing_safety()),
            ("Audio stack", self.check_audio_stack_clean()),
            ("DSP persisted state", self.check_dsp_persisted_state()),
            ("Capture path", self.check_capture_path_consistency()),
        ]
        names = [name for name, _ in checks]
        coros = [coro for _, coro in checks]
        raw = await asyncio.gather(*coros, return_exceptions=True)
        results = []
        for name, outcome in zip(names, raw):
            if isinstance(outcome, BaseException):
                results.append(CheckResult(
                    name=name,
                    passed=False,
                    detail="",
                    error=f"Unexpected error: {outcome}",
                ))
            else:
                results.append(outcome)
        return results

    async def check_hidraw(self) -> CheckResult:
        """Check that the miniDSP HID device node exists at HIDRAW_DEVICE.

        Absence means the Pi's USB host controller hasn't enumerated the device.
        Most common cause on Pi Zero 2 W: using a plain USB-A → micro-USB cable
        instead of a micro-USB OTG adapter (the OTG adapter flips the ID pin that
        switches the port from device mode to host mode).
        """
        if await asyncio.to_thread(os.path.exists, HIDRAW_DEVICE):
            return CheckResult(
                name="miniDSP USB",
                passed=True,
                detail=f"{HIDRAW_DEVICE} present",
            )
        return CheckResult(
            name="miniDSP USB",
            passed=False,
            detail=f"{HIDRAW_DEVICE} not found",
            error=(
                "miniDSP not detected on USB. "
                "Pi Zero 2 W requires a micro-USB OTG adapter — "
                "a plain USB-A to micro-USB cable will not work. "
                "Connect a micro-USB OTG adapter to the Pi's USB port, "
                "then plug the miniDSP into the adapter."
            ),
        )

    async def check_mic(self) -> CheckResult:
        """Check that the UMIK (or configured mic) is visible as an audio input device."""
        try:
            import sounddevice as sd  # lazy: only needs PortAudio at runtime
            devices = sd.query_devices()
            mic_name = self.config.mic.get("name", "UMIK")

            # Find first input device matching the configured name substring
            for idx, dev in enumerate(devices):
                if dev["max_input_channels"] > 0 and mic_name.lower() in dev["name"].lower():
                    return CheckResult(
                        name="Microphone",
                        passed=True,
                        detail=f'{dev["name"]} (device {idx}, {int(dev["default_samplerate"])}Hz)',
                    )

            # No match — show what inputs ARE available to help the user debug
            available_inputs = [d["name"] for d in devices if d["max_input_channels"] > 0]
            if available_inputs:
                shown = ", ".join(available_inputs[:3])
                ellipsis = "…" if len(available_inputs) > 3 else ""
                return CheckResult(
                    name="Microphone",
                    passed=False,
                    detail=f'No "{mic_name}" found. Available inputs: {shown}{ellipsis}',
                    error=f"Connect your {mic_name} microphone via USB and retry",
                )

            return CheckResult(
                name="Microphone",
                passed=False,
                detail="No audio input devices found",
                error="Connect your measurement microphone and retry",
            )
        except Exception as exc:
            return CheckResult(
                name="Microphone",
                passed=False,
                detail="",
                error=str(exc),
            )

    async def check_minidsp(self) -> CheckResult:
        """Check that the configured DSP daemon is reachable via DSPDriver.get_state().

        Driver-agnostic despite the legacy name: display name, daemon name,
        and start hint are all looked up from the configured driver
        (``dsp_driver: minidsp`` / ``dsp_driver: camilladsp`` / etc.).
        """
        from .drivers.base import DriverError
        from .drivers.registry import load_dsp_driver

        dsp_name = self.config.dsp_driver_name
        display = _DSP_DISPLAY_NAMES.get(dsp_name, dsp_name)
        daemon = _DSP_DAEMON_NAMES.get(dsp_name, dsp_name)
        start_hint = _DSP_START_HINTS.get(
            dsp_name, f"Start the {daemon} daemon and ensure it's reachable."
        )
        # Connection target — miniDSP reads from minidsp.*, CamillaDSP from camilladsp.*
        if dsp_name == "camilladsp":
            cam = self.config.camilladsp
            host = cam.get("host", "127.0.0.1")
            port = int(cam.get("port", 1234))
        else:
            host, port = self.config.minidsp_host_port

        driver = load_dsp_driver(self.config)

        try:
            await driver.get_state()

            # CamillaDSP-specific: check that the audio pipeline is Running, not
            # just that the websocket is up. An Inactive pipeline means no audio
            # is flowing (e.g. the loopback write side was never opened). This is
            # a warning — not a hard failure — because the sweep context primes
            # the loopback before each measurement.
            pipeline_state_str = ""
            if dsp_name == "camilladsp" and hasattr(driver, "pipeline_state"):
                state = await driver.pipeline_state()
                pipeline_state_str = state
                if state not in ("Running", "Unknown"):
                    return CheckResult(
                        name=display,
                        passed=True,  # warning, not hard failure
                        detail=(
                            f"{display} at {host}:{port} — pipeline state: {state} "
                            "(not Running; will be primed before measurement)"
                        ),
                    )

            detail = f"{display} at {host}:{port}"
            if pipeline_state_str and pipeline_state_str != "Unknown":
                detail += f" — pipeline: {pipeline_state_str}"
            return CheckResult(
                name=display,
                passed=True,
                detail=detail,
            )

        except DriverError as exc:
            # Driver normalises timeouts and connection errors to DriverError;
            # surface the original distinction via message text so the user gets
            # a pointed hint ("wait" vs "start the daemon") rather than generic
            # "daemon unreachable".
            if "timeout" in str(exc).lower():
                return CheckResult(
                    name=display,
                    passed=False,
                    detail=f"Timeout connecting to {daemon} at {host}:{port}",
                    error=f"{daemon} may be starting — wait a moment and retry",
                )
            return CheckResult(
                name=display,
                passed=False,
                detail=f"Cannot reach {daemon} at {host}:{port}",
                error=start_hint,
            )
        except Exception as exc:
            return CheckResult(
                name=display,
                passed=False,
                detail="",
                error=str(exc),
            )

    async def check_denon(self) -> CheckResult:
        """Check that the Denon AVR is online.

        If denon.host is not configured, performs SSDP discovery (10s timeout)
        to find a Denon AVR on the local network automatically.

        Uses DenonDriver for all hardware access (no raw denonavr imports).
        """
        from .drivers.denon import DenonDriver
        from .drivers.base import DriverError

        host = self.config.denon.get("host")
        auto_discovered = False

        if not host:
            try:
                discovered = await DenonDriver(None).discover()
                if not discovered:
                    return CheckResult(
                        name="Denon AVR",
                        passed=False,
                        detail="No Denon AVR found on network (SSDP scan)",
                        error=(
                            "No Denon AVR discovered. "
                            "Ensure it is powered on and connected to the same network, "
                            "or set denon.host in ~/.avr-calibration/config.yaml."
                        ),
                    )
                host = discovered[0]
                auto_discovered = True
            except Exception as exc:
                return CheckResult(
                    name="Denon AVR",
                    passed=False,
                    detail="",
                    error=f"SSDP discovery failed: {exc}",
                )

        try:
            driver = DenonDriver(host)
            state = await driver.get_state()
            model = state.get("model", "Denon AVR")
            suffix = " (auto-discovered)" if auto_discovered else ""
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=f"{model} online at {host}{suffix}",
            )
        except DriverError as exc:
            return CheckResult(
                name="Denon AVR",
                passed=False,
                detail=f"Cannot connect to Denon AVR at {host}",
                error=str(exc),
            )

    async def check_playback_route(self) -> CheckResult:
        """Check that the configured playback route is ready.

        USB route: verify the playback device (e.g. miniDSP) is visible as an audio output.
        HDMI route: verify Denon AVR is reachable (reuses check_denon result for detail).
        """
        route = self.config.measurement.get("playback_route", "usb")

        if route == "hdmi":
            # Delegate to check_denon() — HDMI playback requires a working Denon connection.
            # For USB route, check_playback_route() only queries sounddevice (no Denon call).
            denon_result = await self.check_denon()
            return CheckResult(
                name="Playback Route",
                passed=denon_result.passed,
                detail=f"HDMI via {denon_result.detail}" if denon_result.passed else denon_result.detail,
                error=denon_result.error,
            )

        # USB route: verify playback device is visible
        try:
            import sounddevice as sd  # lazy: only needs PortAudio at runtime
            devices = sd.query_devices()
            device_name = self.config.measurement.get("playback_device", "miniDSP")
            for idx, dev in enumerate(devices):
                if dev["max_output_channels"] > 0 and device_name.lower() in dev["name"].lower():
                    return CheckResult(
                        name="Playback Route",
                        passed=True,
                        detail=f'USB: {dev["name"]} (device {idx})',
                    )
            available_outputs = [d["name"] for d in devices if d["max_output_channels"] > 0]
            shown = ", ".join(available_outputs[:3])
            ellipsis = "…" if len(available_outputs) > 3 else ""
            return CheckResult(
                name="Playback Route",
                passed=False,
                detail=f'USB: no "{device_name}" found. Available outputs: {shown}{ellipsis}',
                error=f"Connect {device_name} via USB or set measurement.playback_device",
            )
        except Exception as exc:
            return CheckResult(
                name="Playback Route",
                passed=False,
                detail="",
                error=str(exc),
            )

    async def check_signal_path_sync(self) -> CheckResult:
        """Compare the configured signal path against the live device state.

        Skipped (passes) if signal_path is not configured in config.yaml.
        Warns if the live device source or preset differs from config.
        """
        from .drivers.base import DriverError
        from .drivers.registry import load_dsp_driver

        sp = self.config.minidsp.get("signal_path")
        if not sp:
            return CheckResult(
                name="Signal Path",
                passed=True,
                detail="not configured (skipped)",
            )

        cfg_source = sp.get("source")
        cfg_preset = sp.get("preset")
        if cfg_source is None and cfg_preset is None:
            return CheckResult(
                name="Signal Path",
                passed=True,
                detail="no source/preset defined (skipped)",
            )

        driver = load_dsp_driver(self.config)
        try:
            state = await driver.get_state()
        except DriverError as exc:
            return CheckResult(
                name="Signal Path",
                passed=False,
                detail="",
                error=f"Cannot read device state: {exc}",
            )
        except Exception as exc:
            return CheckResult(
                name="Signal Path",
                passed=False,
                detail="",
                error=f"Cannot reach miniDSP daemon: {exc}",
            )

        live_source = state.get("source")
        live_preset = state.get("preset")

        mismatches = []
        if cfg_source is not None and live_source != cfg_source:
            mismatches.append(f"source: device={live_source} config={cfg_source}")
        if cfg_preset is not None and live_preset != cfg_preset:
            mismatches.append(f"preset: device={live_preset} config={cfg_preset}")

        if mismatches:
            return CheckResult(
                name="Signal Path",
                passed=False,
                detail=f"mismatch — {', '.join(mismatches)}",
                error="Run 'calibrate signal-path apply' to sync device to config",
            )

        parts = []
        if cfg_source is not None:
            parts.append(f"source={live_source}")
        if cfg_preset is not None:
            parts.append(f"preset={live_preset}")
        return CheckResult(
            name="Signal Path",
            passed=True,
            detail=", ".join(parts) + " matches config",
        )

    async def check_minidsp_combined(self) -> CheckResult:
        """Combined DSP check: daemon reachability + driver-specific USB diagnostic.

        For ``dsp_driver: minidsp`` the hidraw check is a useful diagnostic
        when the daemon is unreachable — minidspd claims the miniDSP via
        libusb/usbfs (detaching hid-generic), so ``/dev/hidraw0`` only exists
        while the daemon is **down**; its presence with a dead daemon means
        "plugged in but daemon not running." For other DSP drivers (CamillaDSP
        talks over plain TCP to its own daemon), the USB hidraw diagnostic
        doesn't apply and the daemon check stands alone.
        """
        daemon_result = await self.check_minidsp()

        if daemon_result.passed:
            return daemon_result

        # CamillaDSP (and other non-miniDSP drivers): the daemon result is
        # authoritative on its own — no USB diagnostic to layer on top.
        if self.config.dsp_driver_name != "minidsp":
            return daemon_result

        # miniDSP-specific diagnostic path: layer hidraw info onto the error.
        hidraw_result = await self.check_hidraw()

        if hidraw_result.passed:
            return CheckResult(
                name=daemon_result.name,
                passed=False,
                detail=f"USB device found ({hidraw_result.detail}) but daemon unreachable: {daemon_result.detail}",
                error=daemon_result.error,
            )

        daemon_note = f"; also: {daemon_result.error}" if daemon_result.error else ""
        return CheckResult(
            name=daemon_result.name,
            passed=False,
            detail=f"USB: {hidraw_result.detail or 'not found'}; daemon: {daemon_result.detail or 'not reachable'}",
            error=f"{hidraw_result.error}{daemon_note}",
        )

    async def check_denon_and_playback(self) -> CheckResult:
        """Combined check: Denon AVR connectivity (with auto-discovery) and playback route.

        For HDMI playback route: Denon reachable implies playback is ready.
        For USB playback route: also verifies the USB audio output device.
        Returns a single CheckResult named 'Denon AVR'.
        """
        denon_result = await self.check_denon()

        if not denon_result.passed:
            return CheckResult(
                name="Denon AVR",
                passed=False,
                detail=denon_result.detail,
                error=denon_result.error,
            )

        route = self.config.measurement.get("playback_route", "usb")

        if route == "hdmi":
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=f"{denon_result.detail}; HDMI playback ready",
            )

        # USB route: also verify the USB audio output device
        playback_result = await self.check_playback_route()
        if playback_result.passed:
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=f"{denon_result.detail}; {playback_result.detail}",
            )
        return CheckResult(
            name="Denon AVR",
            passed=False,
            detail=playback_result.detail,
            error=playback_result.error,
        )

    async def check_config(self) -> CheckResult:
        """Check that config is valid.

        denon.host is optional — if not set, check_denon() falls back to SSDP discovery.
        The Config check currently validates presence of non-discoverable required fields.
        """
        host = self.config.denon.get("host")
        detail = "denon.host not set (will use SSDP auto-discovery)" if not host else "All fields present"
        return CheckResult(name="Config", passed=True, detail=detail)

    async def check_audio_stack_clean(self) -> CheckResult:
        """Detect competing userspace audio managers (PipeWire/PulseAudio) on the host.

        cal-mode writes the sweep into ``hw:Loopback,0,0``; CamillaDSP captures
        from ``hw:Loopback,1,0``. PipeWire's default behavior is to claim
        ``snd-aloop`` as a managed sink with name
        ``alsa_output.platform-snd_aloop.0.analog-stereo``. Even when nothing
        is actively linked, PipeWire and wireplumber hold ALSA control handles
        and may auto-route audio between sinks unpredictably — observed during
        cal-mode debugging where the sweep was reaching the AVR via a path we
        couldn't trace until we disabled PipeWire.

        Reads ``/proc/asound/cards`` and checks whether any non-CamillaDSP
        process holds ``controlC*`` or ``pcm*`` devices on the audio cards we
        depend on (Loopback, USB-DAC). Returns a warning result if PipeWire/
        wireplumber/pipewire-pulse are present in the holders.
        """
        from pathlib import Path

        proc_asound = Path("/proc/asound")
        if not proc_asound.exists():
            return CheckResult(
                name="Audio stack",
                passed=True,
                detail="No /proc/asound visible — cannot inspect ALSA holders, skipping",
            )

        try:
            import subprocess
            result = subprocess.run(
                ["fuser", "-v", "/dev/snd/controlC2", "/dev/snd/controlC3"],
                capture_output=True, text=True, timeout=5,
            )
            holders = (result.stdout or "") + (result.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CheckResult(
                name="Audio stack",
                passed=True,
                detail=f"Could not run fuser ({exc}) — cannot verify ALSA holders, skipping",
            )

        offenders: list[str] = []
        for line in holders.splitlines():
            line_lower = line.lower()
            for bad in ("pipewire", "wireplumber", "pulseaudio"):
                if bad in line_lower:
                    offenders.append(bad)
        offenders = sorted(set(offenders))

        if offenders:
            return CheckResult(
                name="Audio stack",
                passed=False,
                detail=(
                    f"Userspace audio managers holding ALSA devices: {', '.join(offenders)}. "
                    "cal-mode routing is unreliable when these are active — they may auto-route "
                    "the sweep to the AVR or other sinks."
                ),
                error=(
                    "Disable on the host: "
                    "systemctl --user disable --now pipewire pipewire-pulse wireplumber "
                    "pipewire.socket pipewire-pulse.socket"
                ),
            )
        return CheckResult(
            name="Audio stack",
            passed=True,
            detail="No PipeWire/PulseAudio holders on /dev/snd",
        )

    async def check_output_routing_safety(self) -> CheckResult:
        """Warn when transducers are cabled to Focusrite Monitor-bus outputs.

        The Scarlett 18i20's back-panel Line Outs 01-04 are hardwired to its
        Monitor 1/2 front-panel knobs — read-only in software, so writes via
        `amixer`/CamillaDSP silently have no effect on level. Lines 05-10 are
        direct PCM DACs (full software control). We hit this on run 14 where
        the front-right sub was cabled to Line 02 (Monitor 1 R), silently
        attenuated ~33 dB by a turned-down knob.

        The heuristic:
          - Only warns on CamillaDSP (miniDSP-native installs are unaffected).
          - Only inspects transducers with role in {sub, shaker, main, surround,
            atmos, tactile} — unused slots are skipped by design.
          - Output indices 0-3 (0-indexed) == Lines 01-04 (1-indexed on the
            back panel). If any active output lands there, warn with the list.

        Non-fatal: calibration can still proceed, but the user is informed so
        they can re-cable before running a long session that will silently
        mis-correct the wrong channel.
        """
        if self.config.dsp_driver_name != "camilladsp":
            return CheckResult(
                name="Output routing",
                passed=True,
                detail="not applicable (non-CamillaDSP DSP)",
            )

        graph = self.config.signal_graph
        transducers = getattr(graph, "transducers", ()) or ()
        if not transducers:
            return CheckResult(
                name="Output routing",
                passed=True,
                detail="no signal_graph transducers to inspect",
            )

        monitor_bus_offenders: list[str] = []
        for t in transducers:
            role = (getattr(t, "role", "") or "").lower()
            if role in {"", "unused"}:
                continue
            idx = getattr(t, "output_index", None)
            if isinstance(idx, int) and 0 <= idx <= 3:
                tname = getattr(t, "name", f"idx{idx}")
                line_label = f"Line 0{idx + 1}"
                monitor_bus_offenders.append(f"{tname}→{line_label}")

        if not monitor_bus_offenders:
            return CheckResult(
                name="Output routing",
                passed=True,
                detail="all active outputs on direct-PCM lines (5-10)",
            )

        joined = ", ".join(monitor_bus_offenders)
        return CheckResult(
            name="Output routing",
            passed=False,
            detail=(
                f"{len(monitor_bus_offenders)} transducer(s) on Focusrite Monitor-bus "
                f"Lines 01-04: {joined}"
            ),
            error=(
                "Lines 01-04 on the Scarlett 18i20 are slaved to the Monitor 1/2 "
                "front-panel knobs and are READ-ONLY in software. A turned-down "
                "knob will silently attenuate calibration signal without any "
                "software indication. Move these cables to Lines 05-10 (direct "
                "PCM, full software control) and update signal_graph.transducers "
                "output_index values accordingly."
            ),
        )

    async def check_dsp_persisted_state(self) -> CheckResult:
        """Warn when per-output DSP state persisted in active_dsp_state is non-default.

        `active_dsp_state` re-applies on every container restart — a polarity
        flip, gain trim, delay, or FIR set in a prior calibration session stays
        active on the hardware forever. These re-application silently invalidate
        any subsequent alignment or optimization analysis because a solo-sub IR
        captured with an active polarity flip (or unequal gain) will NOT reflect
        the physical sub — it reflects the sub-plus-persisted-state, and any
        tool that sums those IRs to predict combined response will give wrong
        answers until the state is acknowledged or cleared.

        This is a WARNING, not a failure — the state may be intentional (e.g.
        a previously-tuned calibration that should be preserved). Surface the
        items with their timestamps and let the user decide.
        """
        try:
            from .storage import SessionStore
            store = SessionStore()
            state = await asyncio.to_thread(store.get_active_dsp)
        except Exception as exc:
            return CheckResult(
                name="DSP persisted state",
                passed=True,
                detail=f"could not inspect active_dsp_state ({exc}); skipped",
            )

        # What counts as non-default: polarity inverted, gain != 0 dB, delay != 0 ms,
        # FIR with nonzero taps. EQ and mute are out of scope — EQ is explicitly
        # expected to be set post-calibration, mute changes during sweeps.
        non_default: list[str] = []
        for key, data in state.items():
            parts = key.split(":")
            # Shape: processor:<name>:<kind>:<index>:<field>   (output keys)
            #        processor:<name>:input:<field>            (input keys)
            if len(parts) < 4 or parts[0] != "processor":
                continue
            field = parts[-1]
            ts = data.get("timestamp", "?")
            ident = ":".join(parts[1:-1])  # e.g. "camilla:output:5"
            if field == "polarity" and data.get("inverted"):
                non_default.append(f"{ident} polarity=inverted (set {ts})")
            elif field == "gain":
                gain = float(data.get("gain_db") or 0.0)
                if abs(gain) > 0.01:
                    non_default.append(f"{ident} gain_db={gain:+.2f} (set {ts})")
            elif field == "delay":
                delay = float(data.get("delay_ms") or 0.0)
                if abs(delay) > 0.001:
                    non_default.append(f"{ident} delay_ms={delay:.3f} (set {ts})")
            elif field == "fir":
                taps = int(data.get("num_taps") or 0)
                if taps > 0:
                    non_default.append(f"{ident} fir_taps={taps} (set {ts})")

        if not non_default:
            return CheckResult(
                name="DSP persisted state",
                passed=True,
                detail="all per-output polarity/gain/delay/fir at defaults",
            )

        joined = "; ".join(non_default[:10])
        if len(non_default) > 10:
            joined += f"; …and {len(non_default) - 10} more"
        return CheckResult(
            name="DSP persisted state",
            passed=False,
            detail=f"{len(non_default)} persisted override(s): {joined}",
            error=(
                "active_dsp_state contains non-default per-output settings that "
                "re-apply on every container restart. A solo-sub IR captured "
                "with an active polarity flip or unequal gain will NOT reflect "
                "the physical sub — it reflects the sub plus the persisted state. "
                "Any alignment / optimize_sub_alignment / compare_sub_phase "
                "analysis run against these IRs will silently return wrong "
                "answers. Verify these values are intentional before calibrating. "
                "To clear: set_polarity / set_output_gain / set_delay to defaults, "
                "or end_sweep_session followed by a fresh calibration run."
            ),
        )

    async def check_capture_path_consistency(self) -> CheckResult:
        """Reject configs where direct USB capture and the LFE bridge would compete.

        With ``camilladsp.capture.device`` pointing at a non-Loopback ALSA device
        (e.g. ``plughw:USB,0``), CamillaDSP captures the multichannel USB DAC
        directly. If ``denon-sub-bridge.service`` is *also* enabled, both
        processes race for the same hardware: the bridge will fail to capture,
        respawn forever via Restart=always, and the user sees random xruns plus
        constantly-flapping logs. Refuse to start until exactly one path is
        active.

        Inverse case is also a config error: capture set to ``hw:Loopback,1,0``
        but the bridge service is disabled means CamillaDSP captures silence
        forever (the loopback's write side has no producer).
        """
        if self.config.dsp_driver_name != "camilladsp":
            return CheckResult(
                name="Capture path",
                passed=True,
                detail="dsp_driver is not camilladsp; check skipped",
            )

        cam = self.config.camilladsp
        capture = cam.get("capture") or {}
        device = str(capture.get("device", ""))
        if not device:
            return CheckResult(
                name="Capture path",
                passed=True,
                detail="no explicit capture device; defaults apply",
            )

        bridge_service = cam.get("bridge_service")
        is_loopback = device.startswith("hw:Loopback")
        bridge_enabled = await self._systemctl_is_enabled(bridge_service) if bridge_service else False
        bridge_running = await self._systemctl_is_active(bridge_service) if bridge_service else False

        if is_loopback:
            if not bridge_service:
                return CheckResult(
                    name="Capture path",
                    passed=False,
                    detail=f"capture={device} but no bridge_service configured",
                    error=(
                        "CamillaDSP is configured to capture the ALSA Loopback, "
                        "but no bridge_service is set in camilladsp.* — nothing "
                        "is feeding the loopback's write side, so calibration "
                        "captures pure silence. Either set "
                        "camilladsp.bridge_service: 'denon-sub-bridge.service' "
                        "or switch capture.device to direct USB (e.g. plughw:USB,0)."
                    ),
                )
            return CheckResult(
                name="Capture path",
                passed=True,
                detail=f"loopback path: capture={device}, bridge={bridge_service}",
            )

        # Direct-USB capture path. Bridge must be off.
        if bridge_enabled or bridge_running:
            return CheckResult(
                name="Capture path",
                passed=False,
                detail=(
                    f"capture={device} (direct USB) but {bridge_service} is "
                    f"{'running' if bridge_running else 'enabled'}"
                ),
                error=(
                    "Direct USB capture and the LFE bridge cannot both be "
                    "active — they race for the same hardware. Disable the "
                    "bridge: `sudo systemctl disable --now "
                    f"{bridge_service}`. Keep the unit file in place as a "
                    "rollback option; the preflight check enforces mutual "
                    "exclusion, not removal."
                ),
            )
        return CheckResult(
            name="Capture path",
            passed=True,
            detail=f"direct capture={device}, bridge service inactive",
        )

    async def _systemctl_is_enabled(self, service: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-enabled", service,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and stdout.decode().strip() == "enabled"
        except (FileNotFoundError, PermissionError):
            return False

    async def _systemctl_is_active(self, service: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", service,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            return proc.returncode == 0 and stdout.decode().strip() == "active"
        except (FileNotFoundError, PermissionError):
            return False
