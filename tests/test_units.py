import pytest

from shotlake import units


def test_yards_to_meters():
    assert units.yd_to_m(100) == pytest.approx(91.44)


def test_mph_and_kmh_to_ms():
    assert units.mph_to_ms(100) == pytest.approx(44.704)
    assert units.kmh_to_ms(36) == pytest.approx(10.0)


def test_feet_to_meters_and_fahrenheit():
    assert units.ft_to_m(10) == pytest.approx(3.048)
    assert units.f_to_c(77) == pytest.approx(25.0)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3.8 R", 3.8),
        ("1.2 L", -1.2),
        ("0 R", 0.0),
        ("-0.7", -0.7),
        ("12", 12.0),
    ],
)
def test_parse_signed(text, expected):
    assert units.parse_signed(text) == expected


@pytest.mark.parametrize("text", ["", "-", "- *", "N/A", None, "   "])
def test_missing_markers_are_none(text):
    assert units.parse_signed(text) is None
    assert units.parse_number(text) is None


def test_parse_number_strips_unit_suffix():
    assert units.parse_number("77 °F") == 77.0
    assert units.parse_number("50 %") == 50.0
    assert units.parse_number("14.7 psi") == 14.7
