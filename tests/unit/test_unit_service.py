"""
Unit tests for UnitConversionService (src/application/unit_service.py).

All external I/O is replaced with simple async mocks — no DynamoDB or network
access is required.

Validated properties / requirements:
    Property 2  — Never return an invalid temperature unit (Req 6.3)
    Property 10 — Temperature conversion preserves physical relationships (Req 6.1)
    Req 6.4 / 6.5 — Preference persisted via port
    Req 6.6 / Property 19 — DatabaseError propagated so caller can degrade gracefully
    Req 6.7 — Default unit is CELSIUS when no preference stored
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.application.unit_service import UnitConversionService
from src.domain.exceptions import DatabaseError, ValidationError
from src.domain.models import (
    CurrentConditions,
    DailyForecast,
    HourlyForecast,
    TempUnit,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_port(
    *,
    get_pref: TempUnit = TempUnit.CELSIUS,
    set_pref_exc: Exception | None = None,
) -> AsyncMock:
    """Build a minimal mock UnitConversionServicePort."""
    port = AsyncMock()
    port.get_user_preference.return_value = get_pref
    if set_pref_exc is not None:
        port.set_user_preference.side_effect = set_pref_exc
    else:
        port.set_user_preference.return_value = None
    port.convert_temperature = AsyncMock()
    return port


def _service(port=None) -> UnitConversionService:
    return UnitConversionService(port or _make_port())


def _now() -> datetime:
    return datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _conditions(temp: float = 20.0, feels: float = 18.0) -> CurrentConditions:
    return CurrentConditions(
        temperature=temp,
        feels_like=feels,
        humidity=50,
        pressure=1013,
        wind_speed=5.0,
        wind_direction=180,
        condition="Sunny",
        icon="sun",
        timestamp=_now(),
        sunrise=None,
        sunset=None,
        visibility=10000,
    )


def _hourly(temp: float = 15.0, feels: float = 13.0) -> HourlyForecast:
    return HourlyForecast(
        timestamp=_now(),
        temperature=temp,
        feels_like=feels,
        humidity=60,
        condition="Cloudy",
        icon="cloud",
        precipitation_probability=20,
        wind_speed=8.0,
    )


def _daily(high: float = 25.0, low: float = 10.0) -> DailyForecast:
    return DailyForecast(
        date=_now(),
        high_temp=high,
        low_temp=low,
        condition="Partly cloudy",
        icon="partly_cloudy",
        precipitation_probability=10,
        sunrise=_now(),
        sunset=_now(),
    )


# ---------------------------------------------------------------------------
# Temperature conversion — Property 10
# ---------------------------------------------------------------------------


class TestConvertTemperature:
    """Property 10: Conversion preserves physical temperature relationships."""

    async def test_celsius_to_fahrenheit_freezing_point(self):
        """0 °C must equal 32 °F (freezing point invariant — Property 10)."""
        result = await _service().convert_temperature(
            0.0, TempUnit.CELSIUS, TempUnit.FAHRENHEIT
        )
        assert result == pytest.approx(32.0)

    async def test_fahrenheit_to_celsius_freezing_point(self):
        """32 °F must equal 0 °C (freezing point invariant — Property 10)."""
        result = await _service().convert_temperature(
            32.0, TempUnit.FAHRENHEIT, TempUnit.CELSIUS
        )
        assert result == pytest.approx(0.0)

    async def test_celsius_to_fahrenheit_boiling_point(self):
        """100 °C must equal 212 °F."""
        result = await _service().convert_temperature(
            100.0, TempUnit.CELSIUS, TempUnit.FAHRENHEIT
        )
        assert result == pytest.approx(212.0)

    async def test_fahrenheit_to_celsius_body_temp(self):
        """98.6 °F should round-trip to approximately 37 °C."""
        result = await _service().convert_temperature(
            98.6, TempUnit.FAHRENHEIT, TempUnit.CELSIUS
        )
        assert result == pytest.approx(37.0, abs=1e-4)

    async def test_same_unit_celsius_returns_unchanged(self):
        """Converting C→C must return the original value."""
        result = await _service().convert_temperature(42.0, TempUnit.CELSIUS, TempUnit.CELSIUS)
        assert result == pytest.approx(42.0)

    async def test_same_unit_fahrenheit_returns_unchanged(self):
        """Converting F→F must return the original value."""
        result = await _service().convert_temperature(
            42.0, TempUnit.FAHRENHEIT, TempUnit.FAHRENHEIT
        )
        assert result == pytest.approx(42.0)

    async def test_invalid_from_unit_raises_validation_error(self):
        """An unrecognised unit string must raise ValidationError (Property 2)."""
        with pytest.raises(ValidationError):
            await _service().convert_temperature(0.0, "kelvin", TempUnit.CELSIUS)

    async def test_invalid_to_unit_raises_validation_error(self):
        """An unrecognised target unit must raise ValidationError (Property 2)."""
        with pytest.raises(ValidationError):
            await _service().convert_temperature(0.0, TempUnit.CELSIUS, "rankine")

    async def test_both_invalid_units_raise_validation_error(self):
        """Both units invalid — should still raise ValidationError (Property 2)."""
        with pytest.raises(ValidationError):
            await _service().convert_temperature(0.0, "bad", "also_bad")


# ---------------------------------------------------------------------------
# User preference — Requirement 6.7, 6.4, 6.5, 6.6 / Property 19
# ---------------------------------------------------------------------------


class TestUserPreference:
    """Tests for get/set preference persistence."""

    async def test_get_preference_returns_stored_unit(self):
        """The port's value is returned as-is (Req 6.4, 6.5)."""
        port = _make_port(get_pref=TempUnit.FAHRENHEIT)
        svc = _service(port)
        result = await svc.get_user_preference("user-1")
        assert result == TempUnit.FAHRENHEIT
        port.get_user_preference.assert_awaited_once_with("user-1")

    async def test_get_preference_default_celsius_when_not_set(self):
        """When port returns CELSIUS (default), service passes it through (Req 6.7)."""
        svc = _service(_make_port(get_pref=TempUnit.CELSIUS))
        result = await svc.get_user_preference("new-user")
        assert result == TempUnit.CELSIUS

    async def test_set_preference_success(self):
        """Successful write delegates to port with correct arguments (Req 6.4)."""
        port = _make_port()
        svc = _service(port)
        await svc.set_user_preference("user-1", TempUnit.FAHRENHEIT)
        port.set_user_preference.assert_awaited_once_with("user-1", TempUnit.FAHRENHEIT)

    async def test_set_preference_database_error_propagated(self):
        """DatabaseError from port must propagate so caller can degrade gracefully
        (Req 6.6, Property 19)."""
        port = _make_port(set_pref_exc=DatabaseError("DynamoDB write failed"))
        svc = _service(port)
        with pytest.raises(DatabaseError):
            await svc.set_user_preference("user-1", TempUnit.CELSIUS)

    async def test_set_preference_invalid_unit_raises_validation_error(self):
        """Invalid unit string must raise ValidationError before touching the port
        (Req 6.3, Property 2)."""
        port = _make_port()
        svc = _service(port)
        with pytest.raises(ValidationError):
            await svc.set_user_preference("user-1", "kelvin")
        port.set_user_preference.assert_not_awaited()


