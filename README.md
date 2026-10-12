# Air raid alert forecast, Ukraine

For every oblast and each 30-minute slot of the next 24 hours: the chance that an air raid alert sounds
at some point during that slot (even a 5-minute alert counts). Scored against simple baselines on the
latest 4 weeks of data, with no leakage.

```
pip install -r requirements.txt
python main.py           # about 15 minutes on a 4-core laptop, writes report.html
python test_leakage.py   # under a minute
```

`main.py` downloads the latest alert data itself. **Open `report.html`** (committed) to see the results
without running anything. The grid step is one constant, `SLOT_MIN` in `main.py`: set it to 60 for the
hourly version (about 9 minutes).

## Results (data up to 2026-10-12 08:00 Kyiv)

Test: the last 4 weeks (2026-09-14 .. 2026-10-12). A forecast is made every hour for 24 oblasts and
every 30-minute slot up to 24 h ahead; the model is retrained every week on the 6 months before that
week. "Brier skill" is how much smaller the model's error is than the **28-day habit** (the share of
the last 28 days with an alert in that slot of the day); 0% means no better. "N h ahead" is the slot
that ends N hours after the forecast is made.

| forecast (all 24 oblasts) | Brier skill vs 28-day habit | ROC-AUC model | ROC-AUC 28-day habit |
|---|---|---|---|
| next 30 min, no alert running at forecast time | **+10.6%** | 0.746 | 0.732 |
| next 30 min, alert already running (free, see below) | +97.0% | - | - |
| 2 h ahead | +1.7% | 0.727 | 0.715 |
| 3 h ahead | +1.6% | 0.727 | 0.715 |
| 6 h ahead | +1.3% | 0.725 | 0.715 |
| 12 h ahead | +1.1% | 0.722 | 0.714 |
| 24 h ahead | +1.0% | 0.720 | 0.713 |

Four oblasts from low to high alert pressure. "Right" = the forecast says "alert" at 50% or more and
that matched what happened; the 28-day habit says "alert" if most of the last 28 days had one in that slot.

| oblast | slots with an alert | time under alert | next 30 min, no alert running: model right / habit right / skill | 3 h ahead: model right / habit right / skill |
|---|---|---|---|---|
| Lviv | 15% | 7% | 91% / 91% / +16.7% | 85% / 84% / +5.6% |
| Kyiv City | 19% | 10% | 90% / 90% / +10.9% | 81% / 81% / +2.4% |
| Odesa | 27% | 11% | 81% / 80% / +9.3% | 74% / 73% / +2.3% |
| Kharkiv | 55% | 26% | 61% / 57% / +2.3% | 53% / 57% / +1.8% |

What this means:

- **Next 30 minutes, when no alert is running, the model is clearly better than the oblast's habit**
  (+10.6% Brier skill). On plain right/wrong it rarely beats the habit: for a quiet slot neither gets
  above 50% in most oblasts, so the gain is in the odds, not in yes/no calls.
- **From 2 hours ahead it is only a little better than the 28-day habit** (+1.0% to +1.7%). Alert
  history cannot see a specific attack coming hours in advance.
- **Kharkiv is the weak spot:** the smallest gain, and at 3 h its yes/no calls are worse than the habit's.
- The overall ROC-AUC (0.73) is high mostly because the model tells busy oblasts from quiet ones.
  Inside one oblast the ranking is weak (median 0.56 over 24 oblasts at 3 h; 0.5 is a coin flip).
- The odds are mostly honest: when it says 50-60%, an alert followed 54% of the time; at 70-80% it
  followed 68%.
- The small edge holds in all 12 earlier 4-week blocks (Oct 2025 .. Sep 2026: +0.8% to +3.4% over all
  leads), so it is not the luck of one month.

### Hourly vs 30-minute grid

The first version used hourly slots. Alerts are short (median 12-30 min in these oblasts), so "any alert
in the hour" overstates time under alert about 3x; 30-minute slots cut that to about 2x. Same pipeline,
same test weeks, only `SLOT_MIN` changed:

