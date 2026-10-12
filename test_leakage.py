"""Fails if any model input uses data from after the moment the forecast is made.

    python test_leakage.py

Cuts the alert data at random moments (keeping only what was known then: alerts that
had started, with running ones still open), rebuilds every feature from the cut data,
and checks that the features of forecasts made up to the cut, and the training labels
before it, are identical to the ones built from the full data.
"""
import numpy as np
import pandas as pd

from main import HOUR, LONG_ALERT, LOOKBACK, build_grid, load_alerts, origin_features, rows


def known_at(alerts, at):
    a = alerts[alerts.start < at].copy()
    a.loc[a.end > at, "end"] = at + pd.Timedelta(minutes=1)  # still running: end unknown
    return a


def main():
    alerts = load_alerts()
    g = build_grid(alerts)
    f = origin_features(g)
    T = g["Y"].shape[1]
    checked = 0
    for c in np.random.default_rng(0).integers(LOOKBACK + 48, T - 24, 25):
        at = g["start"] + int(c) * HOUR
        if ((alerts.start < at) & (alerts.end > at) & (alerts.end - alerts.start > LONG_ALERT)).any():
            continue  # the >12h cleaning rule uses hindsight by design (see README)
        g2 = build_grid(known_at(alerts, at), end=g["end"])
        f2 = origin_features(g2)
        origins = np.arange(c - 48, c + 1)
        X1, _ = rows(g, f, origins, live=True)
        X2, _ = rows(g2, f2, origins, live=True)
        pd.testing.assert_frame_equal(X1, X2)
        (X1, m1), (X2, m2) = rows(g, f, origins), rows(g2, f2, origins)
        k1, k2 = m1.h < c, m2.h < c
        pd.testing.assert_frame_equal(m1[k1].reset_index(drop=True), m2[k2].reset_index(drop=True))
        checked += 1
    assert checked >= 10, checked
    print(f"OK: features and labels unchanged without future data at {checked} cut points")


if __name__ == "__main__":
    main()
