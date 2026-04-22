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
            return CheckResult(
                name=display,
                passed=True,
                detail=f"{display} at {host}:{port}",
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