# ---------------------------------------------------------------------------
# convert_conditions — Requirement 6.2
# ---------------------------------------------------------------------------


class TestConvertConditions:
    """Tests for CurrentConditions bulk conversion."""

    async def test_converts_temperature_and_feels_like_to_fahrenheit(self):
        """0 °C / 0 °C feels_like → 32 °F / 32 °F."""
        cond = _conditions(temp=0.0, feels=0.0)
        result = await _service().convert_conditions(cond, TempUnit.FAHRENHEIT)
        assert result.temperature == pytest.approx(32.0)
        assert result.feels_like == pytest.approx(32.0)

    async def test_other_fields_unchanged(self):
        """Humidity, wind speed, etc. must not be mutated."""
        cond = _conditions(temp=20.0, feels=18.0)
        result = await _service().convert_conditions(cond, TempUnit.FAHRENHEIT)
        assert result.humidity == cond.humidity
        assert result.wind_speed == cond.wind_speed
        assert result.condition == cond.condition

    async def test_returns_new_instance(self):
        """The original object must not be mutated (immutability)."""
        cond = _conditions(temp=20.0, feels=18.0)
        result = await _service().convert_conditions(cond, TempUnit.FAHRENHEIT)
        assert result is not cond
        assert cond.temperature == pytest.approx(20.0)  # original unchanged

    async def test_celsius_to_celsius_noop(self):
        """Converting to the same unit returns equal values."""
        cond = _conditions(temp=25.0, feels=23.0)
        result = await _service().convert_conditions(cond, TempUnit.CELSIUS)
        assert result.temperature == pytest.approx(25.0)
        assert result.feels_like == pytest.approx(23.0)


