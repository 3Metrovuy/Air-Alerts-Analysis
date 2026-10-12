"""Hourly air raid alert forecast for every Ukrainian oblast, 1-24 hours ahead.

    pip install -r requirements.txt
    python main.py

Downloads the latest volunteer alert data and archived weather forecasts, scores
the model against four baselines on the last 4 weeks (weekly walk-forward), and
writes report.html.
"""
import base64
import io
import json
import time
import urllib.request
from pathlib import Path

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score  # noqa: E402

URL = ("https://raw.githubusercontent.com/Vadimkin/ukrainian-air-raid-sirens-dataset/"
       "main/datasets/volunteer_data_en.csv")
ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "volunteer_data_en.csv"
WEATHER_FILE = ROOT / "data" / "weather.json"
REPORT = ROOT / "report.html"
TZ = "Europe/Kyiv"
HOUR = pd.Timedelta(hours=1)
K = 24                    # leads 1..24 h
WEEK = 168
TRAIN_H = 182 * 24        # 6-month training window (fixed by choice, not tuned)
VAL_H = 2 * WEEK          # validation: 2 weeks before the test
TEST_WEEKS = 4
LOOKBACK = 4 * WEEK       # oldest hour any feature looks at
LONG_ALERT = pd.Timedelta(hours=12)
HISTORY_DAYS = 600        # grid length: 12 robustness blocks + 6 months training + lookback

# Land borders between oblasts (Luhansk dropped: 3 rows in 4 years). Symmetrised below.
BORDERS = """
Cherkaska: Kyivska Poltavska Kirovohradska Vinnytska
Chernihivska: Kyivska Sumska Poltavska
Chernivetska: Ivano-Frankivska Ternopilska Khmelnytska Vinnytska
Dnipropetrovska: Poltavska Kharkivska Donetska Zaporizka Khersonska Mykolaivska Kirovohradska
Donetska: Kharkivska Zaporizka
Ivano-Frankivska: Lvivska Ternopilska Zakarpatska
Kharkivska: Sumska Poltavska
Khersonska: Mykolaivska Zaporizka
Khmelnytska: Rivnenska Zhytomyrska Vinnytska Ternopilska
Kirovohradska: Poltavska Mykolaivska Odeska Vinnytska
Kyiv City: Kyivska
Kyivska: Zhytomyrska Chernihivska Poltavska Vinnytska
Lvivska: Volynska Rivnenska Ternopilska Zakarpatska
Mykolaivska: Odeska
Odeska: Vinnytska
Rivnenska: Volynska Ternopilska Zhytomyrska
Vinnytska: Zhytomyrska
"""

FEATURES = ["lead", "hour", "dow", "oblast", "now", "run_alert", "run_quiet", "last24",
            "nbr_now", "nbr_last24", "nat_now", "nat_last24", "how4", "clim28", "yday"]
# Weather at the oblast centre for the target hour, from the forecast run made 48 h before
# that hour (Open-Meteo previous-runs API). For leads <= 24 h that run is >= 25 h old at
# forecast time, so it was already published: no leakage.
WEATHER = {"cloud": "cloud_cover", "wind": "wind_speed_10m", "precip": "precipitation"}
WX_FEATURES = FEATURES + list(WEATHER)
CENTRES = {  # oblast capital (Kramatorsk for Donetska)
    "Cherkaska": (49.44, 32.06), "Chernihivska": (51.49, 31.29), "Chernivetska": (48.29, 25.94),
    "Dnipropetrovska": (48.46, 35.05), "Donetska": (48.72, 37.56), "Ivano-Frankivska": (48.92, 24.71),
    "Kharkivska": (49.99, 36.23), "Khersonska": (46.64, 32.62), "Khmelnytska": (49.42, 26.99),
    "Kirovohradska": (48.51, 32.26), "Kyiv City": (50.45, 30.52), "Kyivska": (50.45, 30.52),
    "Lvivska": (49.84, 24.03), "Mykolaivska": (46.97, 31.99), "Odeska": (46.48, 30.72),
    "Poltavska": (49.59, 34.55), "Rivnenska": (50.62, 26.25), "Sumska": (50.91, 34.80),
    "Ternopilska": (49.55, 25.59), "Vinnytska": (49.23, 28.47), "Volynska": (50.75, 25.34),
    "Zakarpatska": (48.62, 22.29), "Zaporizka": (47.84, 35.14), "Zhytomyrska": (50.25, 28.66),
}
FEATURE_SETS = {"alerts only": FEATURES, "alerts + weather": WX_FEATURES}
BASELINES = {"hour-of-week": "how4", "28-day same hour": "clim28", "persistence": "now",
             "yesterday": "yday"}


