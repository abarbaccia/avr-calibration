"""CLI entry point for avr-calibration."""

import asyncio
import sys
from pathlib import Path

import click

from .config import Config, CONFIG_PATH
from .preflight import PreflightChecker

PASS_ICON = "✓"
FAIL_ICON = "✗"
COL_WIDTH = 14


@click.group()
@click.version_option()
def cli() -> None:
    """AVR Calibration — AI-first home theater bass optimization."""


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Config file path (default: {CONFIG_PATH})",
)
def check(config_path: Path | None) -> None:
    """Verify all hardware is connected and reachable before calibrating."""
    path = config_path or CONFIG_PATH

    if not path.exists():
        click.echo(f"No config found at {path}")
        click.echo("Creating a template config for you to fill in...")
        Config.create_template(path)
        click.echo(f"\nEdit {path} with your hardware details, then re-run:")
        click.echo("  calibrate check")
        sys.exit(1)

    cfg = Config.load(path)

    click.echo()
    click.echo("AVR Calibration — Hardware Pre-flight Check")
    click.echo("─" * 45)

    checker = PreflightChecker(cfg)
    results = asyncio.run(checker.run_all())

    for result in results:
        name_col = result.name.ljust(COL_WIDTH)
        if result.passed:
            icon = click.style(PASS_ICON, fg="green")
            click.echo(f"  {icon}  {name_col}{result.detail}")
        else:
            icon = click.style(FAIL_ICON, fg="red")
            click.echo(f"  {icon}  {name_col}{result.detail or ''}")
            if result.error:
                indent = " " * (5 + COL_WIDTH)
                click.echo(f"{indent}{click.style(result.error, fg='yellow')}")

    click.echo()
    failures = [r for r in results if not r.passed]
    if failures:
        msg = f"{len(failures)} of {len(results)} checks failed. Fix the above and re-run `calibrate check`."
        click.echo(click.style(msg, fg="red"))
        sys.exit(1)
    else:
        click.echo(click.style("All checks passed. Ready to calibrate.", fg="green"))


@cli.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Config file path (default: {CONFIG_PATH})",
)
@click.option("--label", default=None, help="Optional label for this session")
def measure(config_path: Path | None, label: str | None) -> None:
    """Run a log-sweep frequency response measurement and save to history."""
    from .measurement import MeasurementEngine
    from .storage import SessionStore

    path = config_path or CONFIG_PATH
    if not path.exists():
        click.echo(f"No config found at {path}. Run 'calibrate check' first to create one.")
        sys.exit(1)

    cfg = Config.load(path)
    freq_min = cfg.measurement.get("freq_min", 20)
    freq_max = cfg.measurement.get("freq_max", 200)
    duration = cfg.measurement.get("sweep_duration", 3.0)

    click.echo()
    click.echo("AVR Calibration — Measurement")
    click.echo("─" * 35)
    click.echo(f"  Sweep: {freq_min}–{freq_max} Hz, {duration:.1f}s log sweep")
    click.echo("  Playing sweep and recording response...")

    try:
        engine = MeasurementEngine(cfg)
        fr = engine.measure()
    except RuntimeError as exc:
        click.echo(click.style(f"\n  Error: {exc}", fg="red"))
        sys.exit(1)

    store = SessionStore()
    session_id = store.save_measurement(fr, label=label)

    click.echo()
    click.echo(click.style("  Measurement complete.", fg="green"))
    click.echo(f"  Session #{session_id} saved{f'  ({label})' if label else ''}")
    click.echo(f"  Peak: {fr.peak_spl:.1f} dBFS at {fr.freq_at_peak:.0f} Hz")
    click.echo(f"  Frequency points: {len(fr.frequencies)}")
    click.echo(f"  Run 'calibrate show {session_id}' to inspect or export the FR.")
    click.echo()


@cli.command()
def history() -> None:
    """List past measurement sessions."""
    from .storage import SessionStore, DB_PATH

    store = SessionStore()
    sessions = store.list_sessions()

    if not sessions:
        click.echo("No measurement sessions yet. Run 'calibrate measure' first.")
        return

    click.echo()
    click.echo("AVR Calibration — Measurement History")
    click.echo("─" * 65)
    header = f"  {'#':<5}  {'Date (UTC)':<22}  {'Label':<18}  {'Peak SPL':<10}  Pts"
    click.echo(header)
    click.echo("  " + "─" * 63)

    for s in sessions:
        ts = s.timestamp[:19].replace("T", " ")  # "2026-03-20 12:34:56"
        label = (s.label or "—")[:18]
        peak = f"{s.start_fr.peak_spl:.1f} dBFS"
        pts = len(s.start_fr.frequencies)
        end_marker = click.style(" ✓", fg="green") if s.end_fr else ""
        click.echo(f"  #{s.id:<4}  {ts:<22}  {label:<18}  {peak:<10}  {pts}{end_marker}")

    click.echo()
    click.echo(f"  {len(sessions)} session(s).  Run 'calibrate show <id>' to inspect.")
    click.echo()


