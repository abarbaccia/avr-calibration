"""Tests for calibrate.drivers.playback — USB and HDMI playback strategies.

Coverage diagram:
  playback_for_route()
  ├── [TESTED] "usb" → returns USBPlayback instance
  ├── [TESTED] "hdmi" → returns HDMIPlayback instance
  └── [TESTED] unknown route → defaults to USBPlayback

  USBPlayback.play_and_record()
  ├── [TESTED] happy path — calls PyTTa PlayRecMeasure, returns (sweep_1d, rec_1d)
  └── [TESTED] PortAudioError → re-raised as RuntimeError

  HDMIPlayback.play_and_record()
  └── [TESTED] happy path — opens explicit InputStream/OutputStream, returns arrays

  _verify_pw_record_binding()
  ├── [TESTED] binding confirmed → returns (True, "")
  ├── [TESTED] node absent from pw-link output → returns (False, reason)
  └── [TESTED] pw-link not found (FileNotFoundError) → skip, returns (True, "")

  LoopbackRefPlayback (pw-record path)
  ├── [TESTED] binding verified OK → ref captured normally
  ├── [TESTED] binding fails → ref_1d zeros, error logged
  └── [TESTED] identity check trips (ref≈mic) → ref_1d zeros, error in log
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch, call

import numpy as np
import pytest

from calibrate.drivers.playback import (
    HDMIAplayPlayback,  # backward-compat alias for HDMIPwCatPlayback
    HDMIPwCatPlayback,
    HDMIPlayback,
    LoopbackRefPlayback,
    USBPlayback,
    _verify_pw_record_binding,
    playback_for_route,
)


class TestPlaybackForRoute:
    def test_usb_route(self):
        assert isinstance(playback_for_route("usb"), USBPlayback)

    def test_hdmi_route(self):
        assert isinstance(playback_for_route("hdmi"), HDMIPlayback)

    def test_unknown_defaults_to_usb(self):
        assert isinstance(playback_for_route("toslink"), USBPlayback)


class TestUSBPlayback:
    def test_happy_path(self):
        """USBPlayback records via sd.InputStream callback, plays via sd.OutputStream."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        n_samples = 4800
        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))
        mock_sd.default.device = [0, 1]

        in_stream = MagicMock()
        out_stream = MagicMock()
        captured_callback = [None]

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            return in_stream

        mock_sd.InputStream = MagicMock(side_effect=fake_input_stream)
        mock_sd.OutputStream = MagicMock(return_value=out_stream)

        # Simulate mic data arriving when out_stream.write() plays the sweep.
        pre_s = int(USBPlayback.PRE_DELAY_S * 48000)
        post_s = int(USBPlayback.POST_DELAY_S * 48000)
        rec_n = pre_s + n_samples + post_s
        fake_mic = np.random.default_rng(99).standard_normal((rec_n, 1)).astype(np.float32)

        def fake_write(buf):
            if captured_callback[0]:
                captured_callback[0](fake_mic, rec_n, None, None)

        out_stream.write = MagicMock(side_effect=fake_write)

        strategy = USBPlayback()
        with patch("time.sleep"):
            sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_sd.InputStream.assert_called_once()
        mock_sd.OutputStream.assert_called_once()
        in_stream.start.assert_called_once()
        out_stream.start.assert_called_once()
        out_stream.write.assert_called_once()

        assert sweep_1d.shape == (n_samples,)
        assert len(rec_1d) > 0
        assert rec_1d.dtype == np.float64
        np.testing.assert_array_equal(sweep_1d, sweep.timeSignal[:, 0])

    def test_portaudio_error_raises_runtime_error(self):
        """Any audio device error from stream operations is re-raised as RuntimeError."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        sweep = MagicMock()
        sweep.timeSignal = np.zeros((4800, 1))
        mock_sd.default.device = [0, 1]

        in_stream = MagicMock()
        in_stream.start.side_effect = Exception("no device")
        mock_sd.InputStream = MagicMock(return_value=in_stream)
        mock_sd.OutputStream = MagicMock(return_value=MagicMock())

        strategy = USBPlayback()
        with patch("time.sleep"), pytest.raises(RuntimeError, match="Audio device error"):
            strategy.play_and_record(sweep, 48000, 1, 1)


class TestHDMIPlayback:
    def test_happy_path(self):
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()

        n_samples = 4800
        sweep = MagicMock()
        sweep.timeSignal = np.random.default_rng(42).standard_normal((n_samples, 1))

        # Mock the default device tuple
        mock_sd.default.device = [0, 1]

        # Capture the InputStream callback so we can feed it fake mic data
        in_stream = MagicMock()
        out_stream = MagicMock()
        captured_callback = [None]

        def fake_input_stream(**kwargs):
            captured_callback[0] = kwargs.get("callback")
            return in_stream

        def fake_output_stream(**kwargs):
            return out_stream

        mock_sd.InputStream = MagicMock(side_effect=fake_input_stream)
        mock_sd.OutputStream = MagicMock(side_effect=fake_output_stream)

        # Simulate the OutputStream.write triggering the callback with mic data
        fake_mic = np.random.default_rng(99).standard_normal((n_samples, 1)).astype(np.float32)

        def fake_write(buf):
            # Feed fake mic data via the captured callback
            if captured_callback[0]:
                captured_callback[0](fake_mic, n_samples, None, None)

        out_stream.write = MagicMock(side_effect=fake_write)

        strategy = HDMIPlayback()
        sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        mock_sd.InputStream.assert_called_once()
        mock_sd.OutputStream.assert_called_once()
        in_stream.start.assert_called_once()
        out_stream.start.assert_called_once()
        out_stream.write.assert_called_once()

        assert sweep_1d.shape == (n_samples,)
        assert rec_1d.shape == (n_samples,)
        assert rec_1d.dtype == np.float64

        # Verify OutputStream was opened with int16 dtype
        out_kwargs = mock_sd.OutputStream.call_args[1]
        assert out_kwargs["dtype"] == "int16"

        # Verify the write buffer is int16
        write_args = out_stream.write.call_args[0]
        assert write_args[0].dtype == np.int16


_PW_NODE = "alsa_output.platform-107c701400.hdmi.hdmi-stereo"


class TestHDMIPwCatPlayback:
    """Native PipeWire HDMI playback via pw-cat subprocess."""

    def _make_sweep(self, n_samples: int = 4800) -> MagicMock:
        sweep_data = np.linspace(-0.5, 0.5, n_samples, dtype=np.float32).reshape(-1, 1)
        sweep = MagicMock()
        sweep.timeSignal = sweep_data
        return sweep

    def _patch_sd(self):
        """Stub out sd.InputStream so callbacks don't actually run."""
        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        in_stream = MagicMock()
        mock_sd.InputStream = MagicMock(return_value=in_stream)
        mock_sd.default.device = (1, 0)
        return mock_sd, in_stream

    def test_factory_returns_pwcat_when_node_provided(self):
        s = playback_for_route("hdmi", hdmi_pipewire_node=_PW_NODE, hdmi_channels=6)
        assert isinstance(s, HDMIPwCatPlayback)
        assert s.pipewire_node == _PW_NODE
        assert s.channels == 6

    def test_factory_compat_alias_still_works(self):
        # HDMIAplayPlayback is an alias; isinstance checks both names.
        s = playback_for_route("hdmi", hdmi_alsa_device=_PW_NODE, hdmi_channels=6)
        assert isinstance(s, HDMIPwCatPlayback)
        assert isinstance(s, HDMIAplayPlayback)

    def test_factory_returns_legacy_when_no_node(self):
        # Backward-compat: callers that pass neither node nor device get HDMIPlayback.
        assert isinstance(playback_for_route("hdmi"), HDMIPlayback)

    def test_constructor_validates_node(self):
        with pytest.raises(ValueError, match="non-empty"):
            HDMIPwCatPlayback(pipewire_node="")
        with pytest.raises(ValueError, match="channels"):
            HDMIPwCatPlayback(pipewire_node=_PW_NODE, channels=0)

    def test_play_and_record_invokes_pwcat_with_correct_args(self):
        """pw-cat is called with --playback --target <node> --channels --rate --format s16."""
        mock_sd, in_stream = self._patch_sd()
        sweep = self._make_sweep(n_samples=4800)

        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = MagicMock(return_value=(b"", b""))
        proc.poll = MagicMock(return_value=0)

        strategy = HDMIPwCatPlayback(pipewire_node=_PW_NODE, channels=6)
        with (
            patch("subprocess.Popen", return_value=proc) as MockPopen,
            patch("time.sleep"),
        ):
            sweep_1d, rec_1d = strategy.play_and_record(sweep, 48000, 1, 1)

        # pw-cat command shape
        cmd = MockPopen.call_args[0][0]
        assert cmd[0] == "pw-cat"
        assert "--playback" in cmd
        assert "--target" in cmd
        assert cmd[cmd.index("--target") + 1] == _PW_NODE
        assert "--channels" in cmd
        assert cmd[cmd.index("--channels") + 1] == "6"
        assert "--rate" in cmd
        assert cmd[cmd.index("--rate") + 1] == "48000"
        assert "--format" in cmd
        assert cmd[cmd.index("--format") + 1] == "s16"
        assert cmd[-1] == "-"  # stdin

        # Three Popen calls: stale-stream cleanup (pw-cli ls Node), warmup burst, sweep.
        assert MockPopen.call_count == 3
        sweep_kwargs = proc.communicate.call_args.kwargs
        assert "input" in sweep_kwargs
        pcm_bytes = sweep_kwargs["input"]
        # 4800 sweep frames × 6 channels × 2 bytes/sample (no preroll in sweep buffer)
        assert len(pcm_bytes) == 4800 * 6 * 2

        # Capture path still ran via PortAudio
        mock_sd.InputStream.assert_called_once()
        in_stream.start.assert_called_once()

        assert sweep_1d.shape == (4800,)
        assert rec_1d.dtype == np.float64

    def test_multichannel_interleave_only_target_channel_has_signal(self):
        """sweep on channel 3 of 6 puts non-zero only on channel 3, zeros elsewhere."""
        mock_sd, in_stream = self._patch_sd()
        sweep = self._make_sweep(n_samples=480)

        captured: dict = {}
        def _fake_communicate(input=None, timeout=None):
            captured["input"] = input
            return (b"", b"")
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = MagicMock(side_effect=_fake_communicate)
        proc.poll = MagicMock(return_value=0)

        strategy = HDMIPwCatPlayback(pipewire_node=_PW_NODE, channels=6)
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("time.sleep"),
        ):
            strategy.play_and_record(sweep, 48000, 1, 3)

        pcm = np.frombuffer(captured["input"], dtype=np.int16).reshape(-1, 6)
        # Sweep buffer has no preroll — channel 3 (1-based) → column index 2
        # has signal across the full buffer; all others are zero.
        for ch in range(6):
            col = pcm[:, ch]
            if ch == 2:
                assert np.any(col != 0), "target channel must have signal"
            else:
                assert np.all(col == 0), f"channel {ch} must be silent (got {col[col != 0][:4]})"

    def test_pwcat_nonzero_returncode_raises(self):
        mock_sd, _ = self._patch_sd()
        sweep = self._make_sweep(n_samples=480)

        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = MagicMock(return_value=(b"", b"pw-cat: no such node"))
        proc.poll = MagicMock(return_value=1)

        strategy = HDMIPwCatPlayback(pipewire_node=_PW_NODE, channels=2)
        with (
            patch("subprocess.Popen", return_value=proc),
            patch("time.sleep"),
            pytest.raises(RuntimeError, match="pw-cat.*no such node"),
        ):
            strategy.play_and_record(sweep, 48000, 1, 1)

    def test_pw_record_capture_used_when_node_configured(self):
        """When capture_pipewire_node is set, pw-record subprocess is used instead of sd.InputStream."""
        import struct

        mock_sd = sys.modules["sounddevice"]
        mock_sd.reset_mock()
        # sd.InputStream must NOT be called when using pw-record capture
        mock_sd.InputStream = MagicMock()

        n_samples = 480
        sweep = self._make_sweep(n_samples=n_samples)

        # Fake pw-record stdout: f32le silence (same length as sweep + pre/post)
        sr = 48000
        pre_s = int(HDMIPwCatPlayback.PRE_DELAY_S * sr)
        post_s = int(HDMIPwCatPlayback.POST_DELAY_S * sr)
        fake_audio = struct.pack(f"{pre_s + n_samples + post_s}f", *([0.0] * (pre_s + n_samples + post_s)))

        warmup_proc = MagicMock()
        warmup_proc.returncode = 0
        warmup_proc.communicate = MagicMock(return_value=(b"", b""))
        warmup_proc.poll = MagicMock(return_value=0)

        sweep_proc = MagicMock()
        sweep_proc.returncode = 0
        sweep_proc.communicate = MagicMock(return_value=(b"", b""))
        sweep_proc.poll = MagicMock(return_value=0)

        rec_proc = MagicMock()
        rec_proc.returncode = 0
        rec_proc.stdout = MagicMock()
        rec_proc.stdout.read = MagicMock(side_effect=[fake_audio, b""])
        rec_proc.poll = MagicMock(return_value=0)

        _UMIK_NODE = "alsa_input.usb-miniDSP_Umik-1"
        call_count = [0]

        def _popen(cmd, **kwargs):
            call_count[0] += 1
            if cmd[0] == "pw-record":
                return rec_proc
            return warmup_proc if call_count[0] == 2 else sweep_proc

        strategy = HDMIPwCatPlayback(
            pipewire_node=_PW_NODE, channels=6,
            capture_pipewire_node=_UMIK_NODE,
        )
        with (
            patch("subprocess.Popen", side_effect=_popen),
            patch("time.sleep"),
        ):
            sweep_1d, rec_1d = strategy.play_and_record(sweep, sr, 1, 1)

        # sd.InputStream must NOT be called — pw-record handles capture
        mock_sd.InputStream.assert_not_called()

        assert sweep_1d.shape == (n_samples,)
        assert rec_1d.dtype == np.float64


