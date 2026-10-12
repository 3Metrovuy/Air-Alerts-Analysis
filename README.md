# Air raid alert forecast, Ukraine

For every oblast, the probability of an air raid alert in each of the next 24 hours,
scored against simple baselines on the latest 4 weeks of data with no leakage.

```
pip install -r requirements.txt
python main.py           # about 8 minutes on a 4-core laptop, writes report.html
python test_leakage.py   # about 15 seconds
```

`main.py` downloads the latest alert data itself. **Open `report.html`** (already committed) to see the
results without running anything.

## Results (data up to 2026-10-12 08:00 Kyiv)

Test: the last 4 weeks (2026-09-14 .. 2026-10-12). A forecast is made every hour for 24 oblasts and
leads of 1-24 h; the model is retrained every week on the 6 months before that week.

| lead | Brier model | best baseline (28-day same hour) | Brier skill | ROC-AUC model | ROC-AUC baseline |
|---|---|---|---|---|---|
| 1 h | 0.137 | 0.184 | **+25.5%** | 0.844 | 0.750 |
| 2 h | 0.183 | 0.184 | +0.8% | 0.756 | 0.751 |
| 6 h | 0.183 | 0.184 | +0.6% | 0.755 | 0.750 |
| 12 h | 0.184 | 0.184 | +0.4% | 0.754 | 0.751 |
| 24 h | 0.185 | 0.185 | +0.1% | 0.751 | 0.750 |

What this means:

- **1 hour ahead the model is clearly useful.** It knows whether an alert is running now, how long it
  has run, and what neighbouring oblasts and the whole country are doing.
- **From 2 hours ahead it is only as good as "the share of the last 28 days with an alert at this
  hour".** Alert history cannot see a specific attack coming hours in advance; what is left is the
  oblast's recent rhythm (Dnipropetrovsk ~75% of hours, Zakarpattia ~0%).
- The small edge holds in all 12 earlier 4-week blocks (Oct 2025 .. Sep 2026, +0.9% to +3.4% over all
  leads), so it is not the luck of one month.
- Weather forecasts (cloud, wind, precipitation) made validation worse (Brier 0.1791 vs 0.1777), so the
  rule dropped them.

An earlier version of this README would have claimed +16% skill at every lead: that was against a
noisy 4-sample "hour-of-week" baseline. Adding the steadier 28-day baseline showed the long-lead skill
was a smoother base rate, not foresight. Both baselines stay in the report.

## How it works

- **Data:** volunteer (eTryvoga) alerts from the
  [Vadimkin dataset](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset), re-downloaded on
  every run. The "official" file in the same repo stopped updating on 2026-09-07.
- **Label:** an oblast-hour is 1 if any alert overlaps it. Hours are on a UTC grid; the hour-of-day
  and weekday inputs use Kyiv local time, so daylight saving does not blur them.
- **Inputs at forecast time t** (only what was known at t): alert active now, length of the current
  alert or quiet spell, alert hours in the last 24 h, the same for neighbouring oblasts and for the
  whole country, target local hour, weekday, lead, the oblast, and the 4-week hour-of-week, 28-day
  same-hour and "yesterday" rates for the target hour.
- **Model:** one scikit-learn `HistGradientBoostingClassifier` for all oblasts and leads.
- **Baselines:** hour-of-week (4 weeks), 28-day same hour, persistence ("same as now"), yesterday.
- **Split:** 6 months training, 2 weeks validation (used only to choose the inputs), 4 test weeks with
  weekly retraining. A training row is used only if its answer was known before the test week began.
- **Leakage test:** `test_leakage.py` cuts the data at 25 random moments, keeps only what was known
  then (alerts that had started; running ones still open), rebuilds every feature and asserts that
  nothing changes. It fails if, for example, the "yesterday" input looks 23 h back instead of 24 h.
  Weather uses the forecast run made 48 h before the target hour (Open-Meteo previous-runs API), so it
  was published before the forecast time for every lead up to 24 h.

## Data cleaning

- Luhansk oblast is dropped (3 alerts in 4 years). Alerts under 1 minute are dropped as mistaken
  announcements.
- Alerts longer than 12 h are missed all-clear messages: every checkable one since Oct 2025 is
  contradicted by the official data (it shows 0-31% of those hours under alert). Their hours are
  removed: no label, and no forecast whose last 24 h touches them. They are selected by rule and
  listed in the report.

## Limitations

- The volunteer data means "eTryvoga reported an alert", not the official siren system. The two
  disagree in the west (Volyn, Lviv).
- No real-time launch signals (bomber take-offs, drone groups over the border), which is where
  hours-ahead skill would have to come from.
- The >12 h rule uses hindsight (an alert's length is known only after it ends). It only removes rows.
- A 1-minute alert and a 6-hour attack count the same.

## Files

- `main.py`: data, features, model, evaluation, report.
- `test_leakage.py`: the leakage test.
- `report.html`: the output of the last run.
- `AI-log.md`: the exported AI conversation logs.
