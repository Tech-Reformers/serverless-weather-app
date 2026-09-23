"""
Input validation functions for the Serverless Weather App.

All functions are pure (no side-effects, no I/O) so they can be used freely
throughout the application layers without introducing infrastructure coupling.

Raises :class:`ValidationError` (imported from ``src.domain.exceptions``) for
inputs that violate the domain rules documented in requirements.md.
"""

from __future__ import annotations

from .models import TempUnit


# ---------------------------------------------------------------------------
# Exceptions — defined here to keep the domain package self-contained.
# A re-export also lives in ``__init__.py`` for convenience.
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    """Raised when a domain-level validation rule is violated.

    Attributes:
        message: Human-readable description of the violation.
        field: Optional name of the field that failed validation.
    """

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def __repr__(self) -> str:  # pragma: no cover
        return f"ValidationError(message={self.message!r}, field={self.field!r})"


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def validate_query_length(query: str) -> bool:
    """Return ``True`` if *query* contains at least 2 non-whitespace characters.

    Per Requirement 1.2, a search must be silently blocked (no external API
    call) when the stripped query is shorter than 2 characters.

    Args:
        query: Raw search string entered by the user.

    Returns:
        ``True`` when the query is valid (≥2 non-whitespace chars).
        ``False`` otherwise — **does not raise**; callers decide the response.

    Examples::

        >>> validate_query_length("Lo")
        True
        >>> validate_query_length("  a  ")   # only 1 non-whitespace char
        False
        >>> validate_query_length("")
        False
    """
    return len(query.replace(" ", "").replace("\t", "").replace("\n", "")) >= 2


def validate_humidity(value: int) -> bool:
    """Return ``True`` if *value* is a valid humidity percentage (0–100 inclusive).

    Per Requirement 2.3, humidity must be displayed as a percentage between
    0 and 100.

    Args:
        value: Humidity value to validate.

    Returns:
        ``True`` when *value* is in [0, 100], ``False`` otherwise.

    Examples::

        >>> validate_humidity(65)
        True
        >>> validate_humidity(101)
        False
        >>> validate_humidity(-1)
        False
    """
    return 0 <= value <= 100


def validate_temp_unit(unit: str) -> TempUnit:
    """Parse and validate a temperature unit string.

    Per Requirement 6.3, the only acceptable values are ``"celsius"`` and
    ``"fahrenheit"`` (case-insensitive).

    Args:
        unit: Raw unit string (e.g. from a query parameter or stored preference).

    Returns:
        The matching :class:`~models.TempUnit` enum member.

    Raises:
        :class:`ValidationError`: When *unit* is not a recognised temperature unit.

    Examples::

        >>> validate_temp_unit("celsius")
        <TempUnit.CELSIUS: 'celsius'>
        >>> validate_temp_unit("Fahrenheit")
        <TempUnit.FAHRENHEIT: 'fahrenheit'>
        >>> validate_temp_unit("kelvin")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
            ...
        ValidationError: Invalid temperature unit: 'kelvin'. ...
    """
    normalised = unit.strip().lower()
    try:
        return TempUnit(normalised)
    except ValueError:
        valid = ", ".join(f"'{m.value}'" for m in TempUnit)
        raise ValidationError(
            f"Invalid temperature unit: {unit!r}. Must be one of: {valid}.",
            field="temperature_unit",
        )


def validate_coordinates(lat: float, lon: float) -> bool:
    """Return ``True`` if *lat* and *lon* are within valid geographic ranges.

    Latitude must be in [-90, 90] and longitude in [-180, 180].

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        ``True`` when both values are within range, ``False`` otherwise.

    Examples::

        >>> validate_coordinates(51.5074, -0.1278)   # London
        True
        >>> validate_coordinates(91.0, 0.0)          # lat out of range
        False
        >>> validate_coordinates(0.0, 181.0)         # lon out of range
        False
    """
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0
