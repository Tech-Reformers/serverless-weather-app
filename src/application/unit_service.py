"""
UnitConversionService — application-layer service for temperature unit
conversion and user preference management.

This service wraps a :class:`~src.domain.services.UnitConversionServicePort`
and adds:

* Real-time in-process conversion of weather data objects without requiring
  a new API call (Requirements 6.2, Property 8).
* Celsius default when no preference is stored (Requirement 6.7).
* Strict validation restricting units to CELSIUS / FAHRENHEIT only
  (Requirement 6.3, Property 2).

The temperature conversion itself is pure math with no I/O, so it completes
well within the 1-second constraint of Requirement 6.2.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import List

from src.domain.exceptions import DatabaseError, ValidationError
from src.domain.models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    TempUnit,
)
from src.domain.services import UnitConversionServicePort

logger = logging.getLogger(__name__)


class UnitConversionService:
    """Application service for temperature unit conversion and user preferences.

    All conversion methods are pure (no I/O) and resolve within 1 second as
    required by Requirement 6.2.  Preference persistence is delegated to the
    injected port (Requirements 6.4, 6.5).

    Args:
        unit_port: Infrastructure adapter implementing
            :class:`~src.domain.services.UnitConversionServicePort`.
    """

    def __init__(self, unit_port: UnitConversionServicePort) -> None:
        self._port = unit_port

    # ------------------------------------------------------------------
    # Core conversion
    # ------------------------------------------------------------------

    async def convert_temperature(
        self,
        value: float,
        from_unit: TempUnit,
        to_unit: TempUnit,
    ) -> float:
        """Convert a temperature value between Celsius and Fahrenheit.

        Pure math — no I/O.  Validates that both units are valid
        :class:`~src.domain.models.TempUnit` members (Property 2,
        Requirement 6.3).

        Args:
            value: Temperature value to convert.
            from_unit: Unit *value* is currently expressed in.
            to_unit: Target unit.

        Returns:
            Converted temperature as a float.

        Raises:
            ValidationError: If *from_unit* or *to_unit* is not a valid
                :class:`~src.domain.models.TempUnit` (Property 2).
        """
        from_unit = _coerce_unit(from_unit)
        to_unit = _coerce_unit(to_unit)
        return _convert(value, from_unit, to_unit)

    # ------------------------------------------------------------------
    # User preference persistence
    # ------------------------------------------------------------------

    async def get_user_preference(self, user_id: str) -> TempUnit:
        """Return the persisted temperature unit preference for *user_id*.

        Falls back to :attr:`~src.domain.models.TempUnit.CELSIUS` when no
        preference has been stored (Requirement 6.7).

        Args:
            user_id: Unique user identifier.

        Returns:
            The user's :class:`~src.domain.models.TempUnit`, defaulting to
            ``CELSIUS``.

        Raises:
            DatabaseError: If the underlying storage read fails.
        """
        return await self._port.get_user_preference(user_id)

    async def set_user_preference(self, user_id: str, unit: TempUnit) -> None:
        """Persist *unit* as *user_id*'s preferred temperature unit.

        Requirements 6.4, 6.5: the preference must survive across sessions.
        If the write fails the caller should retain the selected unit for the
        current session and surface a save-error indication (Requirement 6.6,
        Property 19).

        Args:
            user_id: Unique user identifier.
            unit: The :class:`~src.domain.models.TempUnit` to persist.

        Raises:
            ValidationError: If *unit* is not a valid TempUnit (Requirement 6.3).
            DatabaseError: If the persistence layer write fails.  Callers
                should catch this and degrade gracefully per Requirement 6.6.
        """
        unit = _coerce_unit(unit)
        await self._port.set_user_preference(user_id, unit)

    # ------------------------------------------------------------------
    # Bulk conversion helpers (real-time, no API refresh — Requirement 6.2)
    # ------------------------------------------------------------------

    async def convert_conditions(
        self,
        conditions: CurrentConditions,
        to_unit: TempUnit,
    ) -> CurrentConditions:
        """Return a new :class:`~src.domain.models.CurrentConditions` with
        ``temperature`` and ``feels_like`` converted to *to_unit*.

        The source unit of the incoming data is always Celsius (the internal
        storage / API unit).  This method detects the source unit from the
        data as-is, so callers must track which unit the data is currently in
        if they are calling this more than once.

        All other fields (humidity, wind speed, etc.) are copied unchanged.

        Args:
            conditions: Source weather conditions.
            to_unit: Target temperature unit.

        Returns:
            New :class:`~src.domain.models.CurrentConditions` instance.

        Raises:
            ValidationError: If *to_unit* is invalid.
        """
        to_unit = _coerce_unit(to_unit)
        return dataclasses.replace(
            conditions,
            temperature=_convert(conditions.temperature, TempUnit.CELSIUS, to_unit),
            feels_like=_convert(conditions.feels_like, TempUnit.CELSIUS, to_unit),
        )

    async def convert_hourly_forecast(
        self,
        forecasts: List[HourlyForecast],
        to_unit: TempUnit,
    ) -> List[HourlyForecast]:
        """Return a new list of :class:`~src.domain.models.HourlyForecast`
        entries with ``temperature`` and ``feels_like`` converted to *to_unit*.

        Assumes source data is in Celsius (internal representation).

        Args:
            forecasts: Source hourly forecasts.
            to_unit: Target temperature unit.

        Returns:
            New list of :class:`~src.domain.models.HourlyForecast` instances.

        Raises:
            ValidationError: If *to_unit* is invalid.
        """
        to_unit = _coerce_unit(to_unit)
        return [
            dataclasses.replace(
                entry,
                temperature=_convert(entry.temperature, TempUnit.CELSIUS, to_unit),
                feels_like=_convert(entry.feels_like, TempUnit.CELSIUS, to_unit),
            )
            for entry in forecasts
        ]

    async def convert_daily_forecast(
        self,
        forecasts: List[DailyForecast],
        to_unit: TempUnit,
    ) -> List[DailyForecast]:
        """Return a new list of :class:`~src.domain.models.DailyForecast`
        entries with ``high_temp`` and ``low_temp`` converted to *to_unit*.

        Assumes source data is in Celsius (internal representation).

        Args:
            forecasts: Source daily forecasts.
            to_unit: Target temperature unit.

        Returns:
            New list of :class:`~src.domain.models.DailyForecast` instances.

        Raises:
            ValidationError: If *to_unit* is invalid.
        """
        to_unit = _coerce_unit(to_unit)
        return [
            dataclasses.replace(
                entry,
                high_temp=_convert(entry.high_temp, TempUnit.CELSIUS, to_unit),
                low_temp=_convert(entry.low_temp, TempUnit.CELSIUS, to_unit),
            )
            for entry in forecasts
        ]


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _coerce_unit(unit: object) -> TempUnit:
    """Ensure *unit* is a :class:`~src.domain.models.TempUnit` instance.

    Raises:
        ValidationError: If *unit* cannot be coerced to a valid TempUnit
            (Requirement 6.3, Property 2).
    """
    if isinstance(unit, TempUnit):
        return unit
    try:
        return TempUnit(unit)
    except (ValueError, KeyError) as exc:
        raise ValidationError(
            f"Invalid temperature unit: {unit!r}. "
            "Allowed values: 'celsius', 'fahrenheit'."
        ) from exc


def _convert(value: float, from_unit: TempUnit, to_unit: TempUnit) -> float:
    """Convert *value* between Celsius and Fahrenheit.

    Preserves physical relationships — freezing point is always 0 °C / 32 °F
    (Property 10).

    No validation is performed here; callers must coerce units first via
    :func:`_coerce_unit`.
    """
    if from_unit == to_unit:
        return value
    if from_unit == TempUnit.CELSIUS:
        return value * 9 / 5 + 32  # °C → °F
    return (value - 32) * 5 / 9  # °F → °C
