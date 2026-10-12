# Air raid alert forecast, Ukraine

For every oblast and each hour of the next 24 hours: the chance that an air raid alert sounds at some
point during that hour (even a 5-minute alert counts). Scored against simple baselines on the latest
4 weeks of data, with no leakage.

```
pip install -r requirements.txt
python main.py           # about 9 minutes on a 4-core laptop, writes report.html
python test_leakage.py   # under a minute
```

`main.py` downloads the latest alert data itself. **Open `report.html`** (committed) to see the results
without running anything.

## Results (data up to 2026-10-12 08:00 Kyiv)

Test: the last 4 weeks (2026-09-14 .. 2026-10-12). A forecast is made every hour for 24 oblasts and every hour up
to 24 h ahead; the model is retrained every week on the 6 months before that week. "Brier skill" is how
much smaller the model's error is than the **28-day habit** (the share of the last 28 days with an
alert in that hour of the day); 0% means no better.

| forecast (all 24 oblasts) | Brier skill vs 28-day habit | ROC-AUC model | ROC-AUC 28-day habit |
|---|---|---|---|
| next hour, no alert running at forecast time | **+4.8%** | 0.759 | 0.752 |
| next hour, alert already running (free, see below) | +96.4% | - | - |
| 2 h ahead | +0.8% | 0.756 | 0.751 |
| 3 h ahead | +0.7% | 0.755 | 0.751 |
| 6 h ahead | +0.6% | 0.755 | 0.750 |
| 12 h ahead | +0.4% | 0.754 | 0.751 |
| 24 h ahead | +0.1% | 0.751 | 0.750 |

Four oblasts from low to high alert pressure. "Right" = the forecast says "alert" at 50% or more and
that matched what happened; the 28-day habit says "alert" if most of the last 28 days had one in that hour.

| oblast | hours with an alert | time under alert | next hour, no alert running: model right / habit right / skill | 3 h ahead: model right / habit right / skill |
|---|---|---|---|---|
| Lviv | 22% | 7% | 83% / 82% / +11.1% | 77% / 77% / +3.8% |
| Kyiv City | 28% | 10% | 79% / 79% / +3.8% | 71% / 71% / +0.5% |
| Odesa | 41% | 11% | 66% / 60% / +6.3% | 58% / 57% / +2.9% |
| Kharkiv | 74% | 26% | 63% / 67% / -0.7% | 73% / 74% / -1.3% |

What this means:

- **Next hour, when no alert is running, the model is a little better than the oblast's habit**
  (+4.8% Brier skill). On plain right/wrong it rarely beats the habit: for a quiet hour neither gets
  above 50% in most oblasts, so the gain is in the odds, not in yes/no calls.
- **From 2 hours ahead it is about as good as the 28-day habit** (+0.1% to +0.8%). Alert history
  cannot see a specific attack coming hours in advance.
- **Kharkiv is a weak spot:** slightly worse than the habit at every lead shown.
- The overall ROC-AUC (0.76) is high mostly because the model tells busy oblasts from quiet ones.
  Inside one oblast the ranking is weak (median 0.57 over 24 oblasts at 3 h, 0.5 is a coin flip).
- The odds are honest: when it says 70-80%, an alert followed 77% of the time.
- The small edge holds in all 12 earlier 4-week blocks (Oct 2025 .. Sep 2026: +0.9% to +3.4% over all
  leads), so it is not the luck of one month.

How the numbers got honest (both found during the work and kept in the log):

1. The first version showed +16% skill at every lead, against a noisy 4-sample hour-of-week baseline.
   The steadier 28-day habit showed that the long-lead "skill" was a smoother base rate, not foresight.
2. The next version showed +25% skill one hour ahead. Most of it is free: the next hour starts at the
   moment the forecast is made, so if an alert is already running, that hour has an alert by definition.
   The report now splits it out and only counts forecasts made while no alert was running.

## How it works

- **Data:** volunteer (eTryvoga) alerts from the
  [Vadimkin dataset](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset), re-downloaded on
  every run. The "official" file in the same repo stopped updating on 2026-09-07.
- **Label:** an oblast-hour is 1 if any alert overlaps it. The grid is UTC; the hour-of-day and weekday
  inputs use Kyiv local time, so daylight saving does not blur them. The grid step is one constant
  (`SLOT_MIN` in `main.py`).
- **Inputs at forecast time t** (only what was known at t): alert running now, length of the current
  alert or quiet spell, alert hours in the last 24 h, the same for neighbouring oblasts and for the
  whole country, target local hour, weekday, lead, the oblast, and the 4-week hour-of-week, 28-day and
  "yesterday" rates for the target hour.
- **Model:** one scikit-learn `HistGradientBoostingClassifier` for all oblasts and leads.
- **Baselines:** 28-day habit, hour-of-week (4 weeks), persistence ("same as now"), yesterday.
- **Split:** 6 months training, 2 weeks validation (used only to choose the inputs), 4 test weeks with
  weekly retraining. A training row is used only if its answer was known before the test week began.
- **Leakage test:** `test_leakage.py` cuts the data at 25 random moments, keeps only what was known
  then (alerts that had started; running ones still open), rebuilds every input and asserts that
  nothing changes. It fails if, for example, the "yesterday" input looks 23 h back instead of 24 h.
- **Weather:** archived forecasts (cloud, wind, precipitation) from the Open-Meteo previous-runs API,
  from the run made 48 h before the target hour, so they were published before the forecast time.
  Validation decides whether to keep them; it dropped them (validation Brier 0.1791 vs 0.1777).

## Data cleaning

- Luhansk oblast is dropped (3 alerts in 4 years). Alerts under 1 minute are dropped as mistaken
  announcements.
- Alerts longer than 12 h are missed all-clear messages: every checkable one since Oct 2025 is
  contradicted by the official data (it shows 0-31% of those hours under alert). Their hours get no
  label and no forecast whose last 24 h touches them. They are selected by rule and listed in the
  report.

## Limitations

- The volunteer data means "eTryvoga reported an alert", not the official siren system. The two
  disagree in the west (Volyn, Lviv).
- No real-time launch signals (bomber take-offs, drone groups over the border), which is where
  hours-ahead skill would have to come from.
- The label is "any alert in the hour", not time under alert. Alerts are short (median about 15-30
  min), so Kharkiv had an alert in 74% of hours but the siren was on only 26% of the time.
- The history inputs are built from the same hourly label, so a 5-minute alert enters them as a whole
  hour.
- The >12 h rule uses hindsight (an alert's length is known only after it ends). It only removes rows.

## Files

- `main.py`: data, features, model, evaluation, report.
- `test_leakage.py`: the leakage test.
- `report.html`: the output of the last run.
- `AI-log.md`: the exported AI conversation logs.