# ---------------------------------------------------------------- data

def download(url, path):
    path.parent.mkdir(exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=180) as r:
            path.write_bytes(r.read())
    except OSError as e:
        if not path.exists():
            raise
        print(f"Download failed ({e}); using cached {path.name}")


def load_weather(g):
    """{name: O x (T+K) array} of archived forecasts, by oblast and UTC hour."""
    O, T = g["Y"].shape
    lat, lon = zip(*(CENTRES[r] for r in g["regions"]))
    url = ("https://previous-runs-api.open-meteo.com/v1/forecast"
           f"?latitude={','.join(map(str, lat))}&longitude={','.join(map(str, lon))}"
           f"&hourly={','.join(v + '_previous_day2' for v in WEATHER.values())}"
           f"&start_date={g['start']:%Y-%m-%d}&end_date={g['end'] + K * HOUR:%Y-%m-%d}&timezone=GMT")
    download(url, WEATHER_FILE)
    out = {n: np.full((O, T + K), np.nan, np.float32) for n in WEATHER}
    for i, loc in enumerate(json.loads(WEATHER_FILE.read_bytes())):
        idx = ((pd.to_datetime(loc["hourly"]["time"], utc=True) - g["start"]) / HOUR).astype(int)
        ok = (idx >= 0) & (idx < T + K)
        for n, v in WEATHER.items():
            out[n][i, idx[ok]] = np.array(loc["hourly"][v + "_previous_day2"], float)[ok]
    return out


def load_alerts(path=DATA_FILE):
    df = pd.read_csv(path)
    df = pd.DataFrame({"region": df.region.str.replace(" oblast", ""),
                       "start": pd.to_datetime(df.started_at, utc=True),
                       "end": pd.to_datetime(df.finished_at, utc=True)})
    df = df[df.region != "Luhanska"]
    # Under 1 minute (incl. zero/negative durations): treated as mistaken announcements.
    return df[df.end - df.start >= pd.Timedelta(minutes=1)].reset_index(drop=True)


def build_grid(alerts, end=None):
    """Hourly UTC grid. Y[o, h]: any alert overlaps hour h. NOW[o, t]: alert active at
    instant t (start of hour t). U[o, h]: hour lies inside a >12h alert (unknown)."""
    end = end if end is not None else alerts.end.max().ceil("h")
    start = (end - pd.Timedelta(days=HISTORY_DAYS)).floor("D")
    regions = sorted(alerts.region.unique())
    O, T = len(regions), int((end - start) / HOUR)
    Y = np.zeros((O, T), np.uint8)
    NOW = np.zeros((O, T + 1), np.uint8)
    U = np.zeros((O, T), bool)
    a = alerts[(alerts.end > start) & (alerts.start < end)]
    o = a.region.map({r: i for i, r in enumerate(regions)}).to_numpy()
    s = ((a.start - start) / HOUR).to_numpy()
    e = ((a.end - start) / HOUR).to_numpy()
    long = (a.end - a.start > LONG_ALERT).to_numpy()
    for oi, si, ei, li in zip(o, s, e, long):
        lo, hi = max(int(np.floor(si)), 0), min(int(np.ceil(ei)), T)
        Y[oi, lo:hi] = 1
        NOW[oi, max(int(np.ceil(si)), 0):min(int(np.ceil(ei)), T + 1)] = 1
        if li:
            U[oi, lo:hi] = True
    # ponytail: >12h spans keep Y=1 (as reported) so run-length features count from the
    # span edge; every row whose target or last 24h touches a span is dropped instead.
    nbr = np.zeros((O, O), bool)
    for line in BORDERS.strip().splitlines():
        a_, rest = line.split(":")
        for b in rest.split():
            i, j = regions.index(a_), regions.index(b)
            nbr[i, j] = nbr[j, i] = True
    local = pd.date_range(start, periods=T + K, freq="h").tz_convert(TZ)
    return dict(start=start, end=end, regions=regions, Y=Y, NOW=NOW, U=U, nbr=nbr,
                hour=local.hour.to_numpy(), dow=local.dayofweek.to_numpy(),
                long=a[long].assign(hours=((a.end - a.start) / HOUR)[long].round(1)))


