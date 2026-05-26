"""Coefficient-match tests: simulate_eq vs apply_input_eq (CamillaDSP).

The empirical bug was that designed input-PEQ cut depths landed 2-3× deeper at
the listener than ``simulate_eq`` predicted. The symptom is mostly explained
by measurement-to-measurement variance (4 dB IR-peak drift between supposedly
identical "HPF-only" baselines, polarity flip across the session), but the
specific failure mode the user described — sim says -5 dB, hardware delivers
-14 dB — would also fall out of a coefficient mismatch between the simulator
(calibrate.dsp / mcp_server._biquad_response) and what the CamillaDSP daemon
actually executes.

These tests prove the observable surfaces agree to floating-point precision
and pin the single-application invariant that, if violated, would manifest
exactly as the 2-3× over-cut the user observed.

Surfaces under test:
  A. Simulator path — ``calibrate.mcp_server._biquad_response`` (z-domain
     evaluation of dsp.py's RBJ-cookbook coefficients).
  B. SafetyValidator path — ``calibrate.safety._filter_magnitude_db`` (scipy
     freqz of the same coefficients).
  C. Driver path — ``CamillaDSPDriver._filter_block`` emits {type, freq, q,
     gain} that CamillaDSP's ``Biquad::Peaking`` evaluates with the SAME RBJ
     formulas. We verify the YAML the driver writes matches the spec we
     simulated, byte-for-byte.
  D. Single-application — input PEQ writes to all logical inputs, but the
     routing mixer must deliver each output's signal from at most ONE input.

A regression on ANY of these would re-open the simulate-vs-apply gap.
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

from calibrate.drivers.camilladsp import CamillaDSPDriver
from calibrate.dsp import freq_gain_q_to_biquad
from calibrate.safety import FilterSpec, _filter_magnitude_db


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stub_client(driver: CamillaDSPDriver) -> AsyncMock:
    driver._client._ws = object()
    driver._client.call = AsyncMock(return_value=None)
    return driver._client.call


def _last_pushed_config(call_mock: AsyncMock) -> dict:
    import yaml as _yaml
    for mock_call in reversed(call_mock.await_args_list):
        if mock_call.args and mock_call.args[0] == "SetConfig":
            return _yaml.safe_load(mock_call.args[1])
    raise AssertionError("no SetConfig call was recorded")


def _rbj_response_db(freq: float, ftype: str, fc: float, gain_db: float,
                     q: float, sample_rate: int | None = None) -> float:
    """Analytical RBJ-cookbook biquad magnitude in dB at *freq* Hz.

    Independent of mcp_server._biquad_response so a regression there is
    visible. Matches _biquad_response's sample-rate fallback (SAMPLE_RATE_HZ
    when no driver is bound, which is the test default).
    """
    import cmath
    from calibrate.dsp import SAMPLE_RATE_HZ
    if sample_rate is None:
        sample_rate = SAMPLE_RATE_HZ
    bq = freq_gain_q_to_biquad(
        freq=fc, gain_db=gain_db, q=q, filter_type=ftype, sample_rate=sample_rate
    )
    z = cmath.exp(1j * 2.0 * math.pi * freq / sample_rate)
    zi = 1.0 / z
    num = bq["b0"] + bq["b1"] * zi + bq["b2"] * zi * zi
    den = 1.0 + bq["a1"] * zi + bq["a2"] * zi * zi
    return 20.0 * math.log10(abs(num / den))


# ── A. Simulator matches analytical biquad ────────────────────────────────────

@pytest.mark.parametrize(
    "freq_hz,fc,gain_db,q",
    [
        (50.0, 50.0, -6.0, 3.0),           # at f0
        (40.0, 50.0, -6.0, 3.0),           # below f0
        (63.0, 50.0, -6.0, 3.0),           # above f0
        (31.5, 31.5, -8.0, 2.0),
        (80.0, 80.0, -4.0, 4.0),
        (40.0, 40.0, -7.0, 3.0),           # bug-repro filter
        (50.0, 50.0, -5.0, 2.0),           # bug-repro filter
        (25.0, 25.0, -3.0, 3.0),
        (100.0, 50.0, -6.0, 3.0),
        (20.0, 50.0, -6.0, 3.0),
    ],
)
def test_simulate_eq_biquad_matches_analytical(
    freq_hz: float, fc: float, gain_db: float, q: float
) -> None:
    """_biquad_response = analytical RBJ z-domain evaluation (within 1e-9 dB)."""
    from calibrate.mcp_server import _biquad_response
    expected = _rbj_response_db(freq_hz, "peaking", fc, gain_db, q)
    actual = _biquad_response(freq_hz, "peaking", fc, gain_db, q)
    assert abs(actual - expected) < 1e-9, (
        f"simulate_eq biquad diverges from analytical RBJ at {freq_hz} Hz "
        f"(peaking {fc} Hz {gain_db} dB Q{q}): {actual} vs {expected}"
    )


def test_simulate_eq_full_chain_matches_bug_repro_filter_set() -> None:
    """The 5-filter bug-repro set predicts a specific dB cut at each 1/3-oct
    center. The empirically observed cuts at 40/50/63/80 Hz were 2-3× deeper
    than these values — that gap is the symptom under investigation.

    Computed from the analytical RBJ z-domain biquad evaluation, NOT pulled
    from the simulator under test, so a regression in _biquad_response will
    show as a divergence from this analytical sum.
    """
    from calibrate.mcp_server import _biquad_response

    filters = [
        ("peaking", 25.0, -3.0, 3.0),
        ("peaking", 31.0, -5.5, 3.0),
        ("peaking", 40.0, -7.0, 3.0),
        ("peaking", 50.0, -5.0, 2.0),
        ("peaking", 80.0, -3.5, 3.0),
    ]
    for f in (25.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0):
        analytical = sum(
            _rbj_response_db(f, t, fc, g, q) for (t, fc, g, q) in filters
        )
        simulated = sum(
            _biquad_response(f, t, fc, g, q) for (t, fc, g, q) in filters
        )
        assert abs(simulated - analytical) < 1e-6, (
            f"combined response at {f} Hz: simulator {simulated:.4f} dB "
            f"diverges from analytical {analytical:.4f} dB"
        )
        # Sanity: at the cut centers the combined response is within the
        # filter design envelope. A +10 dB excess (the bug) shows as roughly
        # doubled magnitude relative to the design.
        if 25 <= f <= 80:
            assert -15.0 < simulated < 0.0, (
                f"combined response at {f} Hz outside sane range: {simulated:.2f} dB "
                f"(design total -24 dB; max stacked overlap ≈ -12 dB)"
            )


# ── A2. Shelf filter simulator matches analytical biquad ──────────────────────

@pytest.mark.parametrize(
    "ftype,freq_hz,fc,gain_db,q",
    [
        # low_shelf — Harman bass shelf cases applied by apply_input_eq
        ("low_shelf", 20.0,  38.0, 4.5, 0.7),   # well below shelf corner
        ("low_shelf", 38.0,  38.0, 4.5, 0.7),   # at shelf corner (-3 dB point)
        ("low_shelf", 80.0,  38.0, 4.5, 0.7),   # above shelf corner (gain→0)
        ("low_shelf", 20.0,  68.0, 2.0, 0.7),   # second Harman shelf
        ("low_shelf", 68.0,  68.0, 2.0, 0.7),
        ("low_shelf", 120.0, 68.0, 2.0, 0.7),
        # high_shelf — cuts AND boosts above a corner frequency
        ("high_shelf", 200.0, 100.0, -3.0, 0.7),  # cut: above corner
        ("high_shelf", 100.0, 100.0, -3.0, 0.7),  # cut: at corner
        ("high_shelf", 50.0,  100.0, -3.0, 0.7),  # cut: below corner
        ("high_shelf", 200.0, 100.0,  3.0, 0.7),  # boost: above corner
        ("high_shelf", 100.0, 100.0,  3.0, 0.7),  # boost: at corner
    ],
)
def test_simulate_eq_shelf_matches_analytical(
    ftype: str, freq_hz: float, fc: float, gain_db: float, q: float
) -> None:
    """_biquad_response must agree with the RBJ analytical formula for shelf
    filters — the same tolerance as peaking (1e-9 dB).  A regression here
    would make simulate_eq mis-predict Harman target-curve shaping, causing
    the LLM to over- or under-apply shelf depth before calling apply_input_eq.
    """
    from calibrate.mcp_server import _biquad_response
    expected = _rbj_response_db(freq_hz, ftype, fc, gain_db, q)
    actual = _biquad_response(freq_hz, ftype, fc, gain_db, q)
    assert abs(actual - expected) < 1e-9, (
        f"simulate_eq {ftype} diverges from analytical RBJ at {freq_hz} Hz "
        f"({fc} Hz {gain_db} dB Q{q}): {actual} vs {expected}"
    )


# ── B. SafetyValidator agrees with simulator ──────────────────────────────────

@pytest.mark.parametrize(
    "fc,gain_db,q",
    [
        (50.0, -6.0, 3.0),
        (40.0, -7.0, 3.0),
        (80.0, -3.5, 3.0),
        (31.5, 4.0, 2.0),       # boost case
    ],
)
def test_safety_magnitude_matches_simulator(fc: float, gain_db: float, q: float) -> None:
    """SafetyValidator._filter_magnitude_db must match the simulator within
    0.01 dB across 20-200 Hz. Different code paths (scipy.freqz vs hand-rolled
    cmath) computing the same biquad — divergence means the validator decides
    admissibility on one set of numbers while the LLM reasons about another.
    """
    from calibrate.mcp_server import _biquad_response
    spec = FilterSpec(freq=fc, gain_db=gain_db, q=q, type="peaking")
    for f in (20.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0):
        sim_db = _biquad_response(f, "peaking", fc, gain_db, q)
        safety_db = _filter_magnitude_db(spec, f)
        assert abs(sim_db - safety_db) < 0.01, (
            f"simulator/safety divergence at {f} Hz for peaking "
            f"{fc}/{gain_db}/{q}: sim={sim_db:.4f} safety={safety_db:.4f}"
        )


@pytest.mark.parametrize(
    "ftype,fc,gain_db,q",
    [
        ("low_shelf",  38.0, 4.5, 0.7),    # Harman bass shelf 1
        ("low_shelf",  68.0, 2.0, 0.7),    # Harman bass shelf 2
        ("high_shelf", 100.0, -3.0, 0.7),  # high-shelf cut
        ("low_shelf",  50.0, -4.0, 1.0),   # low-shelf cut
    ],
)
def test_safety_magnitude_matches_simulator_shelf(
    ftype: str, fc: float, gain_db: float, q: float
) -> None:
    """SafetyValidator and simulator must agree for shelf filters too.

    The apply_input_eq Harman target path uses low_shelf filters — if the
    validator approves on one set of numbers while the simulator shows another,
    the safety gate is checking a different curve than what was simulated.
    """
    from calibrate.mcp_server import _biquad_response
    spec = FilterSpec(freq=fc, gain_db=gain_db, q=q, type=ftype)
    for f in (20.0, 31.5, 40.0, 50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0):
        sim_db = _biquad_response(f, ftype, fc, gain_db, q)
        safety_db = _filter_magnitude_db(spec, f)
        assert abs(sim_db - safety_db) < 0.01, (
            f"simulator/safety divergence at {f} Hz for {ftype} "
            f"{fc}/{gain_db}/{q}: sim={sim_db:.4f} safety={safety_db:.4f}"
        )


# ── C. Driver emits the spec we simulated ─────────────────────────────────────

@pytest.mark.asyncio
async def test_camilladsp_filter_block_matches_simulated_spec() -> None:
    """CamillaDSP receives the same {freq, q, gain} the simulator used.

    CamillaDSP's Biquad::Peaking is RBJ cookbook — same formulas as dsp.py
    _peaking. By verifying the freq/q/gain trio is forwarded byte-for-byte we
    prove the daemon evaluates the exact filter the simulator predicted.
    """
    driver = CamillaDSPDriver(input_channels=2, output_channels=4)
    call = _stub_client(driver)

    designed = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 25.0, "gain_db": -3.0, "q": 3.0, "type": "peaking"},
        {"freq": 31.0, "gain_db": -5.5, "q": 3.0, "type": "peaking"},
        {"freq": 40.0, "gain_db": -7.0, "q": 3.0, "type": "peaking"},
        {"freq": 50.0, "gain_db": -5.0, "q": 2.0, "type": "peaking"},
        {"freq": 80.0, "gain_db": -3.5, "q": 3.0, "type": "peaking"},
    ]
    # bypass_iteration_limit so the test set lands in one call without
    # tripping the per-iteration delta safety guard.
    await driver.apply_input_eq(
        preset=0, filters=designed, input_index=0,
        simulation_verified=True, bypass_iteration_limit=True,
    )

    cfg = _last_pushed_config(call)
    # HPF lives in slot 0 (BiquadCombo); peaking slots 1..5 must forward
    # freq/q/gain verbatim from the designed list.
    for i, want in enumerate(designed[1:], start=1):
        block = cfg["filters"][f"cal_in0_peq_{i}"]
        assert block["type"] == "Biquad"
        p = block["parameters"]
        assert p["type"] == "Peaking"
        assert p["freq"] == want["freq"], f"freq mismatch slot {i}"
        assert p["q"] == want["q"], f"q mismatch slot {i}"
        assert p["gain"] == want["gain_db"], f"gain mismatch slot {i}"


@pytest.mark.asyncio
async def test_camilladsp_shelf_filter_block_emitted_correctly() -> None:
    """Driver emits Lowshelf/Highshelf YAML blocks byte-for-byte matching the
    designed spec — the same invariant as test C but for shelf filters.

    The Harman target-curve path uses low_shelf filters on the input channel.
    A regression where the driver mis-maps 'low_shelf' → wrong CamillaDSP
    type (e.g. 'Peaking' or missing block) would apply a flat response while
    group delay still changes (the phase-only symptom observed 2026-05-25).
    """
    driver = CamillaDSPDriver(input_channels=2, output_channels=4)
    call = _stub_client(driver)

    designed = [
        {"freq": 18.0,  "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 38.0,  "gain_db": 4.5, "q": 0.7,   "type": "low_shelf"},
        {"freq": 68.0,  "gain_db": 2.0, "q": 0.7,   "type": "low_shelf"},
        {"freq": 100.0, "gain_db": -3.0, "q": 0.7,  "type": "high_shelf"},
    ]
    await driver.apply_input_eq(
        preset=0, filters=designed, input_index=0,
        simulation_verified=True, bypass_iteration_limit=True,
    )

    cfg = _last_pushed_config(call)
    # slot 0: BiquadCombo HPF — skip
    # slot 1: low_shelf → Biquad Lowshelf
    block1 = cfg["filters"]["cal_in0_peq_1"]
    assert block1["type"] == "Biquad"
    p1 = block1["parameters"]
    assert p1["type"] == "Lowshelf", f"low_shelf must emit Lowshelf, got {p1['type']!r}"
    assert p1["freq"] == 38.0
    assert p1["gain"] == 4.5
    # shelves use slope (dB/oct), not q; Q=0.7 → slope = 6/(0.7*√2) ≈ 6.06
    assert "slope" in p1, f"Lowshelf must use slope, not q; got keys {list(p1.keys())}"
    assert "q" not in p1, "Lowshelf must NOT emit q parameter"
    assert 5.5 <= p1["slope"] <= 7.0, f"slope for Q=0.7 expected ~6.06, got {p1['slope']}"

    # slot 2: second low_shelf
    block2 = cfg["filters"]["cal_in0_peq_2"]
    assert block2["type"] == "Biquad"
    p2 = block2["parameters"]
    assert p2["type"] == "Lowshelf"
    assert p2["freq"] == 68.0
    assert "slope" in p2

    # slot 3: high_shelf → Biquad Highshelf
    block3 = cfg["filters"]["cal_in0_peq_3"]
    assert block3["type"] == "Biquad"
    p3 = block3["parameters"]
    assert p3["type"] == "Highshelf", f"high_shelf must emit Highshelf, got {p3['type']!r}"
    assert p3["freq"] == 100.0
    assert p3["gain"] == -3.0
    assert "slope" in p3, f"Highshelf must use slope, not q; got keys {list(p3.keys())}"
    assert "q" not in p3, "Highshelf must NOT emit q parameter"


# ── C2. apply_input_eq requires target ───────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_input_eq_without_target_returns_error() -> None:
    """_tool_apply_input_eq must reject calls with no target.

    The legacy no-target dispatch path was removed (2026-05-25) because it
    silently routed to the first DSP driver regardless of signal-graph
    topology. Callers that omit target must get a clear error — not a silent
    misroute to the wrong processor.
    """
    from calibrate.mcp_server import _tool_apply_input_eq
    filters = [
        {"freq": 18.0, "gain_db": 0.0, "q": 0.707, "type": "hpf"},
        {"freq": 38.0, "gain_db": 4.5, "q": 0.7,   "type": "low_shelf"},
    ]
    result = await _tool_apply_input_eq(filters=filters, target=None)
    assert result.get("ok") is False, "target=None must return ok=False"
    assert "target" in result.get("error", "").lower(), (
        f"error must mention 'target'; got: {result.get('error')!r}"
    )


# ── D. Single-application guard ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_input_peq_applied_exactly_once_on_routed_path() -> None:
    """Input PEQ writes to all logical inputs, but the routing mixer must
    deliver each output's signal from at most ONE input. Otherwise a stack
    of PEQ cuts gets summed multiple times and cut depth lands 2-3× deeper
    than ``simulate_eq`` predicts — exactly the 2026-05-25 bug symptom.

    Default routing on the production setup: input 0 → outputs 5,6 (subs);
    input 1 routes nowhere. This pins the invariant. If a future change
    routes both inputs to the same output (e.g., stereo input EQ), the
    driver MUST either (a) write PEQ only to the routed input, or
    (b) halve the mixer source gains — either way, this test flags it.
    """
    driver = CamillaDSPDriver(
        input_channels=2,
        output_channels=20,
        capture_channels=20,
        lfe_input_channel=2,
        sub_outputs=[5, 6],
        routed_outputs=[5, 6],
    )
    _stub_client(driver)

    routing = driver._routing
    for out_idx in range(driver._output_channels):
        sources = [
            inp for inp in range(driver._input_channels)
            if routing.get(inp, {}).get(out_idx, False)
        ]
        assert len(sources) <= 1, (
            f"output {out_idx} sums {len(sources)} input channels — "
            f"input PEQ would be applied {len(sources)}×. "
            f"This is the 2-3× cut-depth regression the bug was about."
        )