@cli.command()
@click.argument("session_id", type=int)
@click.option("--csv", "as_csv", is_flag=True, default=False, help="Export FR as CSV to stdout")
@click.option("--json", "as_json", is_flag=True, default=False, help="Export FR as JSON to stdout")
def show(session_id: int, as_csv: bool, as_json: bool) -> None:
    """Inspect a measurement session. Use --csv or --json to export the FR data."""
    import csv as csv_mod
    import io
    import json as json_mod
    from .storage import SessionStore

    store = SessionStore()
    session = store.get_session(session_id)

    if session is None:
        click.echo(f"Session #{session_id} not found.", err=True)
        sys.exit(1)

    fr = session.start_fr

    if as_csv:
        buf = io.StringIO()
        writer = csv_mod.writer(buf)
        writer.writerow(["frequency_hz", "spl_dbfs"])
        for f, s in zip(fr.frequencies, fr.spl):
            writer.writerow([f"{f:.3f}", f"{s:.3f}"])
        click.echo(buf.getvalue(), nl=False)
        return

    if as_json:
        click.echo(json_mod.dumps({
            "session_id": session.id,
            "timestamp": session.timestamp,
            "label": session.label,
            "sample_rate": fr.sample_rate,
            "sweep_duration": fr.sweep_duration,
            "frequencies_hz": fr.frequencies,
            "spl_dbfs": fr.spl,
        }, indent=2))
        return

    # Human-readable summary
    click.echo()
    click.echo(f"AVR Calibration — Session #{session_id}")
    click.echo("─" * 45)
    ts = session.timestamp[:19].replace("T", " ")
    click.echo(f"  Date:       {ts} UTC")
    if session.label:
        click.echo(f"  Label:      {session.label}")
    click.echo(f"  Sample rate:{fr.sample_rate} Hz")
    click.echo(f"  Sweep:      {fr.sweep_duration:.1f}s log sweep")
    click.echo(f"  Band:       {fr.frequencies[0]:.0f}–{fr.frequencies[-1]:.0f} Hz  ({len(fr.frequencies)} pts)")
    click.echo(f"  Peak:       {fr.peak_spl:.1f} dBFS at {fr.freq_at_peak:.0f} Hz")
    if session.end_fr:
        delta = session.end_fr.peak_spl - fr.peak_spl
        click.echo(f"  Final peak: {session.end_fr.peak_spl:.1f} dBFS  (Δ{delta:+.1f} dB)")

    # ASCII mini-plot: 40-char wide, normalized to peak
    click.echo()
    click.echo("  Frequency response (start measurement):")
    _ascii_plot(fr.frequencies, fr.spl)

    feedback = store.get_feedback(session_id)
    if feedback:
        click.echo()
        click.echo(f"  Feedback ({len(feedback)} note(s)):")
        for entry in feedback:
            tag = f"[{entry['content_tag']}] " if entry["content_tag"] else ""
            click.echo(f"    • {tag}{entry['text']}")

    click.echo()
    click.echo("  Export: calibrate show {id} --csv  |  calibrate show {id} --json")
    click.echo()


@cli.command()
@click.option("--host", default="0.0.0.0", show_default=True, help="Host to bind")
@click.option("--port", default=8000, show_default=True, help="Port to listen on")
def web(host: str, port: int) -> None:
    """Start the web UI server for browser-based measurement."""
    import uvicorn
    click.echo(f"Starting web server at http://{host}:{port}")
    click.echo("Open that URL from any browser on your local network.")
    click.echo("Press Ctrl+C to stop.")
    uvicorn.run("calibrate.web:app", host=host, port=port, reload=False)


def _ascii_plot(frequencies: list[float], spl: list[float], width: int = 40) -> None:
    """Print a simple ASCII bar chart of the frequency response."""
    if not frequencies:
        return

    spl_min = min(spl)
    spl_max = max(spl)
    spl_range = spl_max - spl_min or 1.0

    # Sample ~10 representative frequencies log-spaced
    import math
    f_min, f_max = frequencies[0], frequencies[-1]
    n_bars = min(10, len(frequencies))
    log_steps = [
        f_min * (f_max / f_min) ** (i / max(n_bars - 1, 1))
        for i in range(n_bars)
    ]

    for target_f in log_steps:
        # Find closest frequency in data
        idx = min(range(len(frequencies)), key=lambda i: abs(frequencies[i] - target_f))
        f = frequencies[idx]
        s = spl[idx]
        bar_len = int((s - spl_min) / spl_range * width)
        bar = "█" * bar_len
        click.echo(f"  {f:>6.0f} Hz  {bar:<{width}}  {s:+.1f} dB")