def origin_features(g):
    """Per (oblast, instant t) features. Only hours < t and the instant t itself are used."""
    Y, NOW, U, nbr = g["Y"], g["NOW"], g["U"], g["nbr"]
    O, T = Y.shape
    t = np.arange(T + 1)
    cs = np.concatenate([np.zeros((O, 1)), Y.cumsum(1)], 1)
    last24 = cs[:, t] - cs[:, np.maximum(t - 24, 0)]
    cu = np.concatenate([np.zeros((O, 1)), U.cumsum(1)], 1)
    bad24 = (cu[:, t] - cu[:, np.maximum(t - 24, 0)]) > 0

    # length of the run of equal Y values ending at hour t-1
    idx = np.broadcast_to(np.arange(T), (O, T))
    change = np.concatenate([np.ones((O, 1), bool), Y[:, 1:] != Y[:, :-1]], 1)
    run = idx - np.maximum.accumulate(np.where(change, idx, 0), axis=1) + 1
    run = np.concatenate([np.zeros((O, 1)), np.minimum(run, WEEK)], 1)   # shift: value at t uses t-1
    prev = np.concatenate([np.zeros((O, 1)), Y], 1)

    deg = nbr.sum(1, keepdims=True)
    return dict(now=NOW, run_alert=run * prev, run_quiet=run * (1 - prev), last24=last24,
                nbr_now=nbr @ NOW / deg, nbr_last24=nbr @ last24 / deg,
                nat_now=np.broadcast_to(NOW.sum(0), NOW.shape),
                nat_last24=np.broadcast_to(last24.mean(0), NOW.shape), bad24=bad24)


def rows(g, f, origins, live=False, feats=FEATURES):
    """Expand origins x oblasts x leads into a feature table. Target hour h = t + lead - 1.
    Rows with unknown labels or masked inputs are dropped (unless live)."""
    Y, U = g["Y"], g["U"]
    O, T = Y.shape
    t = np.asarray(origins)[:, None, None]
    o = np.arange(O)[None, :, None]
    k = np.arange(1, K + 1)[None, None, :]
    t, o, k = np.broadcast_arrays(t, o, k)
    t, o, k = t.ravel(), o.ravel(), k.ravel()
    h = t + k - 1
    hc = np.minimum(h, T - 1)
    def lag_mean(lags):  # mean of Y at h - lag over known hours; every lag >= 24, so h - lag < t
        s = np.zeros(len(h), np.float32)
        n = np.zeros(len(h), np.float32)
        for lag in lags:
            known = ~U[o, h - lag]
            s += Y[o, h - lag] * known
            n += known
        return np.where(n > 0, s / np.maximum(n, 1), np.nan)

    X = pd.DataFrame({
        "lead": k, "hour": g["hour"][h], "dow": g["dow"][h], "oblast": o,
        **{c: f[c][o, t] for c in FEATURES if c in f},
        "how4": lag_mean(WEEK * np.arange(1, 5)),
        "clim28": lag_mean(24 * np.arange(1, 29)),
        "yday": lag_mean([24]),
        **{n: w[o, h] for n, w in g.get("wx", {}).items()},
    })[feats].astype("float32")
    meta = pd.DataFrame({"t": t, "o": o, "h": h, "y": Y[o, hc]})
    if not live:
        keep = (h < T) & ~U[o, hc] & ~f["bad24"][o, t]
        X, meta = X[keep].reset_index(drop=True), meta[keep].reset_index(drop=True)
    for c in ("how4", "clim28"):
        X[c] = X[c].fillna(X.last24 / 24)
    return X, meta


# ---------------------------------------------------------------- model

def fit(g, f, train_end, feats=FEATURES):
    """Train on origins in the 6 months before train_end whose target hour is < train_end."""
    # ponytail: every 2nd origin hour; same validation Brier (0.1798 vs 0.1797), half the fit time
    origins = np.arange(max(train_end - TRAIN_H, LOOKBACK), train_end, 2)
    X, m = rows(g, f, origins, feats=feats)
    keep = m.h < train_end
    model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.1, max_leaf_nodes=63,
                                           categorical_features=[feats.index("oblast")],
                                           early_stopping=False, random_state=0)
    return model.fit(X[keep], m.y[keep])