| | hourly | 30 min |
|---|---|---|
| Kharkiv: slots with an alert (siren actually on 26% of the time) | 74% | 55% |
| next slot, no alert running: skill vs 28-day habit | +4.8% | +10.6% |
| 2-24 h ahead: skill vs 28-day habit | +0.1% to +0.8% | +1.0% to +1.7% |
| Kharkiv 3 h ahead: skill | -1.3% | +1.8% |
| ROC-AUC at 3 h, all oblasts | 0.755 | 0.727 |
| 12 earlier blocks, all leads | +0.9% to +3.4% | +0.8% to +3.4% |
| run time | 9 min | 15 min |

The two grids answer different questions ("any alert in this hour" vs "in this half hour"), so this is
not a head-to-head score. Against the same kind of baseline on its own grid, the 30-minute model gains
more, which fits the idea that finer labels give the model better information. Part of the gain may
also come from the 28-day habit being noisier on half-hour slots. Finer slots are harder to rank, so
ROC-AUC drops. The hourly version is commit `56f155c`.

How the numbers got honest (both found during the work and kept in the log):

1. The first version showed +16% skill at every lead, against a noisy 4-sample hour-of-week baseline.
   The steadier 28-day habit showed that the long-lead "skill" was a smoother base rate, not foresight.
2. The next version showed +25% skill one hour ahead. Most of it is free: the next slot starts at the
   moment the forecast is made, so if an alert is already running, that slot has an alert by definition.
   The report now splits it out and only counts forecasts made while no alert was running.

## How it works

- **Data:** volunteer (eTryvoga) alerts from the
  [Vadimkin dataset](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset), re-downloaded on
  every run. The "official" file in the same repo stopped updating on 2026-09-07.
- **Label:** an oblast-slot (30 min) is 1 if any alert overlaps it. The grid is UTC; the hour-of-day and weekday
  inputs use Kyiv local time, so daylight saving does not blur them. The grid step is one constant
  (`SLOT_MIN` in `main.py`). Forecasts are issued every hour.
- **Inputs at forecast time t** (only what was known at t): alert running now, length of the current
  alert or quiet spell, alert slots in the last 24 h, the same for neighbouring oblasts and for the
  whole country, target local time of day, weekday, lead, the oblast, and the 4-week hour-of-week, 28-day
  and "yesterday" rates for the target slot.
- **Model:** one scikit-learn `HistGradientBoostingClassifier` for all oblasts and leads.
- **Baselines:** 28-day habit, hour-of-week (4 weeks), persistence ("same as now"), yesterday.
- **Split:** 6 months training, 2 weeks validation (used only to choose the inputs), 4 test weeks with
  weekly retraining. A training row is used only if its answer was known before the test week began.
- **Leakage test:** `test_leakage.py` cuts the data at 25 random moments, keeps only what was known
  then (alerts that had started; running ones still open), rebuilds every input and asserts that
  nothing changes. It fails if, for example, the "yesterday" input looks 23 h back instead of 24 h.
- **Weather:** archived forecasts (cloud, wind, precipitation) from the Open-Meteo previous-runs API,
  from the run made 48 h before the target hour, so they were published before the forecast time.
  Validation decides whether to keep them; it dropped them (validation Brier 0.1539 vs 0.1538).

## Data cleaning

- Luhansk oblast is dropped (3 alerts in 4 years). Alerts under 1 minute are dropped as mistaken
  announcements.
- Alerts longer than 12 h are missed all-clear messages: every checkable one since Oct 2025 is
  contradicted by the official data (it shows 0-31% of those hours under alert). Their slots get no
  label and no forecast whose last 24 h touches them. They are selected by rule and listed in the
  report.

## Limitations

- The volunteer data means "eTryvoga reported an alert", not the official siren system. The two
  disagree in the west (Volyn, Lviv).
- No real-time launch signals (bomber take-offs, drone groups over the border), which is where
  hours-ahead skill would have to come from.
- The label is "any alert in the slot", not time under alert. Alerts are short (median 12-30 min),
  so Kharkiv had an alert in 55% of 30-minute slots but the siren was on only 26% of the time.
- The history inputs are built from the same slot label, so a 5-minute alert enters them as a whole
  30-minute slot.
- The >12 h rule uses hindsight (an alert's length is known only after it ends). It only removes rows.

## Files

- `main.py`: data, features, model, evaluation, report.
- `test_leakage.py`: the leakage test.
- `report.html`: the output of the last run.
- `AI-log.md`: the exported AI conversation logs.