# ── _verify_pw_record_binding ────────────────────────────────────────────────

class TestVerifyPwRecordBinding:
    """Unit tests for the pw-record source-binding verifier."""

    def test_confirmed_binding_returns_true(self):
        """pw-link output shows the expected node as a source → (True, '')."""
        pw_output = (
            "alsa_input.usb-miniDSP_loopback_ref:capture_FL\n"
            "  |-> pw-record-stream:input_1\n"
        )
        with patch(
            "subprocess.run",
            return_value=MagicMock(stdout=pw_output, returncode=0),
        ):
            ok, reason = _verify_pw_record_binding(
                "alsa_input.usb-miniDSP_loopback_ref", timeout_s=0.01, poll_interval_s=0.005
            )
        assert ok is True
        assert reason == ""

    def test_wrong_binding_returns_false_with_reason(self):
        """pw-link shows a different source (UMIK, not loopback_ref) → (False, reason)."""
        pw_output = (
            "alsa_input.usb-miniDSP_Umik-1:capture_FL\n"
            "  |-> pw-record-stream:input_1\n"
        )
        with patch(
            "subprocess.run",
            return_value=MagicMock(stdout=pw_output, returncode=0),
        ):
            ok, reason = _verify_pw_record_binding(
                "loopback_ref", timeout_s=0.05, poll_interval_s=0.01
            )
        assert ok is False
        assert "loopback_ref" in reason
        assert "fell back" in reason.lower() or "fallback" in reason.lower() or "wrong source" in reason.lower()

    def test_pw_link_not_found_skips_verification(self):
        """If pw-link binary is absent, verification is skipped → (True, '')."""
        with patch("subprocess.run", side_effect=FileNotFoundError("pw-link not found")):
            ok, reason = _verify_pw_record_binding(
                "loopback_ref", timeout_s=0.01, poll_interval_s=0.005
            )
        assert ok is True
        assert reason == ""


