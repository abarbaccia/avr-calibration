"""Tests for hardware_mute_output and diagnose_audio_stack MCP tools.

Coverage:
  hardware_mute_output (MCP tool):
    - Happy path: mute and unmute delegated to MeasurementServiceClient
    - Unknown output_index → error (service rejects it; also checked here)
    - MeasurementServiceError → _err propagated
    - Unexpected exception → _err propagated

  _tool_hardware_mute_output (integration via call_tool dispatcher):
    - Registered and reachable via the dispatch table

  diagnose_audio_stack (MCP tool):
    - Happy path healthy → ok=True, healthy=True, warnings=[]
    - Unhealthy response → ok=True, healthy=False, warnings non-empty
    - MeasurementServiceError → _err propagated
    - Unexpected exception → _err propagated

  hardware_mute_output (service endpoint helpers):
    - _output_index_to_scarlett_line: formula validation
    - Known vs unknown output_index validation

  _check_pw_link_l (wiring parser):
    - All three critical links detected
    - UMIK→camilladsp_capture detected as hazard
    - Missing links produce correct booleans
    - pw-link not found → error string

  _check_pw_dump_umik (UMIK node parser):
    - UMIK present: resample_quality, autoconnect, node_state extracted
    - UMIK absent → present=False
    - pw-dump JSON error → error string

  _check_camilladsp_state:
    - websockets ImportError → error
    - Connection error → error

  _check_service_states:
    - Returns active/inactive for each named service
    - Handles subprocess failure gracefully
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

_MEAS_CLIENT = "calibrate.measurement_client.MeasurementServiceClient"


# ── MCP tool handler tests ─────────────────────────────────────────────────────


class TestHardwareMuteOutputTool:
    """Tests for _tool_hardware_mute_output (the MCP server handler)."""

    async def test_mute_happy_path(self):
        """Successful mute delegates to client and returns ok result."""
        from calibrate.mcp_server import _tool_hardware_mute_output

        expected = {
            "ok": True,
            "output_index": 5,
            "line": "Line 06 Mute",
            "muted": True,
            "amixer_card": "USB",
        }
        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.hardware_mute_output = AsyncMock(return_value=expected)
            result = await _tool_hardware_mute_output(output_index=5, muted=True)

        assert result["ok"] is True
        assert result["output_index"] == 5
        assert result["line"] == "Line 06 Mute"
        assert result["muted"] is True
        MockClient.return_value.hardware_mute_output.assert_awaited_once_with(
            output_index=5, muted=True
        )

    async def test_unmute_happy_path(self):
        """Successful unmute delegates to client with muted=False."""
        from calibrate.mcp_server import _tool_hardware_mute_output

        expected = {
            "ok": True,
            "output_index": 7,
            "line": "Line 08 Mute",
            "muted": False,
            "amixer_card": "USB",
        }
        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.hardware_mute_output = AsyncMock(return_value=expected)
            result = await _tool_hardware_mute_output(output_index=7, muted=False)

        assert result["ok"] is True
        assert result["muted"] is False

    async def test_measurement_service_error(self):
        """MeasurementServiceError is wrapped as _err response."""
        from calibrate.mcp_server import _tool_hardware_mute_output
        from calibrate.measurement_client import MeasurementServiceError

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.hardware_mute_output = AsyncMock(
                side_effect=MeasurementServiceError("avr-measurement service unreachable")
            )
            result = await _tool_hardware_mute_output(output_index=6, muted=True)

        assert result["ok"] is False
        assert "unreachable" in result["error"]

    async def test_unexpected_exception(self):
        """Unexpected exceptions are wrapped as _err response."""
        from calibrate.mcp_server import _tool_hardware_mute_output

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.hardware_mute_output = AsyncMock(
                side_effect=RuntimeError("boom")
            )
            result = await _tool_hardware_mute_output(output_index=5, muted=True)

        assert result["ok"] is False
        assert "boom" in result["error"]

    async def test_registered_in_dispatch_table(self):
        """hardware_mute_output is reachable via the MCP call_tool dispatcher."""
        from calibrate.mcp_server import call_tool

        expected = {
            "ok": True,
            "output_index": 5,
            "line": "Line 06 Mute",
            "muted": True,
            "amixer_card": "USB",
        }
        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.hardware_mute_output = AsyncMock(return_value=expected)
            results = await call_tool(
                "hardware_mute_output", {"output_index": 5, "muted": True}
            )

        assert len(results) == 1
        data = json.loads(results[0].text)
        assert data["ok"] is True
        assert data["line"] == "Line 06 Mute"


class TestDiagnoseAudioStackTool:
    """Tests for _tool_diagnose_audio_stack (the MCP server handler)."""

    def _healthy_response(self) -> dict:
        return {
            "ok": True,
            "healthy": True,
            "warnings": [],
            "camilladsp": {"state": "Running", "cpu_load_percent": 12.5, "error": None},
            "umik": {
                "present": True,
                "node_state": "running",
                "resample_quality": 14,
                "autoconnect": False,
                "error": None,
            },
            "wiring": {
                "input3_linked": True,
                "loopback_ref_linked": True,
                "umik_into_dsp": False,
                "umik_sources": [],
                "playback_link_count": 20,
                "error": None,
            },
            "services": {
                "camilladsp.service": "active",
                "avr-measurement.service": "active",
                "camilladsp-watchdog.service": "active",
            },
        }

    async def test_healthy_system(self):
        """Healthy system returns ok=True, healthy=True, empty warnings."""
        from calibrate.mcp_server import _tool_diagnose_audio_stack

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.audio_stack_health = AsyncMock(
                return_value=self._healthy_response()
            )
            result = await _tool_diagnose_audio_stack()

        assert result["ok"] is True
        assert result["healthy"] is True
        assert result["warnings"] == []
        assert result["camilladsp"]["state"] == "Running"
        assert result["umik"]["resample_quality"] == 14

    async def test_unhealthy_system_propagated(self):
        """Unhealthy response (warnings present) is returned as-is."""
        from calibrate.mcp_server import _tool_diagnose_audio_stack

        response = self._healthy_response()
        response["healthy"] = False
        response["warnings"] = ["UMIK resample.quality=4 (expected 14)"]

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.audio_stack_health = AsyncMock(return_value=response)
            result = await _tool_diagnose_audio_stack()

        assert result["ok"] is True
        assert result["healthy"] is False
        assert len(result["warnings"]) == 1
        assert "resample" in result["warnings"][0]

    async def test_measurement_service_error(self):
        """MeasurementServiceError is wrapped as _err response."""
        from calibrate.mcp_server import _tool_diagnose_audio_stack
        from calibrate.measurement_client import MeasurementServiceError

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.audio_stack_health = AsyncMock(
                side_effect=MeasurementServiceError("service unreachable at 8767")
            )
            result = await _tool_diagnose_audio_stack()

        assert result["ok"] is False
        assert "unreachable" in result["error"]

    async def test_unexpected_exception(self):
        """Unexpected exceptions are wrapped as _err response."""
        from calibrate.mcp_server import _tool_diagnose_audio_stack

        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.audio_stack_health = AsyncMock(
                side_effect=RuntimeError("unexpected crash")
            )
            result = await _tool_diagnose_audio_stack()

        assert result["ok"] is False
        assert "unexpected crash" in result["error"]

    async def test_registered_in_dispatch_table(self):
        """diagnose_audio_stack is reachable via the MCP call_tool dispatcher."""
        from calibrate.mcp_server import call_tool

        healthy = {
            "ok": True,
            "healthy": True,
            "warnings": [],
            "camilladsp": {"state": "Running", "cpu_load_percent": None, "error": None},
            "umik": {
                "present": True,
                "node_state": "running",
                "resample_quality": 14,
                "autoconnect": False,
                "error": None,
            },
            "wiring": {
                "input3_linked": True,
                "loopback_ref_linked": True,
                "umik_into_dsp": False,
                "umik_sources": [],
                "playback_link_count": 20,
                "error": None,
            },
            "services": {"camilladsp.service": "active"},
        }
        with patch(_MEAS_CLIENT) as MockClient:
            MockClient.return_value.audio_stack_health = AsyncMock(return_value=healthy)
            results = await call_tool("diagnose_audio_stack", {})

        assert len(results) == 1
        data = json.loads(results[0].text)
        assert data["ok"] is True
        assert data["healthy"] is True


# ── Service endpoint helper tests ──────────────────────────────────────────────


class TestOutputIndexToScarlettLine:
    """Unit tests for the Scarlett line name formula in measurement_service."""

    def test_sub_front_right(self):
        from calibrate.measurement_service import _output_index_to_scarlett_line
        assert _output_index_to_scarlett_line(5) == "Line 06 Mute"

    def test_sub_nearfield(self):
        from calibrate.measurement_service import _output_index_to_scarlett_line
        assert _output_index_to_scarlett_line(6) == "Line 07 Mute"

    def test_shaker(self):
        from calibrate.measurement_service import _output_index_to_scarlett_line
        assert _output_index_to_scarlett_line(7) == "Line 08 Mute"

    def test_formula_general(self):
        """Confirm general formula: line_number = output_index + 1, zero-padded to 2 digits."""
        from calibrate.measurement_service import _output_index_to_scarlett_line
        # Index 9 → Line 10 (no leading zero needed, but still formatted)
        assert _output_index_to_scarlett_line(9) == "Line 10 Mute"


class TestHardwareMuteEndpoint:
    """Tests for the FastAPI /hardware_mute_output endpoint (amixer path)."""

    async def test_unknown_output_index_rejected(self):
        """output_index not in _KNOWN_OUTPUT_INDICES returns 400 without calling amixer."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app

        client = TestClient(app)
        resp = client.post("/hardware_mute_output", json={"output_index": 99, "muted": True})
        assert resp.status_code == 400
        body = resp.json()
        assert body["ok"] is False
        assert "99" in body["error"]
        assert "Known indices" in body["error"]

    async def test_amixer_not_found(self):
        """FileNotFoundError from amixer returns 500 with helpful message."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app
        import subprocess

        client = TestClient(app)
        with patch("subprocess.run", side_effect=FileNotFoundError("amixer")):
            resp = client.post("/hardware_mute_output", json={"output_index": 5, "muted": True})
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert "amixer not found" in body["error"]

    async def test_amixer_timeout(self):
        """TimeoutExpired from amixer returns 500 with timeout message."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app
        import subprocess

        client = TestClient(app)
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("amixer", 5.0)):
            resp = client.post("/hardware_mute_output", json={"output_index": 5, "muted": True})
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert "timed out" in body["error"]

    async def test_amixer_nonzero_exit(self):
        """Non-zero exit from amixer returns 500 with stderr in error message."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Unable to find control 'Line 06 Mute'"
        mock_result.stdout = ""

        client = TestClient(app)
        with patch("subprocess.run", return_value=mock_result):
            resp = client.post("/hardware_mute_output", json={"output_index": 5, "muted": True})
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False
        assert "Line 06 Mute" in body["error"]

    async def test_mute_success(self):
        """Successful amixer mute returns ok=True with line name and card."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_result.stdout = "Simple mixer control 'Line 06 Mute',0\n  Item0: 'On'\n"

        client = TestClient(app)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            resp = client.post("/hardware_mute_output", json={"output_index": 5, "muted": True})

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["output_index"] == 5
        assert body["line"] == "Line 06 Mute"
        assert body["muted"] is True
        assert body["amixer_card"] == "USB"

        # Verify amixer was called with the right arguments
        mock_run.assert_called_once_with(
            ["amixer", "-c", "USB", "sset", "Line 06 Mute", "on"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )

    async def test_unmute_uses_off(self):
        """muted=False passes 'off' to amixer."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        mock_result.stdout = ""

        client = TestClient(app)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            resp = client.post(
                "/hardware_mute_output", json={"output_index": 7, "muted": False}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["muted"] is False
        mock_run.assert_called_once_with(
            ["amixer", "-c", "USB", "sset", "Line 08 Mute", "off"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )


# ── PipeWire link parser tests ─────────────────────────────────────────────────


class TestCheckPwLinkL:
    """Tests for _check_pw_link_l (pw-link -l output parser)."""

    # Sample pw-link -l output fragments
    _GOOD_WIRING = """\
avr_cal_sweep:monitor_FL
  |-> camilladsp_capture:input_3
  |-> loopback_ref:playback_1
camilladsp_playback:output_1
  |-> alsa_output.usb-Focusrite_Scarlett_18i20:playback_AUX0
camilladsp_playback:output_2
  |-> alsa_output.usb-Focusrite_Scarlett_18i20:playback_AUX1
"""

    _UMIK_FEEDBACK = """\
avr_cal_sweep:monitor_FL
  |-> camilladsp_capture:input_3
  |-> loopback_ref:playback_1
alsa_input.usb-miniDSP_Umik-1_Gain:capture_FL
  |-> camilladsp_capture:input_1
"""

    _MISSING_LINKS = """\
some_other_node:output_1
  |-> some_dest:input
"""

    def _run(self, stdout: str, side_effect=None):
        from calibrate.measurement_service import _check_pw_link_l
        mock_result = MagicMock()
        mock_result.stdout = stdout
        if side_effect is not None:
            with patch("subprocess.run", side_effect=side_effect):
                return _check_pw_link_l()
        with patch("subprocess.run", return_value=mock_result):
            return _check_pw_link_l()

    def test_good_wiring_detected(self):
        result = self._run(self._GOOD_WIRING)
        assert result["error"] is None
        assert result["input3_linked"] is True
        assert result["loopback_ref_linked"] is True
        assert result["umik_into_dsp"] is False
        assert result["umik_sources"] == []

    def test_playback_links_counted(self):
        result = self._run(self._GOOD_WIRING)
        # 2 playback links in our sample
        assert result["playback_link_count"] == 2

    def test_umik_feedback_loop_detected(self):
        result = self._run(self._UMIK_FEEDBACK)
        assert result["umik_into_dsp"] is True
        assert len(result["umik_sources"]) == 1
        assert "umik" in result["umik_sources"][0].lower()

    def test_missing_links(self):
        result = self._run(self._MISSING_LINKS)
        assert result["input3_linked"] is False
        assert result["loopback_ref_linked"] is False
        assert result["umik_into_dsp"] is False

    def test_pw_link_not_found(self):
        result = self._run("", side_effect=FileNotFoundError("pw-link"))
        assert result["error"] == "pw-link not found"

    def test_pw_link_timeout(self):
        import subprocess
        result = self._run("", side_effect=subprocess.TimeoutExpired("pw-link", 5.0))
        assert "timed out" in result["error"]


# ── UMIK node parser tests ─────────────────────────────────────────────────────


class TestCheckPwDumpUmik:
    """Tests for _check_pw_dump_umik (pw-dump UMIK node extractor)."""

    def _make_pw_dump(
        self,
        node_name: str = "alsa_input.usb-miniDSP_Umik-1_Gain",
        media_class: str = "Audio/Source",
        resample_quality: int | None = 14,
        autoconnect: bool | None = False,
        state: str = "running",
    ) -> list:
        props: dict = {
            "node.name": node_name,
            "media.class": media_class,
        }
        if resample_quality is not None:
            props["resample.quality"] = resample_quality
        if autoconnect is not None:
            props["node.autoconnect"] = autoconnect
        return [
            {
                "info": {
                    "state": state,
                    "props": props,
                }
            }
        ]

    def _run(self, nodes: list, side_effect=None):
        from calibrate.measurement_service import _check_pw_dump_umik
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(nodes)
        if side_effect is not None:
            with patch("subprocess.run", side_effect=side_effect):
                return _check_pw_dump_umik()
        with patch("subprocess.run", return_value=mock_result):
            return _check_pw_dump_umik()

    def test_umik_present_healthy(self):
        nodes = self._make_pw_dump()
        result = self._run(nodes)
        assert result["error"] is None
        assert result["present"] is True
        assert result["node_state"] == "running"
        assert result["resample_quality"] == 14
        assert result["autoconnect"] is False

    def test_umik_absent(self):
        result = self._run([])
        assert result["error"] is None
        assert result["present"] is False
        assert result["node_state"] is None
        assert result["resample_quality"] is None

    def test_bad_resample_quality_flagged(self):
        """resample_quality != 14 is returned as-is (warning logic is in audio_stack_health)."""
        nodes = self._make_pw_dump(resample_quality=4)
        result = self._run(nodes)
        assert result["resample_quality"] == 4

    def test_autoconnect_true_returned(self):
        """autoconnect=True is returned as-is (warning logic is in audio_stack_health)."""
        nodes = self._make_pw_dump(autoconnect=True)
        result = self._run(nodes)
        assert result["autoconnect"] is True

    def test_umik_not_audio_source_skipped(self):
        """Nodes with wrong media.class are not matched as UMIK."""
        nodes = self._make_pw_dump(media_class="Audio/Sink")
        result = self._run(nodes)
        assert result["present"] is False

    def test_pw_dump_not_found(self):
        result = self._run([], side_effect=FileNotFoundError("pw-dump"))
        assert result["error"] == "pw-dump not found"

    def test_pw_dump_json_error(self):
        from calibrate.measurement_service import _check_pw_dump_umik
        mock_result = MagicMock()
        mock_result.stdout = "not valid json {"
        with patch("subprocess.run", return_value=mock_result):
            result = _check_pw_dump_umik()
        assert result["error"] is not None
        assert "JSON parse error" in result["error"]

    def test_pw_dump_timeout(self):
        import subprocess
        result = self._run([], side_effect=subprocess.TimeoutExpired("pw-dump", 10.0))
        assert "timed out" in result["error"]


# ── Service state checker tests ────────────────────────────────────────────────


class TestCheckServiceStates:
    """Tests for _check_service_states (systemctl wrapper)."""

    def _run(self, services: list[str], outputs: dict[str, str] | None = None, side_effect=None):
        from calibrate.measurement_service import _check_service_states

        def _fake_run(cmd, **kwargs):
            svc = cmd[-1]
            mock = MagicMock()
            mock.stdout = (outputs or {}).get(svc, "unknown") + "\n"
            return mock

        if side_effect is not None:
            with patch("subprocess.run", side_effect=side_effect):
                return _check_service_states(services)
        with patch("subprocess.run", side_effect=_fake_run):
            return _check_service_states(services)

    def test_active_services(self):
        result = self._run(
            ["camilladsp.service", "avr-measurement.service"],
            outputs={
                "camilladsp.service": "active",
                "avr-measurement.service": "active",
            },
        )
        assert result["camilladsp.service"] == "active"
        assert result["avr-measurement.service"] == "active"

    def test_inactive_service(self):
        result = self._run(
            ["camilladsp-watchdog.service"],
            outputs={"camilladsp-watchdog.service": "inactive"},
        )
        assert result["camilladsp-watchdog.service"] == "inactive"

    def test_subprocess_exception_returns_unknown(self):
        result = self._run(
            ["some.service"],
            side_effect=Exception("systemctl not found"),
        )
        assert result["some.service"] == "unknown"


# ── audio_stack_health endpoint tests ─────────────────────────────────────────


class TestAudioStackHealthEndpoint:
    """Tests for the /audio_stack_health FastAPI endpoint warning logic."""

    def _mock_helpers(
        self,
        camilla: dict | None = None,
        umik: dict | None = None,
        wiring: dict | None = None,
        services: dict | None = None,
    ):
        """Return a context-manager patch set for all four probe helpers."""
        default_camilla = {"error": None, "state": "Running", "cpu_load_percent": 5.0}
        default_umik = {
            "error": None,
            "present": True,
            "node_state": "running",
            "resample_quality": 14,
            "autoconnect": False,
        }
        default_wiring = {
            "error": None,
            "input3_linked": True,
            "loopback_ref_linked": True,
            "umik_into_dsp": False,
            "umik_sources": [],
            "playback_link_count": 20,
        }
        default_services = {
            "camilladsp.service": "active",
            "avr-measurement.service": "active",
            "camilladsp-watchdog.service": "active",
        }

        from unittest.mock import patch as _patch
        import calibrate.measurement_service as svc_mod

        patches = [
            _patch.object(svc_mod, "_check_camilladsp_state", return_value=camilla or default_camilla),
            _patch.object(svc_mod, "_check_pw_dump_umik", return_value=umik or default_umik),
            _patch.object(svc_mod, "_check_pw_link_l", return_value=wiring or default_wiring),
            _patch.object(svc_mod, "_check_service_states", return_value=services or default_services),
        ]
        return patches

    def _apply_and_call(self, patches):
        """Apply patches, call the endpoint, return parsed body."""
        from fastapi.testclient import TestClient
        from calibrate.measurement_service import app

        client = TestClient(app)
        with patches[0], patches[1], patches[2], patches[3]:
            resp = client.get("/audio_stack_health")
        assert resp.status_code == 200
        return resp.json()

    def test_all_healthy(self):
        patches = self._mock_helpers()
        body = self._apply_and_call(patches)
        assert body["ok"] is True
        assert body["healthy"] is True
        assert body["warnings"] == []

    def test_camilladsp_not_running(self):
        patches = self._mock_helpers(camilla={"error": None, "state": "Idle", "cpu_load_percent": None})
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("not Running" in w for w in body["warnings"])

    def test_camilladsp_error(self):
        patches = self._mock_helpers(camilla={"error": "connection refused", "state": None, "cpu_load_percent": None})
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("camilladsp probe error" in w for w in body["warnings"])

    def test_umik_not_present(self):
        patches = self._mock_helpers(umik={
            "error": None, "present": False, "node_state": None,
            "resample_quality": None, "autoconnect": None,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("UMIK microphone node not found" in w for w in body["warnings"])

    def test_umik_bad_resample_quality(self):
        patches = self._mock_helpers(umik={
            "error": None, "present": True, "node_state": "running",
            "resample_quality": 4, "autoconnect": False,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("resample.quality=4" in w for w in body["warnings"])

    def test_umik_autoconnect_true_warns(self):
        patches = self._mock_helpers(umik={
            "error": None, "present": True, "node_state": "running",
            "resample_quality": 14, "autoconnect": True,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("autoconnect=true" in w for w in body["warnings"])

    def test_missing_input3(self):
        patches = self._mock_helpers(wiring={
            "error": None,
            "input3_linked": False,
            "loopback_ref_linked": True,
            "umik_into_dsp": False,
            "umik_sources": [],
            "playback_link_count": 20,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("input_3" in w and "MISSING" in w for w in body["warnings"])

    def test_missing_loopback_ref(self):
        patches = self._mock_helpers(wiring={
            "error": None,
            "input3_linked": True,
            "loopback_ref_linked": False,
            "umik_into_dsp": False,
            "umik_sources": [],
            "playback_link_count": 20,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("loopback_ref" in w and "MISSING" in w for w in body["warnings"])

    def test_umik_feedback_loop_warning(self):
        patches = self._mock_helpers(wiring={
            "error": None,
            "input3_linked": True,
            "loopback_ref_linked": True,
            "umik_into_dsp": True,
            "umik_sources": ["alsa_input.usb-miniDSP_Umik-1"],
            "playback_link_count": 20,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("FEEDBACK LOOP HAZARD" in w for w in body["warnings"])

    def test_low_playback_link_count(self):
        patches = self._mock_helpers(wiring={
            "error": None,
            "input3_linked": True,
            "loopback_ref_linked": True,
            "umik_into_dsp": False,
            "umik_sources": [],
            "playback_link_count": 15,
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("15/20" in w for w in body["warnings"])

    def test_service_inactive_warns(self):
        patches = self._mock_helpers(services={
            "camilladsp.service": "active",
            "avr-measurement.service": "failed",
            "camilladsp-watchdog.service": "active",
        })
        body = self._apply_and_call(patches)
        assert body["healthy"] is False
        assert any("avr-measurement.service" in w and "failed" in w for w in body["warnings"])

    def test_wiring_probe_error(self):
        patches = self._mock_helpers(wiring={
            "error": "pw-link not found",
            "input3_linked": False,
            "loopback_ref_linked": False,
            "umik_into_dsp": False,
            "umik_sources": [],
            "playback_link_count": 0,
        })
        body = self._apply_and_call(patches)
        # When wiring has an error, we report the error warning, NOT the individual link warnings
        assert any("pw-link probe error" in w for w in body["warnings"])
