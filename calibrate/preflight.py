"""Hardware pre-flight checks — verify mic, miniDSP, and Denon AVR are reachable."""

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Config


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
        """Run all hardware checks concurrently. Never raises — errors become failed results."""
        checks = [
            ("Microphone", self.check_mic()),
            ("miniDSP", self.check_minidsp()),
            ("Denon AVR", self.check_denon()),
            ("Playback Route", self.check_playback_route()),
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
        """Check that minidspd is running and has a device connected."""
        host = self.config.minidsp.get("host", "localhost")
        port = self.config.minidsp.get("port", 5380)
        url = f"http://{host}:{port}/devices"

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url)
                response.raise_for_status()
                devices = response.json()

            if not devices:
                return CheckResult(
                    name="miniDSP",
                    passed=False,
                    detail=f"minidspd reachable at {host}:{port} but no devices found",
                    error="Connect the miniDSP 2x4 HD via USB and retry",
                )

            device = devices[0]
            product = device.get("product_name", "Unknown")
            serial = device.get("version", {}).get("serial", "")
            serial_str = f" (serial {serial})" if serial else ""

            return CheckResult(
                name="miniDSP",
                passed=True,
                detail=f"{product} at {host}:{port}{serial_str}",
            )

        except httpx.ConnectError:
            return CheckResult(
                name="miniDSP",
                passed=False,
                detail=f"Cannot reach minidspd at {host}:{port}",
                error="Start the daemon: run 'minidspd' in a separate terminal",
            )
        except httpx.TimeoutException:
            return CheckResult(
                name="miniDSP",
                passed=False,
                detail=f"Timeout connecting to minidspd at {host}:{port}",
                error="minidspd may be starting — wait a moment and retry",
            )
        except Exception as exc:
            return CheckResult(
                name="miniDSP",
                passed=False,
                detail="",
                error=str(exc),
            )

    async def check_denon(self) -> CheckResult:
        """Check that the Denon AVR is online at the configured IP."""
        host = self.config.denon.get("host")
        if not host:
            return CheckResult(
                name="Denon AVR",
                passed=False,
                detail="",
                error="denon.host not set — edit ~/.avr-calibration/config.yaml",
            )

        try:
            import denonavr
            receiver = denonavr.DenonAVR(host)
            await receiver.async_setup()
            model = receiver.model_name or "Denon AVR"
            return CheckResult(
                name="Denon AVR",
                passed=True,
                detail=f"{model} online at {host}",
            )
        except Exception as exc:
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
            # HDMI route requires a reachable Denon AVR
            host = self.config.denon.get("host")
            if not host:
                return CheckResult(
                    name="Playback Route",
                    passed=False,
                    detail="HDMI route requires denon.host — edit ~/.avr-calibration/config.yaml",
                    error="Set denon.host and measurement.playback_route",
                )
            try:
                import denonavr
                receiver = denonavr.DenonAVR(host)
                await receiver.async_setup()
                model = receiver.model_name or "Denon AVR"
                return CheckResult(
                    name="Playback Route",
                    passed=True,
                    detail=f"HDMI via {model} at {host}",
                )
            except Exception as exc:
                return CheckResult(
                    name="Playback Route",
                    passed=False,
                    detail=f"HDMI route: cannot connect to Denon AVR at {host}",
                    error=str(exc),
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