def test_start_pw_record_multi_source_links_each_source_in_order():
    """_start_pw_record_multi_source starts pw-record with autoconnect OFF and
    pw-links each source port to the recorder's input ports IN ORDER, so channel
    k captures source_ports[k]. This is the R11 fix for `--target`'s
    duplicate-one-channel behavior on a multi-ch sink monitor."""
    import subprocess as _sp
    from calibrate.drivers.playback import _start_pw_record_multi_source

    fake_proc = MagicMock()
    fake_proc.stdout.read.return_value = b""  # reader thread exits immediately
    link_calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        r = MagicMock()
        if cmd[:2] == ["pw-link", "-i"]:
            r.stdout = "cal_caprec_2ch:input_FL\ncal_caprec_2ch:input_FR\n"
        else:
            link_calls.append(cmd)
            r.stdout = ""
        return r

    with (
        patch.object(_sp, "Popen", return_value=fake_proc) as popen,
        patch.object(_sp, "run", side_effect=fake_run),
    ):
        proc, chunks, t = _start_pw_record_multi_source(
            ["loopback_ref:monitor_FL", "loopback_ref:monitor_FR"], 48000,
        )
        t.join(timeout=2.0)

    popen_cmd = popen.call_args[0][0]
    assert popen_cmd[0] == "pw-record"
    assert "node.autoconnect=false" in popen_cmd
    assert "2" in popen_cmd  # --channels 2
    # Each source linked to the matching input port, in order.
    assert ["pw-link", "loopback_ref:monitor_FL", "cal_caprec_2ch:input_FL"] in link_calls
    assert ["pw-link", "loopback_ref:monitor_FR", "cal_caprec_2ch:input_FR"] in link_calls


