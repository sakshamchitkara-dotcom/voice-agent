import functools

import httpx
import pytest

from app import convert


@pytest.mark.parametrize("amount,frm,to,expected", [
    (10, "km", "miles", "10 km is 6.21 miles."),
    (5, "feet", "cm", "5 feet is 152.4 cm."),
    (1, "cup", "ml", "1 cup is 236.59 ml."),
    (98.6, "°F", "celsius", "98.6 °F is 37 celsius."),
    (-40, "C", "F", "-40 C is -40 F."),
    (2.5, "lbs", "kg", "2.5 lbs is 1.13 kg."),
    (100, "km/h", "mph", "100 km/h is 62.14 mph."),
    (3000, "m", "ft", "3,000 m is 9,842.52 ft."),
])
async def test_unit_conversions(amount, frm, to, expected):
    assert await convert.convert(amount, frm, to) == expected


async def test_mismatched_dimensions_and_unknown_units():
    with pytest.raises(ValueError, match="can't convert length"):
        await convert.convert(1, "km", "kg")
    with pytest.raises(ValueError, match="don't know how to convert"):
        await convert.convert(1, "furlongs", "parsecs")


@pytest.fixture
def fx(monkeypatch):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if req.url.params["symbols"] == "XYZ":
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={"amount": 1.0, "base": "USD", "date": "2026-09-24",
                                         "rates": {"EUR": 0.87974}})
    real = httpx.AsyncClient
    monkeypatch.setattr(convert.httpx, "AsyncClient",
                        functools.partial(real, transport=httpx.MockTransport(handler)))
    return seen


async def test_currency_uses_frankfurter_and_caches(fx):
    out = await convert.convert(250, "usd", "eur")
    assert out == "250 USD is about 219.94 EUR (rate 0.8797, ECB reference rate from 2026-09-24)."
    await convert.convert(10, "USD", "EUR")
    assert fx == [{"base": "USD", "symbols": "EUR"}]


async def test_unknown_currency_is_a_spoken_error(fx):
    with pytest.raises(ValueError, match="don't have exchange rates for USD to XYZ"):
        await convert.convert(1, "USD", "XYZ")
    assert (await convert.convert(5, "EUR", "eur")).startswith("5 EUR is about 5 EUR")