def predict(g, f, model, origins, feats=FEATURES):
    X, m = rows(g, f, origins, feats=feats)
    m["model"] = model.predict_proba(X)[:, 1]
    for name, col in BASELINES.items():
        m[name] = X[col].to_numpy()
    m["lead"] = X.lead.astype(int).to_numpy()
    return m


def walk_forward(g, f, test_start, weeks, feats=FEATURES):
    """Retrain every week; each fold trains only on labels known before the fold starts."""
    out = []
    for w in range(weeks):
        s = test_start + w * WEEK
        model = fit(g, f, s, feats)
        out.append(predict(g, f, model, np.arange(s, min(s + WEEK, g["Y"].shape[1])), feats))
    return pd.concat(out, ignore_index=True)


def scores(p):
    r = {"rows": len(p), "alert share": p.y.mean()}
    for c in ["model", *BASELINES]:
        r[f"Brier {c}"] = brier_score_loss(p.y, p[c])
    best = min(BASELINES, key=lambda b: r[f"Brier {b}"])
    r["best baseline"] = best
    r["Brier skill"] = 1 - r["Brier model"] / r[f"Brier {best}"]
    for c, tag in (("model", "model"), (best, "best baseline")):
        r[f"ROC-AUC {tag}"] = roc_auc_score(p.y, p[c])
        r[f"PR-AUC {tag}"] = average_precision_score(p.y, p[c])
    return r


def score_table(p, leads=(1, 2, 3, 6, 12, 18, 24)):
    t = {f"{k}h": scores(p[p.lead == k]) for k in leads}
    t["all leads"] = scores(p)
    return pd.DataFrame(t).T


def calibration(p):
    b = pd.cut(p.model, np.linspace(0, 1, 11), include_lowest=True)
    c = p.groupby(b, observed=True).agg(forecast=("model", "mean"), observed=("y", "mean"),
                                        rows=("y", "size"))
    c.index = c.index.astype(str)
    return c


def robustness(g, f, test_start, feats, blocks=12):
    """Same pipeline, test block moved back 4 weeks at a time (one fit per block)."""
    out = []
    for i in range(blocks, 0, -1):
        s = test_start - i * 4 * WEEK
        print(f"  block {blocks - i + 1}/{blocks}")
        r = scores(predict(g, f, fit(g, f, s, feats), np.arange(s, s + 4 * WEEK), feats))
        out.append({"test block": f"{local(g, s):%Y-%m-%d} .. {local(g, s + 4 * WEEK - 1):%Y-%m-%d}", **r})
    return pd.DataFrame(out).set_index("test block")


# ---------------------------------------------------------------- report

COLORS = {"model": "#2a78d6", "hour-of-week": "#eb6834", "28-day same hour": "#1baf7a",
          "persistence": "#eda100", "yesterday": "#e87ba4"}


def local(g, t):
    return (g["start"] + int(t) * HOUR).tz_convert(TZ)


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e4e3df", lw=0.8)
    ax.set_axisbelow(True)


def chart_by_lead(p):
    b = pd.DataFrame({c: p.groupby("lead")[["y", c]].apply(lambda d: brier_score_loss(d.y, d.iloc[:, 1]))
                      for c in COLORS})
    fig, ax = plt.subplots(figsize=(8, 4))
    for c, col in COLORS.items():
        ax.plot(b.index, b[c], color=col, lw=2, label=c)
    ax.set(xlabel="lead (hours after the forecast is made)", ylabel="Brier score (lower is better)",
           xlim=(1, 24), ylim=(0, None))
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1))
    style(ax)
    return png(fig)


def heatmaps(g, panels, t0, title):
    """panels: list of (name, O x K matrix). Columns are local hours starting at origin t0."""
    hours = [f"{local(g, t0 + k):%H}" for k in range(K)]
    fig, axes = plt.subplots(1, len(panels), figsize=(1.5 + 4.4 * len(panels), 6.4), squeeze=False,
                             sharey=True)
    cmap = plt.get_cmap("Blues").copy()
    cmap.set_bad("#d9d8d4")
    for ax, (name, m) in zip(axes[0], panels):
        im = ax.imshow(np.ma.masked_invalid(m), cmap=cmap, vmin=0, vmax=1, aspect="auto")
        ax.set_yticks(range(len(g["regions"])), g["regions"], fontsize=8)
        ax.tick_params(axis="y", labelleft=ax is axes[0][0])
        ax.set_xticks(range(0, K, 2), hours[::2], fontsize=8)
        ax.set_title(name, fontsize=10)
        ax.set_xlabel("local hour (Kyiv)")
    fig.colorbar(im, ax=axes[0].tolist(), shrink=0.6, label="alert probability (forecast) / 0-1 (actual)")
    fig.suptitle(title, fontsize=11)
    return png(fig)


