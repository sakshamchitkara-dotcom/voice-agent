import functools

import httpx
import pytest

from app import web_tools


@pytest.fixture
def mock_http(monkeypatch):
    """Route web_tools' httpx clients through a handler: mock_http(fn)."""
    def install(handler):
        real = httpx.AsyncClient
        monkeypatch.setattr(web_tools.httpx, "AsyncClient",
                            functools.partial(real, transport=httpx.MockTransport(handler)))
    return install


async def test_weather_formats_open_meteo(mock_http):
    def handler(req):
        if "geocoding" in req.url.host:
            return httpx.Response(200, json={"results": [
                {"name": "Paris", "country": "France", "latitude": 48.85, "longitude": 2.35}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 18.4, "apparent_temperature": 17.6,
                        "weather_code": 61, "wind_speed_10m": 9.2},
            "daily": {"temperature_2m_max": [21.0], "temperature_2m_min": [12.0],
                      "precipitation_probability_max": [70]}})
    mock_http(handler)
    out = await web_tools.get_weather("Paris")
    assert out == ("Paris, France: light rain, 18°C (feels like 18°C), wind 9 km/h. "
                   "Today 12 to 21°C, 70% chance of rain.")


async def test_weather_unknown_place(mock_http):
    mock_http(lambda req: httpx.Response(200, json={}))
    assert "couldn't find" in await web_tools.get_weather("Nowhere")
