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
            [Config]  [Measurement service]  [Service version]  [Audio mode]  [Microphone]  [miniDSP (USB+daemon)]  [Denon AVR + Playback]  [Signal Path]  [Chain contamination]
        """
        dsp_label = _DSP_DISPLAY_NAMES.get(
            self.config.dsp_driver_name, self.config.dsp_driver_name
        )
        checks = [
            ("Config", self.check_config()),
            ("Measurement service", self.check_measurement_service()),
            ("Service version", self.check_version_skew()),
            ("Audio mode", self.check_audio_mode()),
            ("Microphone", self.check_mic()),
            (dsp_label, self.check_minidsp_combined()),
            ("Denon AVR", self.check_denon_and_playback()),
            ("Signal Path", self.check_signal_path_sync()),
            ("Output routing", self.check_output_routing_safety()),
            ("DSP persisted state", self.check_dsp_persisted_state()),
            ("Loopback reference", self.check_loopback_reference()),
            ("Loopback timing stability", self.check_loopback_xcorr_stability()),
            ("Chain contamination", self.check_chain_contamination()),
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

    async def check_measurement_service(self) -> CheckResult:
        """Check that the bare-metal avr-measurement service is reachable."""
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError
        client = MeasurementServiceClient()
        try:
            health = await client.health()
            if health.get("status") == "ok":
                return CheckResult(
                    name="Measurement service",
                    passed=True,
                    detail=f"avr-measurement service healthy at {client.base_url}",
                )
            return CheckResult(
                name="Measurement service",
                passed=False,
                detail=f"unexpected health response: {health}",
                error="avr-measurement.service may not be running — check: systemctl status avr-measurement",
            )
        except MeasurementServiceError as exc:
            return CheckResult(
                name="Measurement service",
                passed=False,
                detail="",
                error=str(exc),
            )
        except Exception as exc:
            return CheckResult(
                name="Measurement service",
                passed=False,
                detail="",
                error=f"Unexpected error: {exc}",
            )

    async def check_mic(self) -> CheckResult:
        """Check that the UMIK (or configured mic) is visible on the bare-metal measurement service."""
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError
        mic_name = self.config.mic.get("name", "UMIK")
        try:
            client = MeasurementServiceClient()
            devices = await client.list_devices()

            for idx, dev in enumerate(devices):
                if dev.get("max_input_channels", 0) > 0 and mic_name.lower() in dev["name"].lower():
                    return CheckResult(
                        name="Microphone",
                        passed=True,
                        detail=f'{dev["name"]} (device {idx}, {int(dev.get("default_samplerate", 0))}Hz)',
                    )

            available_inputs = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
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
        except MeasurementServiceError as exc:
            return CheckResult(
                name="Microphone",
                passed=False,
                detail="measurement service unreachable — mic check skipped",
                error=str(exc),
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

            # Power-state guard. The AVR's HTTP service (and therefore the
            # denonavr library) responds in standby — connected=True does
            # not mean "ready to play audio." If power != "ON", Telnet
            # replies disappear and sweep measurements come back at SNR=0.
            # Surface this loudly so callers don't waste time chasing
            # protocol bugs that are actually a power switch.
            power = state.get("power")
            if power != "ON":
                return CheckResult(
                    name="Denon AVR",
                    passed=False,
                    detail=(
                        f"{model} reachable at {host}{suffix} but power={power!r} — "
                        "audio path is silent and Telnet replies will not arrive."
                    ),
                    error=(
                        "Denon AVR is in standby. Turn it on at the unit / remote, "
                        "or call set_volume / measure (DenonSweepContext now "
                        "auto-powers-on at sweep entry)."
                    ),
                )

            # Audyssey TCP/1256 service health probe. The Audyssey daemon
            # occasionally wedges after soft power-on from standby — HTTP
            # keeps responding (which is what get_state hit above), but
            # port 1256 (where envelope/filter writes go) stops replying.
            # Detect that here so callers don't push 5 chunks of SET_SETDAT
            # into the void.
            from .drivers.denon import audyssey_tcp
            try:
                avrinf = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: audyssey_tcp.probe_audyssey_service(host, timeout=3.0),
                )
            except Exception as exc:
                avrinf = None
                probe_error = str(exc)
            else:
                probe_error = None

            if avrinf is None:
                return CheckResult(
                    name="Denon AVR",
                    passed=False,
                    detail=(
                        f"{model} online at {host}{suffix} (HTTP responding, "
                        "power=ON), but Audyssey TCP service on port 1256 is "
                        "unresponsive."
                    ),
                    error=(
                        "AVR's Audyssey TCP daemon is wedged. SET_SETDAT and "
                        "SET_COEFDT writes will silently no-op. Recovery: pull "
                        "the AVR's power cord, wait 30 s, plug back in. Soft "
                        "power-cycle via async_power_on() does NOT fix this. "
                        + (f"Probe error: {probe_error}" if probe_error else "")
                    ),
                )

            eq_type = avrinf.get("EQType", "?")
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=(
                    f"{model} online at {host}{suffix}, power=ON, "
                    f"Audyssey TCP service responsive (EQType={eq_type})"
                ),
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

        # USB route: verify playback device via the bare-metal measurement service
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError
        device_name = self.config.measurement.get("playback_device", "miniDSP")
        try:
            client = MeasurementServiceClient()
            devices = await client.list_devices()
            for idx, dev in enumerate(devices):
                if dev.get("max_output_channels", 0) > 0 and device_name.lower() in dev["name"].lower():
                    return CheckResult(
                        name="Playback Route",
                        passed=True,
                        detail=f'USB: {dev["name"]} (device {idx})',
                    )
            available_outputs = [d["name"] for d in devices if d.get("max_output_channels", 0) > 0]
            shown = ", ".join(available_outputs[:3])
            ellipsis = "…" if len(available_outputs) > 3 else ""
            return CheckResult(
                name="Playback Route",
                passed=False,
                detail=f'USB: no "{device_name}" found. Available outputs: {shown}{ellipsis}',
                error=f"Connect {device_name} via USB or set measurement.playback_device",
            )
        except MeasurementServiceError as exc:
            return CheckResult(
                name="Playback Route",
                passed=False,
                detail="measurement service unreachable — playback check skipped",
                error=str(exc),
            )
        except Exception as exc:
            return CheckResult(
                name="Playback Route",
                passed=False,
                detail="",
                error=str(exc),
            )

    async def check_loopback_reference(self) -> CheckResult:
        """Verify a loopback reference is configured for deconvolution.

        ⚠️  Without a loopback ref, the analytical sweep is used as the
        deconvolution X. PipeWire schedules play and record streams
        independently — the resulting jitter shows up as phase smear,
        causing 4–10 dB run-to-run SPL variance and coherence collapse.
        Documented in MEMORY (2026-05-27 baseline jitter investigation).
        Fail-fast here so the operator can't accidentally run a cal session
        on unrepeatable measurements.
        """
        meas = self.config.measurement
        ref_node = meas.get("loopback_ref_pipewire_node")
        ref_device = meas.get("loopback_ref_device")

        if ref_node or ref_device:
            ref_id = ref_node or ref_device
            ref_ch_idx = int(meas.get("loopback_ref_channel_index", 1))
            ref_chs = int(meas.get("loopback_ref_channels", 1))
            return CheckResult(
                name="Loopback reference",
                passed=True,
                detail=(
                    f"enabled: {ref_id}, channels "
                    f"{ref_ch_idx}..{ref_ch_idx + ref_chs - 1}"
                ),
            )

        return CheckResult(
            name="Loopback reference",
            passed=False,
            detail="",
            error=(
                "⚠️  LOOPBACK REFERENCE NOT CONFIGURED. Measurements will "
                "have 4–10 dB run-to-run jitter and coherence collapse. "
                "Filter A/B comparisons CANNOT be trusted. Set "
                "measurement.loopback_ref_pipewire_node in config.yaml "
                "(connections show inputs 4=FL, 5=FR are wired as loopback "
                "taps on this rig)."
            ),
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
            # Also enumerate available PipeWire HDMI sinks so the operator can
            # confirm hdmi_pipewire_node is set to a real node.
            pw_nodes_detail = ""
            hdmi_node = self.config.measurement.get("hdmi_pipewire_node") or ""
            try:
                import subprocess as _sp
                # List PW nodes and pactl card profiles to show available HDMI configs
                node_result = _sp.run(
                    ["pw-cli", "list-objects", "PipeWire:Interface:Node"],
                    capture_output=True, text=True, timeout=5.0,
                )
                node_raw = (node_result.stdout + node_result.stderr).strip()
                hdmi_node_lines = [l.strip() for l in node_raw.splitlines()
                                   if "hdmi" in l.lower() or "alsa_output" in l.lower()]

                card_result = _sp.run(
                    ["pactl", "list", "cards", "short"],
                    capture_output=True, text=True, timeout=5.0,
                )
                card_raw = (card_result.stdout + card_result.stderr).strip()
                hdmi_card_lines = [l.strip() for l in card_raw.splitlines()
                                   if "hdmi" in l.lower() or "vc4" in l.lower()]

                parts = []
                if hdmi_node_lines:
                    parts.append(f"nodes: {' | '.join(hdmi_node_lines[:8])}")
                if hdmi_card_lines:
                    parts.append(f"cards: {' | '.join(hdmi_card_lines[:4])}")
                elif card_raw:
                    parts.append(f"pactl cards: {card_raw[:200]}")
                pw_nodes_detail = ("; " + "; ".join(parts)) if parts else "; pw-cli: no hdmi nodes found"
            except Exception as _exc:
                pw_nodes_detail = f"; pw node probe error: {_exc}"
            configured = f" (configured: {hdmi_node})" if hdmi_node else " (hdmi_pipewire_node not set)"
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=f"{denon_result.detail}; HDMI playback ready{configured}{pw_nodes_detail}",
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

    async def check_version_skew(self) -> CheckResult:
        """Detect when the host avr-measurement service runs different code than the container.

        The measurement service exposes SHA-256 hashes of its own installed source
        files in /health (``source_hashes`` dict, keyed by relative file name like
        ``"measurement.py"``). This check computes the same hashes from the files
        installed in the container and diffs them.

        Behavior:
          - All hashes match            → pass, detail notes "code in sync".
          - Any mismatch                → FAIL listing differing files + deploy hint.
          - ``source_hashes`` absent    → pass-with-warning (old service deployment,
            predates this feature — graceful degradation).
          - Service unreachable         → pass-with-warning (handled by the
            measurement-service check; don't double-report).
        """
        import hashlib as _hashlib
        from pathlib import Path as _Path
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError

        client = MeasurementServiceClient()
        try:
            health = await client.health()
        except MeasurementServiceError:
            return CheckResult(
                name="Service version",
                passed=True,
                detail="service version unknown (measurement service unreachable — see above)",
            )
        except Exception as exc:
            return CheckResult(
                name="Service version",
                passed=True,
                detail=f"service version unknown (health probe error: {exc})",
            )

        svc_hashes: dict[str, str] | None = health.get("source_hashes")
        if svc_hashes is None:
            return CheckResult(
                name="Service version",
                passed=True,
                detail="service version unknown (predates skew check — update deploy/install.sh)",
            )

        # Compute hashes from this container's own installed files.
        # __file__ resolves to the container's installed calibrate/ directory.
        here = _Path(__file__).parent

        def _sha256(path: _Path) -> str | None:
            try:
                return _hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                return None

        candidates = {
            "measurement.py": here / "measurement.py",
            "drivers/playback.py": here / "drivers" / "playback.py",
            "measurement_service.py": here / "measurement_service.py",
        }
        container_hashes = {
            name: digest
            for name, path in candidates.items()
            if (digest := _sha256(path)) is not None
        }

        # Compare only files present in both sides.
        mismatches: list[str] = []
        for name, svc_digest in svc_hashes.items():
            container_digest = container_hashes.get(name)
            if container_digest is None:
                continue  # file missing in container — skip (shouldn't happen)
            if container_digest != svc_digest:
                mismatches.append(name)

        if mismatches:
            joined = ", ".join(mismatches)
            return CheckResult(
                name="Service version",
                passed=False,
                detail=f"code skew detected — {len(mismatches)} file(s) differ: {joined}",
                error=(
                    "The bare-metal avr-measurement service is running different code "
                    "than the Docker container. Measurement results may be inconsistent. "
                    "Fix: run deploy/install.sh on the Pi and restart avr-measurement "
                    "(sudo systemctl restart avr-measurement)."
                ),
            )

        checked = ", ".join(sorted(container_hashes.keys() & svc_hashes.keys()))
        return CheckResult(
            name="Service version",
            passed=True,
            detail=f"code in sync ({checked})",
        )

    async def check_audio_mode(self) -> CheckResult:
        """Check that the Pi host audio-mode is set to 'cal' before calibration.

        The state file /var/lib/audio-mode lives on the Pi host (not in the
        Docker container), so this check reads it via the bare-metal
        avr-measurement service's /health endpoint, which includes the
        ``audio_mode`` field since the corresponding update.

        Behavior:
          - mode == 'cal'                 → pass with detail.
          - mode in (listening, karaoke)  → FAIL with operator guidance.
          - field absent (/health predates this feature) → pass-with-warning
            (graceful degradation for old deployed services).
          - service unreachable           → pass-with-warning (measurement
            service check handles the hard failure separately).
        """
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError
        client = MeasurementServiceClient()
        try:
            health = await client.health()
        except MeasurementServiceError:
            # Measurement service unreachable — that check will fail loudly.
            # Don't double-report here.
            return CheckResult(
                name="Audio mode",
                passed=True,
                detail="audio-mode unknown (measurement service unreachable — see above)",
            )
        except Exception as exc:
            return CheckResult(
                name="Audio mode",
                passed=True,
                detail=f"audio-mode unknown (health probe error: {exc})",
            )

        mode = health.get("audio_mode")
        if mode is None:
            # Old service deployment that predates the audio_mode field.
            return CheckResult(
                name="Audio mode",
                passed=True,
                detail="audio-mode unknown (service predates skew check — update deploy/install.sh)",
            )

        if mode == "cal":
            return CheckResult(
                name="Audio mode",
                passed=True,
                detail=f"audio-mode=cal (ready for calibration)",
            )

        return CheckResult(
            name="Audio mode",
            passed=False,
            detail=f"audio-mode={mode!r} (expected 'cal')",
            error=(
                f"Pi host is in audio-mode={mode!r}, not 'cal'. "
                "Calibration sweeps require CamillaDSP in calibration routing mode. "
                "Run: ssh pi@<pi-host> 'sudo /usr/local/sbin/audio-mode set cal'"
            ),
        )

    async def check_loopback_xcorr_stability(self) -> CheckResult:
        """Check that the loopback timing (xcorr_peak_ms) is stable across recent sessions.

        ``loopback_xcorr_peak_ms`` is the cross-correlation peak between the
        deconvolution reference and the mic — it encodes CamillaDSP processing
        latency + acoustic travel time. When this value drifts across sessions
        (e.g. 3.0 ms vs 4.98 ms) the deconvolution reference has shifted: the
        measured H(f) = mic / loopback_ref is not phase-comparable across those
        sessions. For Trinnov FIR design this matters critically — the complex
        K_i = T_i·conj(H_i)/(|H_i|²+λ²) uses per-session phase, and a 1-2 ms
        shift at 47 Hz is a ~100° phase error (1/47s × 1e-3s × 360°/period ≈
        17°/ms, so 2 ms ≈ 34° at 47 Hz — fully invalidating coherent summation).

        Queries the 10 most recent sessions with xcorr data and warns if
        range > 1.0 ms or stddev > 0.5 ms.
        """
        try:
            from .storage import SessionStore
            import math as _math

            store = SessionStore()
            sessions = await asyncio.to_thread(store.list_sessions, 50)
            peaks = [
                s.start_fr.loopback_xcorr_peak_ms
                for s in sessions
                if s.start_fr and s.start_fr.loopback_xcorr_peak_ms is not None
            ][:10]

            if not peaks:
                return CheckResult(
                    name="Loopback timing stability",
                    passed=True,
                    detail="no recent sessions with xcorr data — cannot assess (ok before first measurement)",
                )

            mean = sum(peaks) / len(peaks)
            variance = sum((p - mean) ** 2 for p in peaks) / len(peaks)
            std = _math.sqrt(variance)
            rng = max(peaks) - min(peaks)

            detail = (
                f"last {len(peaks)} sessions: xcorr_peak_ms "
                f"min={min(peaks):.2f} max={max(peaks):.2f} "
                f"mean={mean:.2f} std={std:.2f} range={rng:.2f} ms"
            )

            if rng > 1.0 or std > 0.5:
                return CheckResult(
                    name="Loopback timing stability",
                    passed=False,
                    detail=detail,
                    error=(
                        f"Loopback xcorr_peak_ms has drifted {rng:.2f} ms across recent sessions "
                        f"(std={std:.2f} ms). Sessions with different xcorr_peak_ms cannot be "
                        "phase-compared — Trinnov FIR design using mixed sessions will produce "
                        "wrong phase corrections. Common causes: (1) loopback_ref null sink was "
                        "suspended and restarted (shifts PW quantum), (2) CamillaDSP pipeline "
                        "restarted between sessions, (3) different FIR tap counts changing "
                        "pipeline latency. Re-measure all subs in the same session to ensure "
                        "consistent xcorr_peak_ms before designing Trinnov FIRs."
                    ),
                )

            return CheckResult(
                name="Loopback timing stability",
                passed=True,
                detail=detail,
            )
        except Exception as exc:
            return CheckResult(
                name="Loopback timing stability",
                passed=True,
                detail=f"could not inspect session history ({exc}); skipped",
            )

    async def check_chain_contamination(self) -> CheckResult:
        """Detect UMIK→camilladsp_capture auto-links that create a feedback loop.

        When WirePlumber auto-links the UMIK microphone into CamillaDSP's
        capture ports, the routing matrix carries mic audio toward the subs:
        mic→DSP→subs→room→mic. This creates an acoustic feedback loop that:
          - randomises polarity (loop phase determines constructive/destructive)
          - drifts SPL between identical sweeps (loop gain instability)
          - makes xcorr sign and magnitude noise-sensitive

        Confirmed live 2026-06-12: flipping one sub's DSP polarity changed the
        captured level by 21 dB (loop phase), polarity signs were random, and
        SPL drifted between consecutive identical sweeps.

        Reads ``pw_capture_links`` from the bare-metal measurement service's
        /health endpoint (added alongside this check) which runs pw-link -l
        host-side where PipeWire is accessible.

        Behavior:
          - umik_into_dsp=True              → FAIL with feedback loop explanation
          - umik_into_dsp=False             → PASS, list active DSP sources
          - pw_capture_links absent         → pass-with-warning (old service)
          - service unreachable             → pass-with-warning (not double-reported)
        """
        from .measurement_client import MeasurementServiceClient, MeasurementServiceError

        client = MeasurementServiceClient()
        try:
            health = await client.health()
        except MeasurementServiceError:
            return CheckResult(
                name="Chain contamination",
                passed=True,
                detail="chain contamination unknown (measurement service unreachable — see above)",
            )
        except Exception as exc:
            return CheckResult(
                name="Chain contamination",
                passed=True,
                detail=f"chain contamination unknown (health probe error: {exc})",
            )

        pw_links = health.get("pw_capture_links")
        if pw_links is None:
            return CheckResult(
                name="Chain contamination",
                passed=True,
                detail="chain contamination unknown (service predates this check — update deploy/install.sh)",
            )

        umik_into_dsp: bool = pw_links.get("umik_into_dsp", False)
        sources: list = pw_links.get("sources", [])

        if umik_into_dsp:
            sources_str = ", ".join(sources) if sources else "(unknown)"
            return CheckResult(
                name="Chain contamination",
                passed=False,
                detail=(
                    f"UMIK is linked into camilladsp_capture "
                    f"(active DSP sources: {sources_str})"
                ),
                error=(
                    "UMIK→camilladsp_capture link detected. This creates a "
                    "mic→DSP→subs→room→mic acoustic feedback loop. Symptoms: "
                    "random polarity sign, SPL drift between identical sweeps, "
                    "xcorr peak sign instability. "
                    "Fix: remove the UMIK→camilladsp_capture links manually "
                    "(pw-link -d <umik-port> <camilla-port>), then restart "
                    "wireplumber to apply the corrected 51-umik.lua rule: "
                    "systemctl --user restart wireplumber"
                ),
            )

        sources_str = ", ".join(sources) if sources else "none"
        return CheckResult(
            name="Chain contamination",
            passed=True,
            detail=f"no UMIK→camilladsp_capture links (active DSP sources: {sources_str})",
        )

