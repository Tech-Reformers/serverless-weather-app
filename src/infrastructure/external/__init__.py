"""
External API adapters — infrastructure implementations of the domain service
ports that communicate with third-party weather and geocoding providers.
"""
from .geocoding_adapter import GeocodingAdapter
from .weather_api_adapter import WeatherAPIAdapter

__all__ = [
    "GeocodingAdapter",
    "WeatherAPIAdapter",
]
