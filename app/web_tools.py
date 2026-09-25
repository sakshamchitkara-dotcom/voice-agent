"""Keyless internet tools: Open-Meteo weather, DuckDuckGo search, URL fetch."""
from __future__ import annotations

import httpx

UA = "Mozilla/5.0 (compatible; voice-agent/0.1; +https://github.com/sakshamchitkara-dotcom/voice-agent)"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)

# WMO weather interpretation codes used by Open-Meteo.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


async def get_weather(location: str) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA}) as client:
        geo = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        places = geo.json().get("results") or []
        if not places:
            return f"I couldn't find a place called {location}."
        p = places[0]
        wx = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": p["latitude"],
                "longitude": p["longitude"],
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "forecast_days": 1,
                "timezone": "auto",
            },
        )
        wx.raise_for_status()
        d = wx.json()
    cur, day = d["current"], d["daily"]
    name = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
    rain = day["precipitation_probability_max"][0]
    return (
        f"{name}: {WMO.get(cur['weather_code'], 'unknown conditions')}, "
        f"{cur['temperature_2m']:.0f}°C (feels like {cur['apparent_temperature']:.0f}°C), "
        f"wind {cur['wind_speed_10m']:.0f} km/h. Today {day['temperature_2m_min'][0]:.0f} to "
        f"{day['temperature_2m_max'][0]:.0f}°C"
        + (f", {rain}% chance of rain." if rain is not None else ".")
    )
