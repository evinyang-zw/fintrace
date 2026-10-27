from __future__ import annotations

from eval.bootstrap import paired_delta_ci, percentile_ci


def test_ci_bounds_point_estimate() -> None:
    values = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0]
    low, high = percentile_ci(values)
    mean = sum(values) / len(values)
    assert low <= mean <= high


def test_ci_narrows_with_more_samples() -> None:
    small = [1.0] * 6 + [0.0] * 4
    large = ([1.0] * 60 + [0.0] * 40) * 5
    low_s, high_s = percentile_ci(small)
    low_l, high_l = percentile_ci(large)
    assert (high_l - low_l) < (high_s - low_s)


def test_paired_delta_ci_is_centred_on_delta() -> None:
    baseline = [0.0] * 30
    current = [1.0] * 30
    low, high = paired_delta_ci(baseline, current)
    assert (low, high) == (1.0, 1.0)


def test_paired_requires_same_length() -> None:
    try:
        paired_delta_ci([1.0], [1.0, 0.0])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_single_sample_has_zero_width() -> None:
    assert percentile_ci([0.7]) == (0.7, 0.7)
