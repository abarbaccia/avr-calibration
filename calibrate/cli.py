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
    click.echo()