def to_matrix(g, p, col):
    m = np.full((len(g["regions"]), K), np.nan)
    m[p.o.to_numpy(), p.lead.to_numpy() - 1] = p[col].to_numpy()
    return m


def example_days(g, test):
    """Forecasts made at 18:00 Kyiv, picked by rule: busiest, quietest and a random one."""
    at18 = test[np.array([local(g, t).hour == 18 for t in test.t])]
    sev = at18.groupby("t").y.mean()
    picks = {"busiest": sev.idxmax(), "quietest": sev.idxmin(),
             "random (seed 0)": np.random.default_rng(0).choice(sev.index)}
    out = []
    for why, t in picks.items():
        d = at18[at18.t == t]
        out.append(heatmaps(g, [("forecast", to_matrix(g, d, "model")),
                                ("what happened", to_matrix(g, d, "y"))], t,
                            f"Forecast made {local(g, t):%a %Y-%m-%d %H:%M} Kyiv ({why} in the test)"))
    return "\n".join(out)


def fmt(df):
    return df.to_html(float_format=lambda x: f"{x:.3f}", border=0, classes="t")


CSS = """body{font:15px/1.5 system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:16px;
background:#fcfcfb;color:#0b0b0b}h1{font-size:26px}h2{margin-top:2em;border-bottom:1px solid #ddd}
.t{border-collapse:collapse;font-size:13px;margin:8px 0;display:block;overflow-x:auto}
.t td,.t th{padding:3px 8px;text-align:right;border-bottom:1px solid #eee}img{max-width:100%}
.note{color:#52514e}"""


def write_report(g, live, test, val, chosen, rob, dropped, runtime):
    T = g["Y"].shape[1]
    st = score_table(test)
    vt = pd.DataFrame({f"Brier {n}": score_table(p)["Brier model"] for n, p in val.items()})
    v0 = score_table(next(iter(val.values())))
    vt["best baseline"], vt["Brier best baseline"] = v0["best baseline"], [
        v0.loc[i, "Brier " + b] for i, b in v0["best baseline"].items()]
    skills = ", ".join(f"{i} {r['Brier skill']:+.1%} (vs {r['best baseline']})" for i, r in st.iterrows())
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Air Alert Forecast</title>
<style>{CSS}</style></head><body>
<h1>Air raid alert forecast, Ukraine</h1>
<p class="note">Hourly probability that each oblast is under an air raid alert, 1-24 hours ahead.
Data: volunteer (eTryvoga) alerts from the Vadimkin dataset, up to {local(g, T):%Y-%m-%d %H:%M} Kyiv.
Generated by <code>python main.py</code> in {runtime:.0f} s.</p>

<h2>Next 24 hours</h2>
<p>Forecast made at {local(g, T):%Y-%m-%d %H:%M} Kyiv (the end of the data) by a model trained on the
last 6 months. Inputs: {chosen}.</p>
{heatmaps(g, [("forecast", live)], T, "Alert probability by oblast and hour")}

<h2>Does it work? Test on the last 4 weeks</h2>
<p>Test: {local(g, T - TEST_WEEKS * WEEK):%Y-%m-%d} .. {local(g, T - 1):%Y-%m-%d}. A forecast is made every hour
for every oblast; the model is retrained every week on the 6 months before that week, using only answers
known by then.</p>
<p><b>Brier skill against the best baseline at each lead:</b> {skills}.
Skill near 0 means the model is no better than that baseline.</p>
<p class="note"><b>hour-of-week</b>: share of the last 4 weeks with an alert at the same weekday and hour.
<b>28-day same hour</b>: share of the last 28 days with an alert at the same hour.
<b>persistence</b>: "same as now", 1 if an alert is active when the forecast is made.
<b>yesterday</b>: was there an alert at the same hour 24 h earlier.
<b>Brier skill</b> = 1 - Brier(model) / Brier(best baseline at that lead); above 0 means the model wins.</p>
{fmt(st)}
{chart_by_lead(test)}