# ── LoopbackRefPlayback pw-record binding + identity checks ──────────────────

class TestLoopbackRefPlaybackBinding:
    """Integration tests for the binding verification and identity check in
    LoopbackRefPlayback.play_and_record (pw-record path)."""

    def _make_base(self, mic_signal: np.ndarray) -> MagicMock:
        """Return a fake base strategy that gives *mic_signal* as the recording."""
        base = MagicMock()
        sweep_1d = np.linspace(-0.5, 0.5, len(mic_signal))
        base.play_and_record = MagicMock(return_value=(sweep_1d, mic_signal))
        base.skip_warmup = True  # suppress warmup trimming
        return base

    def _ref_raw_bytes(self, ref_audio: np.ndarray) -> bytes:
        """Encode *ref_audio* as f32le bytes (as pw-record would produce)."""
        return ref_audio.astype(np.float32).tobytes()

    def test_verified_binding_captures_ref_normally(self):
        """When pw-link confirms the binding, ref_1d is non-zero and distinct."""
        n = 4800
        mic = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.1
        # ref is a different signal (loopback pre-DSP — ~10 dB louder)
        ref = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.3

        base = self._make_base(mic)
        raw = self._ref_raw_bytes(ref)
        chunks: list[bytes] = [raw]

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=1,
            )
            sweep_out, mic_out, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.any(ref_out != 0), "ref_1d must be non-zero when binding is confirmed"

    def test_mic_capture_starts_concurrently_with_binding_verify(self):
        """R7: the base (mic) capture MUST start concurrently with ref binding
        verification, not after it.

        The old order was: start ref pw-record → block on
        _verify_pw_record_binding (100s of ms, variable) → start mic capture.
        That variable gap injected a large, run-to-run-variable ref/mic timebase
        offset (observed avr_processing_ms 650–858 ms) that shoved the IR around
        the analysis gate and collapsed the coherence proxy differently every
        run. The base's PRE_DELAY gives the verify ample time to finish before the
        sweep plays, so the verify must run *concurrently* with the base, not
        gate it. Regression guard for docs/pipewire-architecture.md R7.
        """
        import time as _t
        n = 4800
        mic = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.1
        ref = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.3
        chunks: list[bytes] = [self._ref_raw_bytes(ref)]

        verify_end: list[float] = []
        base_started: list[float] = []

        def _slow_verify(*a, **k):
            _t.sleep(0.2)
            verify_end.append(_t.monotonic())
            return (True, "")

        base = MagicMock()
        sweep_1d = np.linspace(-0.5, 0.5, n)

        def _base_play(*a, **k):
            base_started.append(_t.monotonic())
            return (sweep_1d, mic)

        base.play_and_record = MagicMock(side_effect=_base_play)
        base.skip_warmup = True

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", side_effect=_slow_verify),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=1,
            )
            strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert base_started and verify_end, "both base and verify must have run"
        assert base_started[0] < verify_end[0], (
            "mic capture (base) must start BEFORE binding verification completes; "
            f"it started {base_started[0] - verify_end[0]:.3f}s relative to verify end "
            "(positive = started after verify, the R7 bug)"
        )

    def test_r11_single_2ch_capture_splits_ref_and_mic(self):
        """R11: with a 2-ch loopback capture, ref=ch0 and mic=ch1 come from ONE
        sample-locked recording; the base strategy's separately-captured mic is
        ignored. This is the structural fix for the inter-stream timebase offset
        (docs/pipewire-architecture.md §6b)."""
        n = 4800
        ref_ch0 = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.30   # stimulus ref
        mic_ch1 = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.07   # room mic (distinct level)
        base_mic = np.full(n, 0.999, dtype=np.float64)                   # WRONG mic — must be ignored
        interleaved = np.stack([ref_ch0, mic_ch1], axis=1).astype(np.float32).tobytes()
        chunks: list[bytes] = [interleaved]

        base = MagicMock()
        sweep_1d = np.linspace(-0.5, 0.5, n)
        base.play_and_record = MagicMock(return_value=(sweep_1d, base_mic))
        base.skip_warmup = True

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record_multi_source",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=2,
            )
            sweep_out, mic_out, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.allclose(ref_out[:200], ref_ch0[:200], atol=1e-4), "ref must be loopback ch0"
        assert np.allclose(mic_out[:200], mic_ch1[:200], atol=1e-4), (
            "mic must be loopback ch1 (sample-locked with ref), not the base strategy's mic"
        )
        assert not np.allclose(mic_out[:200], base_mic[:200]), (
            "base strategy mic must be IGNORED in R11 2-ch mode"
        )

    def test_r11_2ch_raw_too_short_zeroes_ref(self):
        """R11 error branch: if the 2-ch recording has too few bytes to decode,
        the ref is zeroed (deconvolution falls back to the analytical template)."""
        n = 4800
        base_mic = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.1
        chunks: list[bytes] = [b"\x00\x00\x00\x00"]  # one float32 < 2ch*4 bytes

        base = MagicMock()
        base.play_and_record = MagicMock(return_value=(np.linspace(-0.5, 0.5, n), base_mic))
        base.skip_warmup = True

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record_multi_source",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base, ref_pipewire_node="loopback_ref", ref_pw_channels=2,
            )
            _sweep, _mic, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.all(ref_out == 0), "ref must be zeroed when 2-ch raw is too short"

    def test_r11_2ch_parse_exception_zeroes_ref(self):
        """R11 error branch: a malformed 2-ch buffer (size not divisible by 2)
        raises in reshape; it must be caught and the ref zeroed, not propagated."""
        n = 4800
        base_mic = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.1
        # 3 float32 samples → passes the >=2ch*4 byte guard but reshape(-1,2) fails.
        chunks: list[bytes] = [np.zeros(3, dtype=np.float32).tobytes()]

        base = MagicMock()
        base.play_and_record = MagicMock(return_value=(np.linspace(-0.5, 0.5, n), base_mic))
        base.skip_warmup = True

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record_multi_source",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base, ref_pipewire_node="loopback_ref", ref_pw_channels=2,
            )
            _sweep, _mic, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.all(ref_out == 0), "ref must be zeroed when 2-ch buffer is malformed"

    def test_wrong_binding_zeroes_ref(self):
        """When pw-link reports wrong binding, ref_1d is zeros."""
        n = 4800
        mic = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.1
        ref = np.sin(2 * np.pi * 50 * np.arange(n) / 48000) * 0.3
        raw = self._ref_raw_bytes(ref)
        chunks: list[bytes] = [raw]

        base = self._make_base(mic)

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch(
                "calibrate.drivers.playback._verify_pw_record_binding",
                return_value=(
                    False,
                    "pw-record bound to wrong source — PipeWire fell back to default source",
                ),
            ),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=1,
            )
            sweep_out, mic_out, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.all(ref_out == 0), "ref_1d must be all-zeros when binding check fails"
        # R7: the base now runs CONCURRENTLY with a failing binding verify, so the
        # sweep/mic result must still be valid (only the ref is sacrificed) —
        # deconvolution falls back to the analytical sweep template.
        assert sweep_out is not None and mic_out is not None
        assert np.any(mic_out != 0), "mic recording must survive a binding-verify failure"

    def test_identity_check_zeroes_ref_when_ref_matches_mic(self):
        """When ref and mic peak+rms are within 0.5 dB, ref_1d is zeroed (same source)."""
        n = 48000
        # Same signal for both mic and ref (as happens when pw-record captures UMIK)
        identical = np.sin(2 * np.pi * 40 * np.arange(n) / 48000) * 0.05

        base = self._make_base(identical.copy())
        # Inject the same signal as ref
        raw = self._ref_raw_bytes(identical)
        chunks: list[bytes] = [raw]

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=1,
            )
            sweep_out, mic_out, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.all(ref_out == 0), (
            "ref_1d must be zeroed when ref and mic are statistically identical"
        )

    def test_identity_check_passes_when_signals_differ(self):
        """When ref and mic differ by more than 0.5 dB, ref is accepted as valid."""
        n = 48000
        mic = np.sin(2 * np.pi * 40 * np.arange(n) / 48000) * 0.05
        # ref is ~6 dB louder — clearly different physical signal
        ref = np.sin(2 * np.pi * 40 * np.arange(n) / 48000) * 0.10

        base = self._make_base(mic.copy())
        raw = self._ref_raw_bytes(ref)
        chunks: list[bytes] = [raw]

        with (
            patch(
                "calibrate.drivers.playback._start_pw_record",
                return_value=(MagicMock(), chunks, MagicMock()),
            ),
            patch("calibrate.drivers.playback._stop_pw_record"),
            patch("calibrate.drivers.playback._verify_pw_record_binding", return_value=(True, "")),
        ):
            strategy = LoopbackRefPlayback(
                base=base,
                ref_pipewire_node="loopback_ref",
                ref_pw_channels=1,
            )
            sweep_out, mic_out, ref_out = strategy.play_and_record(MagicMock(), 48000, 1, 1)

        assert np.any(ref_out != 0), (
            "ref_1d must be non-zero when ref and mic differ sufficiently"
        )
