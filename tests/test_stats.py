"""Bootstrap-Intervalle und Zeitraumvergleich."""

import statistics

import pytest

from shotlake.analytics.stats import bootstrap_ci, bootstrap_diff_ci, compare


def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    values = [20.0, 22.0, 25.0, 30.0, 35.0, 38.0, 41.0]
    ci = bootstrap_ci(values)
    assert ci is not None and ci == bootstrap_ci(values)  # gleicher Seed, gleiches Ergebnis
    assert ci.low < statistics.fmean(values) < ci.high and ci.level == 0.95
    assert ci.low >= min(values) and ci.high <= max(values)


def test_too_few_values_give_no_interval():
    assert bootstrap_ci([1.0, 2.0]) is None
    assert bootstrap_diff_ci([1.0, 2.0, 3.0], [1.0, 2.0]) is None


def test_constant_values_have_zero_width_interval():
    ci = bootstrap_ci([5.0] * 6)
    assert ci is not None and ci.low == ci.high == 5.0


@pytest.mark.parametrize(
    ("a", "b", "better", "expected"),
    [
        ([10.0] * 6, [20.0] * 6, "lower", "wahrscheinlich_besser"),
        ([20.0] * 6, [10.0] * 6, "lower", "wahrscheinlich_schlechter"),
        ([20.0] * 6, [10.0] * 6, "higher", "wahrscheinlich_besser"),
        ([10.0] * 6, [20.0] * 6, None, "unterschied_belegt"),
        (
            [10, 30, 20, 25, 15, 28],
            [12, 28, 22, 24, 18, 26],
            "lower",
            "kein_belegbarer_unterschied",
        ),
        ([10.0, 11.0], [20.0, 21.0], "lower", "zu_wenig_daten"),  # n < min_n: keine Richtung
        ([], [1.0, 2.0, 3.0, 4.0, 5.0], "lower", "zu_wenig_daten"),
    ],
)
def test_comparison_assessment(a, b, better, expected):
    result = compare(a, b, better=better, min_n=5)
    assert result.assessment == expected
    if expected == "zu_wenig_daten":
        assert "insufficient_data" in result.warnings


def test_delta_and_relative_change():
    result = compare([8.0] * 5, [10.0] * 5, better="lower", min_n=5)
    assert result.delta == pytest.approx(-2.0) and result.relative_change == pytest.approx(-0.2)
    assert result.ci is not None and result.ci.low == result.ci.high == pytest.approx(-2.0)
    zero = compare([1.0] * 5, [0.0] * 5, min_n=5)
    assert zero.relative_change is None  # keine Division durch 0


def test_median_statistic():
    result = compare([1.0, 2.0, 100.0, 3.0, 2.0], [1.0, 2.0, 3.0, 2.0, 2.0], stat="median", min_n=5)
    assert result.delta == pytest.approx(0.0)


def test_bootstrap_results_are_cached_and_identical():
    from shotlake.analytics import stats

    a, b = [1.0, 2.0, 4.0, 8.0, 9.0], [2.0, 3.0, 3.5, 5.0]
    stats._ci.cache_clear()
    stats._diff_ci.cache_clear()
    first = stats.bootstrap_ci(a), stats.bootstrap_diff_ci(a, b)
    again = (
        stats.bootstrap_ci(list(a)),
        stats.bootstrap_diff_ci(tuple(a), b),
    )  # anderer Typ, gleiche Werte
    assert first == again and first[0] is again[0] and first[1] is again[1]
    assert stats._ci.cache_info().hits == 1 and stats._diff_ci.cache_info().hits == 1
    assert stats.bootstrap_ci([1.0, 2.0]) is None  # zu wenige Werte: kein Eintrag