<h3>Calibration (test)</h3>
<p class="note">When the model says p, an alert should follow about p of the time.</p>
{fmt(calibration(test))}

<h3>Example forecasts (picked by rule, not by hand)</h3>
{example_days(g, test)}

<h2>Robustness: 12 earlier 4-week blocks</h2>
<p class="note">Same pipeline with the test block moved back 4 weeks at a time, one fit per block
(trained on the 6 months before it). Checks that the skill is not the luck of one autumn month.</p>
{fmt(rob[["alert share", "Brier model", "best baseline", "Brier skill", "ROC-AUC model", "ROC-AUC best baseline"]])}

<h2>Validation: which inputs to keep</h2>
<p>The 2 weeks before the test, same weekly retraining. Weather forecasts are kept only if they lower the
validation Brier score. Chosen: <b>{chosen}</b>.</p>
{fmt(vt)}

<h2>Data cleaning</h2>
<ul><li>Luhansk oblast dropped (3 alerts in 4 years). Alerts under 1 minute dropped as mistaken announcements.</li>
<li>Alerts longer than 12 h are treated as missed all-clear messages: every one since Oct 2025 that could be
checked against the official data was an artifact. Their hours are removed: no label, and no forecast whose
last 24 h touches them. Selected by rule; all of them in the data window (UTC):</li></ul>
{fmt(dropped)}

<h2>Limitations</h2>
<ul><li>Volunteer data means "eTryvoga reported an alert", not the official siren system; the two disagree
in the west (Volyn, Lviv).</li>
<li>Inputs are alert history, the calendar and (if chosen above) weather forecasts. No real-time launch signals (bomber take-offs, drone
groups), so the model cannot see a specific attack coming; its skill comes from activity patterns.</li>
<li>The >12 h rule uses hindsight (an alert's length is known only after it ends). It only removes rows; it
never feeds a value into a feature.</li>
<li>The label is "any alert in this hour", so a 1-minute alert and a 6-hour attack count the same.</li></ul>
<p class="note">Leakage check: <code>python test_leakage.py</code> rebuilds every feature from data cut at a
point in time and asserts that nothing before the cut changes.</p>
</body></html>"""
    REPORT.write_text(html, encoding="utf-8")


def main():
    t0 = time.time()
    print("Downloading volunteer alert data and archived weather forecasts ...")
    download(URL, DATA_FILE)
    g = build_grid(load_alerts())
    g["wx"] = load_weather(g)
    f = origin_features(g)
    T = g["Y"].shape[1]
    test_start = T - TEST_WEEKS * WEEK
    print(f"Data to {local(g, T):%Y-%m-%d %H:%M} Kyiv, {len(g['regions'])} oblasts")

    print("Validation: which inputs to keep (2 weekly folds) ...")
    val = {n: walk_forward(g, f, test_start - VAL_H, 2, fs) for n, fs in FEATURE_SETS.items()}
    vb = {n: brier_score_loss(p.y, p.model) for n, p in val.items()}
    chosen = min(vb, key=vb.get)
    feats = FEATURE_SETS[chosen]
    print(f"  validation Brier {vb} -> {chosen}")
    print("Test (4 weekly folds) ...")
    test = walk_forward(g, f, test_start, TEST_WEEKS, feats)
    print(score_table(test)[["Brier model", "best baseline", "Brier skill", "ROC-AUC model"]].to_string())
    print("Robustness (12 blocks) ...")
    rob = robustness(g, f, test_start, feats)
    print(rob[["Brier model", "best baseline", "Brier skill"]].to_string())

    print("Forecast for the next 24 h ...")
    X, m = rows(g, f, [T], live=True, feats=feats)
    m["model"], m["lead"] = fit(g, f, T, feats).predict_proba(X)[:, 1], X.lead.astype(int).to_numpy()
    live = to_matrix(g, m, "model")

    lg = g["long"]
    dropped = lg.assign(start=lg.start.dt.strftime("%Y-%m-%d %H:%M"),
                        end=lg.end.dt.strftime("%Y-%m-%d %H:%M")).set_index("region")
    write_report(g, live, test, val, chosen, rob, dropped, time.time() - t0)
    print(f"Wrote {REPORT.name} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
