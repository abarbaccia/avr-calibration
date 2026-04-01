"""Shared base types for hardware drivers."""

from __future__ import annotations


class DriverError(RuntimeError):
    """Raised by driver methods when hardware communication fails.

    All driver methods (AVRDriver, DSPDriver) catch hardware-specific exceptions
    (MinidspApiError, denonavr exceptions, asyncio.TimeoutError) and re-raise
    as DriverError so callers need only catch one exception type.
    """
