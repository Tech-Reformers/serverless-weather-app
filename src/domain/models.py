"""
Domain models for the Serverless Weather App.

These data classes represent the core entities and value objects used
throughout the application, independent of any infrastructure concerns.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class TempUnit(str, Enum):
    """Temperature unit preference.

    Restricted to Celsius and Fahrenheit per Requirements 6.3.
    """

    CELSIUS = "celsius"
    FAHRENHEIT = "fahrenheit"


@dataclass
class Location:
    """Geographic location identified by name and coordinates.

    Attributes:
        id: Unique location identifier (e.g. "london-uk").
        name: Human-readable city/place name.
        region: Administrative region or state.
        country: Country name or ISO code.
        latitude: Geographic latitude in decimal degrees.
        longitude: Geographic longitude in decimal degrees.
        timezone: IANA timezone string (e.g. "Europe/London").
    """

    id: str
    name: str
    region: str
    country: str
    latitude: float
    longitude: float
    timezone: str


@dataclass
class CurrentConditions:
    """Present weather state for a location.

    Attributes:
        temperature: Current temperature (unit determined by caller).
        feels_like: Apparent temperature.
        humidity: Relative humidity, 0–100 inclusive (Requirements 2.3).
        pressure: Atmospheric pressure in hPa.
        wind_speed: Wind speed with associated unit.
        wind_direction: Wind direction in degrees (0–360).
        condition: Human-readable condition description.
        icon: Icon identifier / URL supplied by the weather provider.
        timestamp: UTC time when conditions were recorded.
        sunrise: Local sunrise time (optional).
        sunset: Local sunset time (optional).
        visibility: Horizontal visibility in metres.
    """

    temperature: float
    feels_like: float
    humidity: int  # 0–100
    pressure: int  # hPa
    wind_speed: float
    wind_direction: int  # degrees 0–360
    condition: str
    icon: str
    timestamp: datetime
    sunrise: Optional[datetime]
    sunset: Optional[datetime]
    visibility: int  # metres


@dataclass
class HourlyForecast:
    """Single hourly forecast entry.

    Attributes:
        timestamp: UTC hour this forecast applies to.
        temperature: Forecast temperature.
        feels_like: Apparent temperature.
        humidity: Relative humidity, 0–100.
        condition: Condition description.
        icon: Icon identifier.
        precipitation_probability: Chance of precipitation, 0–100.
        wind_speed: Wind speed.
    """

    timestamp: datetime
    temperature: float
    feels_like: float
    humidity: int
    condition: str
    icon: str
    precipitation_probability: int  # 0–100
    wind_speed: float


@dataclass
class DailyForecast:
    """Single daily forecast entry.

    Attributes:
        date: Calendar date this forecast applies to.
        high_temp: Maximum temperature for the day.
        low_temp: Minimum temperature for the day.
        condition: Condition description.
        icon: Icon identifier.
        precipitation_probability: Chance of precipitation, 0–100.
        sunrise: Local sunrise datetime.
        sunset: Local sunset datetime.
    """

    date: datetime  # date portion is used; stored as datetime for convenience
    high_temp: float
    low_temp: float
    condition: str
    icon: str
    precipitation_probability: int  # 0–100
    sunrise: datetime
    sunset: datetime


@dataclass
class FavoriteLocation:
    """A location saved by a user for quick access.

    Attributes:
        user_id: Owner of this favourite.
        location: The saved Location.
        added_at: When the favourite was created.
        last_accessed: When the favourite was last viewed.
        current_temperature: Most recently fetched temperature; None when
            unavailable (Requirements 5.5).
    """

    user_id: str
    location: Location
    added_at: datetime
    last_accessed: datetime
    current_temperature: Optional[float] = None


@dataclass
class DeviceLocationData:
    """Raw coordinates reported by the user's device.

    Attributes:
        latitude: Device latitude in decimal degrees.
        longitude: Device longitude in decimal degrees.
        accuracy: Estimated accuracy radius in metres.
        timestamp: When the coordinates were recorded.
    """

    latitude: float
    longitude: float
    accuracy: float  # metres
    timestamp: datetime
