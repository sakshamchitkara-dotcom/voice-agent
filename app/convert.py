"""Unit conversion (local table) and currency conversion (Frankfurter, ECB rates, no key)."""
from __future__ import annotations

import re

import httpx

from .web_tools import http, ttl_cache

FRANKFURTER = "https://api.frankfurter.dev/v1/latest"

# Factor to the dimension's base unit (m, kg, L, m/s). US customary for volumes.
UNITS: dict[str, dict[str, float]] = {
    "length": {
        "m": 1, "meter": 1, "metre": 1, "km": 1000, "kilometer": 1000, "kilometre": 1000,
        "cm": 0.01, "centimeter": 0.01, "centimetre": 0.01, "mm": 0.001, "millimeter": 0.001,
        "millimetre": 0.001, "mi": 1609.344, "mile": 1609.344, "yd": 0.9144, "yard": 0.9144,
        "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "in": 0.0254, "inch": 0.0254,
        "inches": 0.0254, "nmi": 1852, "nauticalmile": 1852,
    },
    "mass": {
        "kg": 1, "kilogram": 1, "g": 0.001, "gram": 0.001, "mg": 1e-6, "milligram": 1e-6,
        "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "oz": 0.028349523125,
        "ounce": 0.028349523125, "st": 6.35029318, "stone": 6.35029318, "t": 1000, "tonne": 1000,
    },
    "volume": {
        "l": 1, "liter": 1, "litre": 1, "ml": 0.001, "milliliter": 0.001, "millilitre": 0.001,
        "gal": 3.785411784, "gallon": 3.785411784, "qt": 0.946352946, "quart": 0.946352946,
        "pt": 0.473176473, "pint": 0.473176473, "cup": 0.2365882365, "floz": 0.0295735295625,
        "fluidounce": 0.0295735295625, "tbsp": 0.01478676478125, "tablespoon": 0.01478676478125,
        "tsp": 0.00492892159375, "teaspoon": 0.00492892159375,
    },
    "speed": {
        "m/s": 1, "mps": 1, "km/h": 1 / 3.6, "kph": 1 / 3.6, "kmh": 1 / 3.6, "mph": 0.44704,
        "knot": 0.514444, "kn": 0.514444,
    },
}
TEMPS = {"c": "c", "celsius": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def _norm(unit: str) -> str:
    u = re.sub(r"[\s.°_-]", "", unit.lower())
    return u.replace("degrees", "").replace("degree", "") or u


def _lookup(unit: str) -> tuple[str, float | str] | None:
    u = _norm(unit)
    for cand in (u, u[:-1] if u.endswith("s") else None, u[:-2] if u.endswith("es") else None):
        if not cand:
            continue
        if cand in TEMPS:
            return "temperature", TEMPS[cand]
        for dim, table in UNITS.items():
            if cand in table:
                return dim, table[cand]
    return None


def _to_kelvin(v: float, u: str) -> float:
    return {"c": v + 273.15, "f": (v - 32) * 5 / 9 + 273.15, "k": v}[u]


def _from_kelvin(v: float, u: str) -> float:
    return {"c": v - 273.15, "f": (v - 273.15) * 9 / 5 + 32, "k": v}[u]


def convert_units(amount: float, frm: str, to: str) -> float | None:
    """Convert between physical units. None if either unit is unknown (maybe a currency)."""
    a, b = _lookup(frm), _lookup(to)
    if not a or not b:
        return None
    if a[0] != b[0]:
        raise ValueError(f"I can't convert {a[0]} ({frm}) into {b[0]} ({to}).")
    if a[0] == "temperature":
        return _from_kelvin(_to_kelvin(amount, a[1]), b[1])
    return amount * a[1] / b[1]


def fmt(x: float) -> str:
    """Speakable number: 3 significant decimals, no trailing zeros, no float noise."""
    if abs(x) >= 1000:
        return f"{x:,.0f}" if abs(x) >= 100_000 else f"{x:,.2f}".rstrip("0").rstrip(".")
    return f"{x:.4g}" if abs(x) < 1 else f"{x:.2f}".rstrip("0").rstrip(".")


@ttl_cache(3600)  # ECB reference rates change once per working day
async def fx_rate(base: str, quote: str) -> tuple[float, str]:
    if base == quote:
        return 1.0, "today"
    r = await http().get(FRANKFURTER, params={"base": base, "symbols": quote})
    if r.status_code == 404:
        raise ValueError(f"I don't have exchange rates for {base} to {quote}. "
                         "I cover about 30 major currencies.")
    r.raise_for_status()
    d = r.json()
    return float(d["rates"][quote]), d["date"]


async def convert(amount: float, frm: str, to: str) -> str:
    value = convert_units(amount, frm, to)
    if value is not None:
        return f"{fmt(amount)} {frm} is {fmt(value)} {to}."
    base, quote = frm.strip().upper(), to.strip().upper()
    if not (re.fullmatch(r"[A-Z]{3}", base) and re.fullmatch(r"[A-Z]{3}", quote)):
        raise ValueError(f"I don't know how to convert {frm} to {to}. Use units like km or lb, "
                         "or three-letter currency codes like USD.")
    rate, date = await fx_rate(base, quote)
    return (f"{fmt(amount)} {base} is about {fmt(amount * rate)} {quote} "
            f"(rate {fmt(rate)}, ECB reference rate from {date}).")
