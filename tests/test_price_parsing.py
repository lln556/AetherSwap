"""Price text parsing and currency inference."""

import pytest

from utils.money import currency_code_from_price_text, parse_price_text


@pytest.mark.parametrize("raw,expected", [
    ("\u00a5 1,234.56", 1234.56),
    ("1.234,56 \u20ac", 1234.56),
    ("$1,234,567.89", 1234567.89),
    ("1,234", 1234.0),
    ("1,23 \u20ac", 1.23),
    ("R$ 12,50", 12.5),
    ("3.18", 3.18),
    ("  9,98  ", 9.98),
    ("", None),
    ("abc", None),
    ("\u00a5", None),
])
def test_parse_price_text(raw, expected):
    assert parse_price_text(raw) == expected


@pytest.mark.parametrize("raw,code", [
    ("\u00a5 5", "CNY"),
    ("5 \u20ac", "EUR"),
    ("US$ 5", "USD"),
    ("$ 5", "USD"),
    ("HK$ 5", "HKD"),
    ("R$ 5", "BRL"),
    ("S$ 5", "SGD"),
    ("A$ 5", "AUD"),
    ("C$ 5", "CAD"),
    ("\u20b9 5", "INR"),
    ("\u20bd 5", "RUB"),
    ("5", "CNY"),
])
def test_currency_code_from_price_text(raw, code):
    assert currency_code_from_price_text(raw) == code