# ---------------------------------------------------------------------------
# convert_hourly_forecast — Requirement 6.2
# ---------------------------------------------------------------------------


class TestConvertHourlyForecast:
    """Tests for HourlyForecast list bulk conversion."""

    async def test_converts_all_entries(self):
        """Every entry in the list must have temperature/feels_like converted."""
        forecasts = [_hourly(0.0, 0.0), _hourly(100.0, 100.0)]
        result = await _service().convert_hourly_forecast(forecasts, TempUnit.FAHRENHEIT)
        assert len(result) == 2
        assert result[0].temperature == pytest.approx(32.0)
        assert result[0].feels_like == pytest.approx(32.0)
        assert result[1].temperature == pytest.approx(212.0)
        assert result[1].feels_like == pytest.approx(212.0)

    async def test_empty_list_returns_empty_list(self):
        result = await _service().convert_hourly_forecast([], TempUnit.FAHRENHEIT)
        assert result == []

    async def test_other_fields_unchanged(self):
        entry = _hourly(15.0, 13.0)
        result = await _service().convert_hourly_forecast([entry], TempUnit.FAHRENHEIT)
        assert result[0].humidity == entry.humidity
        assert result[0].precipitation_probability == entry.precipitation_probability

    async def test_returns_new_instances(self):
        """Original forecast entries must not be mutated."""
        entry = _hourly(20.0, 18.0)
        result = await _service().convert_hourly_forecast([entry], TempUnit.FAHRENHEIT)
        assert result[0] is not entry
        assert entry.temperature == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# convert_daily_forecast — Requirement 6.2
# ---------------------------------------------------------------------------


class TestConvertDailyForecast:
    """Tests for DailyForecast list bulk conversion."""

    async def test_converts_high_and_low(self):
        """0 °C high/low → 32 °F high/low."""
        forecasts = [_daily(high=0.0, low=0.0)]
        result = await _service().convert_daily_forecast(forecasts, TempUnit.FAHRENHEIT)
        assert result[0].high_temp == pytest.approx(32.0)
        assert result[0].low_temp == pytest.approx(32.0)

    async def test_converts_multiple_entries(self):
        forecasts = [_daily(25.0, 10.0), _daily(30.0, 15.0)]
        result = await _service().convert_daily_forecast(forecasts, TempUnit.FAHRENHEIT)
        assert len(result) == 2
        assert result[0].high_temp == pytest.approx(77.0)
        assert result[0].low_temp == pytest.approx(50.0)
        assert result[1].high_temp == pytest.approx(86.0)
        assert result[1].low_temp == pytest.approx(59.0)

    async def test_empty_list_returns_empty_list(self):
        result = await _service().convert_daily_forecast([], TempUnit.FAHRENHEIT)
        assert result == []

    async def test_other_fields_unchanged(self):
        entry = _daily(25.0, 10.0)
        result = await _service().convert_daily_forecast([entry], TempUnit.FAHRENHEIT)
        assert result[0].condition == entry.condition
        assert result[0].precipitation_probability == entry.precipitation_probability

    async def test_returns_new_instances(self):
        """Original daily entries must not be mutated."""
        entry = _daily(20.0, 5.0)
        result = await _service().convert_daily_forecast([entry], TempUnit.FAHRENHEIT)
        assert result[0] is not entry
        assert entry.high_temp == pytest.approx(20.0)
