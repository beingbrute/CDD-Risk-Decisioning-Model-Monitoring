"""Calculations used by the model and the monitoring."""
import numpy as np
import pytest

from monitor import psi, psi_status
from train_model import at_threshold, threshold_for_recall


def test_psi_is_zero_for_identical_data():
    x = np.random.default_rng(1).normal(size=5000)
    assert psi(x, x) == pytest.approx(0, abs=1e-9)


def test_psi_is_large_for_a_shifted_distribution():
    rng = np.random.default_rng(1)
    base, shifted = rng.normal(size=5000), rng.normal(loc=1.0, size=5000)
    assert psi(base, shifted) > 0.25


def test_psi_ignores_missing_values():
    rng = np.random.default_rng(1)
    x = rng.normal(size=1000)
    with_nan = np.concatenate([x, [np.nan] * 50])
    assert psi(x, with_nan) == pytest.approx(0, abs=1e-9)


@pytest.mark.parametrize("value,status", [(0.05, "stable"), (0.10, "stable"), (0.11, "investigate"),
                                          (0.25, "investigate"), (0.26, "act")])
def test_psi_bands(value, status):
    assert psi_status(value) == status


def test_threshold_reaches_target_recall_with_least_workload():
    y = np.array([1, 0, 1, 0, 0, 1, 0, 0, 0, 0])
    s = np.array([.9, .8, .7, .6, .5, .4, .3, .2, .1, .05])
    thr = threshold_for_recall(y, s, 2 / 3)
    assert thr == 0.7                      # top 3 cases catch 2 of 3 escalations
    r = at_threshold(y, s, thr)
    assert r["recall"] == pytest.approx(0.667, abs=1e-3) and r["cases_flagged"] == 3
