"""Air raid alert forecast for every Ukrainian oblast, up to 24 hours ahead.

    pip install -r requirements.txt
    python main.py

Downloads the latest volunteer alert data and archived weather forecasts, scores
the model against simple baselines on the last 4 weeks (weekly walk-forward), and
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
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402
from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: E402
from sklearn.metrics import brier_score_loss, roc_auc_score  # noqa: E402

URL = ("https://raw.githubusercontent.com/Vadimkin/ukrainian-air-raid-sirens-dataset/"
       "main/datasets/volunteer_data_en.csv")
ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data" / "volunteer_data_en.csv"
WEATHER_FILE = ROOT / "data" / "weather.json"
REPORT = ROOT / "report.html"
TZ = "Europe/Kyiv"
SLOT_MIN = 60             # grid step in minutes: the target is "any alert in this slot"
SLOT = pd.Timedelta(minutes=SLOT_MIN)
SLOT_NAME = "hour" if SLOT_MIN == 60 else f"{SLOT_MIN}-minute slot"
PER_H = 60 // SLOT_MIN    # slots per hour
DAY = 24 * PER_H
WEEK = 7 * DAY
K = DAY                   # leads 1..K slots, i.e. up to 24 h ahead
TRAIN = 182 * DAY         # 6-month training window (fixed by choice, not tuned)
VAL = 2 * WEEK            # validation: 2 weeks before the test
TEST_WEEKS = 4
LOOKBACK = 4 * WEEK       # oldest slot any feature looks at
LONG_ALERT = pd.Timedelta(hours=12)
HISTORY_DAYS = 600        # grid length: 12 robustness blocks + 6 months training + lookback
SHOWN = ["Lvivska", "Kyiv City", "Odeska", "Kharkivska"]   # report cards, low to high alert pressure

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
BASE = "28-day habit"     # the baseline every skill number is measured against
BASELINES = {BASE: "clim28", "hour-of-week": "how4", "persistence": "now", "yesterday": "yday"}


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
    """{name: O x (T+K) array} of archived forecasts, by oblast and grid slot."""
    O, T = g["Y"].shape
    lat, lon = zip(*(CENTRES[r] for r in g["regions"]))
    url = ("https://previous-runs-api.open-meteo.com/v1/forecast"
           f"?latitude={','.join(map(str, lat))}&longitude={','.join(map(str, lon))}"
           f"&hourly={','.join(v + '_previous_day2' for v in WEATHER.values())}"
           f"&start_date={g['start']:%Y-%m-%d}&end_date={g['end'] + K * SLOT:%Y-%m-%d}&timezone=GMT")
    download(url, WEATHER_FILE)
    out = {n: np.full((O, T + K), np.nan, np.float32) for n in WEATHER}
    for i, loc in enumerate(json.loads(WEATHER_FILE.read_bytes())):
        idx = ((pd.to_datetime(loc["hourly"]["time"], utc=True) - g["start"]) / SLOT).astype(int)
        for n, v in WEATHER.items():
            vals = np.array(loc["hourly"][v + "_previous_day2"], float)
            for j in range(PER_H):  # an hourly value fills every slot of its hour
                ok = (idx + j >= 0) & (idx + j < T + K)
                out[n][i, idx[ok] + j] = vals[ok]
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
    """UTC grid of SLOT steps. Y[o, h]: any alert overlaps slot h. NOW[o, t]: alert active at
    instant t (start of slot t). U[o, h]: slot lies inside a >12h alert (unknown)."""
    end = end if end is not None else alerts.end.max().ceil(f"{SLOT_MIN}min")
    start = (end - pd.Timedelta(days=HISTORY_DAYS)).floor("D")
    regions = sorted(alerts.region.unique())
    O, T = len(regions), int((end - start) / SLOT)
    Y = np.zeros((O, T), np.uint8)
    NOW = np.zeros((O, T + 1), np.uint8)
    U = np.zeros((O, T), bool)
    a = alerts[(alerts.end > start) & (alerts.start < end)]
    o = a.region.map({r: i for i, r in enumerate(regions)}).to_numpy()
    s = ((a.start - start) / SLOT).to_numpy()
    e = ((a.end - start) / SLOT).to_numpy()
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
    local = pd.date_range(start, periods=T + K, freq=SLOT).tz_convert(TZ)
    return dict(start=start, end=end, regions=regions, Y=Y, NOW=NOW, U=U, nbr=nbr,
                hour=(local.hour + local.minute / 60).to_numpy(), dow=local.dayofweek.to_numpy(),
                long=a[long].assign(hours=((a.end - a.start) / pd.Timedelta(hours=1))[long].round(1)))


def origin_features(g):
    """Per (oblast, instant t) features. Only slots < t and the instant t itself are used."""
    Y, NOW, U, nbr = g["Y"], g["NOW"], g["U"], g["nbr"]
    O, T = Y.shape
    t = np.arange(T + 1)
    cs = np.concatenate([np.zeros((O, 1)), Y.cumsum(1)], 1)
    last24 = cs[:, t] - cs[:, np.maximum(t - DAY, 0)]
    cu = np.concatenate([np.zeros((O, 1)), U.cumsum(1)], 1)
    bad24 = (cu[:, t] - cu[:, np.maximum(t - DAY, 0)]) > 0

    # length of the run of equal Y values ending at slot t-1
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
    """Expand origins x oblasts x leads into a feature table. Target slot h = t + lead - 1.
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
    def lag_mean(lags):  # mean of Y at h - lag over known slots; every lag >= DAY >= K, so h - lag < t
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
        "clim28": lag_mean(DAY * np.arange(1, 29)),
        "yday": lag_mean([DAY]),
        **{n: w[o, h] for n, w in g.get("wx", {}).items()},
    })[feats].astype("float32")
    meta = pd.DataFrame({"t": t, "o": o, "h": h, "y": Y[o, hc]})
    if not live:
        keep = (h < T) & ~U[o, hc] & ~f["bad24"][o, t]
        X, meta = X[keep].reset_index(drop=True), meta[keep].reset_index(drop=True)
    for c in ("how4", "clim28"):
        X[c] = X[c].fillna(X.last24 / DAY)
    return X, meta


# ---------------------------------------------------------------- model

def fit(g, f, train_end, feats=FEATURES):
    """Train on origins in the 6 months before train_end whose target slot is < train_end."""
    # ponytail: an origin every 2nd hour; same validation Brier (0.1798 vs 0.1797), half the fit time
    origins = np.arange(max(train_end - TRAIN, LOOKBACK), train_end, 2 * PER_H)
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
    m["lead"], m["now"] = X.lead.astype(int).to_numpy(), X.now.to_numpy()
    return m


def walk_forward(g, f, test_start, weeks, feats=FEATURES):
    """Retrain every week; each fold trains only on labels known before the fold starts.
    A forecast is issued every hour (every PER_H slots)."""
    out = []
    for w in range(weeks):
        s = test_start + w * WEEK
        model = fit(g, f, s, feats)
        out.append(predict(g, f, model, np.arange(s, min(s + WEEK, g["Y"].shape[1]), PER_H), feats))
    return pd.concat(out, ignore_index=True)


def auc(y, s):
    return roc_auc_score(y, s) if y.nunique() == 2 else np.nan


def skill(p):
    return 1 - brier_score_loss(p.y, p.model) / brier_score_loss(p.y, p[BASE])


def scores(p):
    r = {"rows": len(p), "alert share": p.y.mean()}
    for c in ["model", *BASELINES]:
        r[f"Brier {c}"] = brier_score_loss(p.y, p[c])
    r["Brier skill"] = skill(p)
    r["ROC-AUC model"], r[f"ROC-AUC {BASE}"] = auc(p.y, p.model), auc(p.y, p[BASE])
    return r


def ahead(p, hrs):
    """Forecasts whose target slot ends `hrs` hours after the forecast is made."""
    return p[p.lead == hrs * PER_H]


def by_lead(p):
    q = p[p.lead == 1]
    t = {f"next {SLOT_NAME}, all": scores(q),
         f"next {SLOT_NAME}, alert already running": scores(q[q.now == 1]),
         f"next {SLOT_NAME}, no alert running": scores(q[q.now == 0])}
    t |= {f"{hrs} h ahead": scores(ahead(p, hrs)) for hrs in (2, 3, 6, 12, 24)}
    t["all leads"] = scores(p)
    return pd.DataFrame(t).T


def right_wrong(p):
    """Card metrics at a 50% threshold, against the 28-day habit."""
    y = p.y.to_numpy() == 1
    m, b = p.model.to_numpy() >= 0.5, p[BASE].to_numpy() >= 0.5
    pct = lambda x: "—" if np.isnan(x) else f"{x:.0%}"  # noqa: E731
    return {f"{SLOT_NAME}s with an alert": pct(y.mean()),
            "model right": pct((m == y).mean()),
            "28-day habit right": pct((b == y).mean()),
            "alerts flagged": pct(m[y].mean() if y.any() else np.nan),
            "flags that came true": pct(y[m].mean() if m.any() else np.nan),
            "Brier skill": f"{skill(p):+.1%}",
            "ROC-AUC model": f"{auc(p.y, p.model):.2f}",
            "ROC-AUC 28-day habit": f"{auc(p.y, p[BASE]):.2f}"}


def calibration(p):
    b = pd.cut(p.model, np.linspace(0, 1, 11), include_lowest=True)
    c = p.groupby(b, observed=True).agg(**{"model said (average)": ("model", "mean"),
                                           "alert followed": ("y", "mean"), "forecasts": ("y", "size")})
    c.index = c.index.astype(str).rename("model said (range)")
    return c


def robustness(g, f, test_start, feats, blocks=12):
    """Same pipeline, test block moved back 4 weeks at a time (one fit per block)."""
    out = []
    for i in range(blocks, 0, -1):
        s = test_start - i * 4 * WEEK
        print(f"  block {blocks - i + 1}/{blocks}")
        r = scores(predict(g, f, fit(g, f, s, feats), np.arange(s, s + 4 * WEEK, PER_H), feats))
        out.append({"test block": f"{local(g, s):%Y-%m-%d} .. {local(g, s + 4 * WEEK - 1):%Y-%m-%d}", **r})
    return pd.DataFrame(out).set_index("test block")


def time_under_alert(alerts, region, a, b):
    """Share of minutes in [a, b) with an alert running, and the median alert length (min)."""
    minute = pd.Timedelta(minutes=1)
    x = alerts[(alerts.region == region) & (alerts.end > a) & (alerts.start < b)
               & (alerts.end - alerts.start <= LONG_ALERT)]
    on = np.zeros(int((b - a) / minute), bool)
    for s, e in zip((x.start - a) / minute, (x.end - a) / minute):
        on[max(int(s), 0):max(int(np.ceil(e)), 0)] = True
    return on.mean(), ((x.end - x.start) / minute).median()


# ---------------------------------------------------------------- report

BLUE, ORANGE, GREEN, GREY = "#2a78d6", "#eb6834", "#1baf7a", "#d9d8d4"


def local(g, t):
    return (g["start"] + int(t) * SLOT).tz_convert(TZ)


def png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return f'<img src="data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}">'


def style(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#e4e3df", lw=0.8)
    ax.set_axisbelow(True)


def skill_chart(p):
    s = pd.Series({k: skill(d) for k, d in p.groupby("lead")})
    quiet = p[(p.lead == 1) & (p.now == 0)]
    fig, ax = plt.subplots(figsize=(8, 3.6))
    ax.axhline(0, color="#888", lw=1)
    ax.plot(s.index / PER_H, s.values, color=BLUE, lw=2, label="all forecasts")
    ax.plot([1 / PER_H], [skill(quiet)], "o", color=ORANGE, ms=8,
            label=f"next {SLOT_NAME}, only when no alert is running at forecast time")
    ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
    ax.set(xlabel="hours ahead (end of the target slot)", ylabel=f"Brier skill vs {BASE}", xlim=(0, 24))
    ax.legend(frameon=False)
    style(ax)
    return png(fig)


def week_chart(g, p, o):
    """Last test week of one oblast: slots with an alert (grey) and the forecasts for each slot
    made up to 1 h and 6 h before it. Origins are hourly, so each slot has one forecast per band."""
    d = p[p.o == o]
    h = np.arange(d.h.max() - WEEK + 1, d.h.max() + 1)
    x = (g["start"] + pd.to_timedelta(h * SLOT_MIN, unit="min")).tz_convert(TZ).tz_localize(None)
    band = lambda hrs: d[(d.lead > (hrs - 1) * PER_H) & (d.lead <= hrs * PER_H)].set_index("h")  # noqa: E731
    fig, ax = plt.subplots(figsize=(10, 2.4))
    ax.fill_between(x, 0, band(1).y.reindex(h).fillna(0), step="post", color=GREY, lw=0,
                    label=f"{SLOT_NAME} had an alert")
    ax.plot(x, band(1).model.reindex(h), color=BLUE, lw=1.2, drawstyle="steps-post",
            label="forecast made up to 1 h before")
    ax.plot(x, band(6).model.reindex(h), color=ORANGE, lw=1.2, drawstyle="steps-post",
            label="made 5-6 h before")
    ax.xaxis.set_major_locator(mdates.DayLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %d"))
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.set(ylim=(0, 1), xlim=(x[0], x[-1]))
    ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(1, 1), fontsize=8)
    style(ax)
    return png(fig)


def heatmap(g, m, t0):
    labels = [f"{local(g, t0 + k):%H:%M}" for k in range(K)]
    fig, ax = plt.subplots(figsize=(10, 6.4))
    im = ax.imshow(m, cmap="Blues", vmin=0, vmax=1, aspect="auto")
    ax.set_yticks(range(len(g["regions"])), g["regions"], fontsize=8)
    ax.set_xticks(range(0, K, 2 * PER_H), labels[::2 * PER_H], fontsize=8)
    ax.set_xlabel(f"start of the {SLOT_NAME}, Kyiv time")
    fig.colorbar(im, ax=ax, shrink=0.6, label=f"chance of an alert during the {SLOT_NAME}")
    return png(fig)


def to_matrix(g, p, col):
    m = np.full((len(g["regions"]), K), np.nan)
    m[p.o.to_numpy(), p.lead.to_numpy() - 1] = p[col].to_numpy()
    return m


def fmt(df, **kw):
    return df.to_html(float_format=lambda x: f"{x:.3f}", border=0, classes="t", **kw)


def cards(g, alerts, test, test_start):
    a, b = g["start"] + test_start * SLOT, g["end"]
    out = []
    for name in SHOWN:
        o = g["regions"].index(name)
        p = test[test.o == o]
        q = p[p.lead == 1]
        share, med = time_under_alert(alerts, name, a, b)
        cols = {f"next {SLOT_NAME}, no alert running when forecast": right_wrong(q[q.now == 0]),
                "3 h ahead": right_wrong(ahead(p, 3)), "12 h ahead": right_wrong(ahead(p, 12))}
        out.append(f"""<h3>{name}</h3>
<p>{p[p.lead <= PER_H].y.mean():.0%} of {SLOT_NAME}s in the test had an alert, but the siren was on only {share:.0%} of the
time: alerts are short (median {med:.0f} min).</p>
{fmt(pd.DataFrame(cols))}
{week_chart(g, test, o)}""")
    return "\n".join(out)


CSS = """body{font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin:0 auto;padding:16px;
background:#fcfcfb;color:#0b0b0b}h1{font-size:26px}h2{margin-top:2em;border-bottom:1px solid #ddd}
.t{border-collapse:collapse;font-size:13px;margin:8px 0;display:block;overflow-x:auto}
.t td,.t th{padding:3px 8px;text-align:right;border-bottom:1px solid #eee}img{max-width:100%}
.note{color:#52514e}summary{cursor:pointer;color:#2a78d6}"""


def write_report(g, alerts, live, test, vb, chosen, rob, dropped, runtime):
    T = g["Y"].shape[1]
    test_start = T - TEST_WEEKS * WEEK
    lt = by_lead(test)
    every, quiet, running = (lt.loc[f"next {SLOT_NAME}, {w}", "Brier skill"]
                         for w in ("all", "no alert running", "alert already running"))
    later = lt.loc[["2 h ahead", "3 h ahead", "6 h ahead", "12 h ahead", "24 h ahead"], "Brier skill"]
    rs = rob["Brier skill"]
    inside = np.nanmedian([auc(d.y, d.model) for _, d in ahead(test, 3).groupby("o")])
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Air Alert Forecast</title>
<style>{CSS}</style></head><body>
<h1>Air raid alert forecast, Ukraine</h1>
<p>For each oblast and each {SLOT_NAME} of the next 24 hours: the chance that an alert sounds at some
point during that {SLOT_NAME}. Even a 5-minute alert counts, so this is not the share of time under alert.</p>
<p class="note">Data: volunteer (eTryvoga) alerts from the Vadimkin dataset, up to {local(g, T):%Y-%m-%d %H:%M}
Kyiv. Test: the last {TEST_WEEKS} weeks ({local(g, test_start):%Y-%m-%d} .. {local(g, T - 1):%Y-%m-%d}). A forecast is
made every hour for every oblast; the model is retrained every week on the 6 months before that week,
using only answers known by then. Generated by <code>python main.py</code> in {runtime / 60:.0f} min.</p>

<h2>How to read the scores</h2>
<ul>
<li><b>Right / wrong.</b> The model "says alert" when its chance is 50% or more. The report counts how
often that matched what happened, next to a lazy guess, the <b>{BASE}</b>: say "alert" if this
{SLOT_NAME} of the day had an alert on most of the last 28 days. Without the lazy guess a high score
misleads: in a quiet oblast, "never an alert" is right most of the time.</li>
<li><b>Alerts flagged</b>: of the {SLOT_NAME}s that had an alert, the share where the model said "alert".
<b>Flags that came true</b>: of the {SLOT_NAME}s where it said "alert", the share that had one.
"—" means it never said "alert".</li>
<li><b>"When it says 70%"</b>: of all the times the model gave about 70%, how often an alert came. A good
forecast gets close to 70%, like a weather forecast's chance of rain.</li>
<li><b>Brier skill</b>: how much smaller the model's error is than the {BASE}'s error, where the error is
(chance &minus; outcome)&sup2; and the outcome is 1 or 0. 0% means no better; negative means worse.</li>
<li><b>ROC-AUC</b>: take a random {SLOT_NAME} with an alert and a random one without. How often does the
model give the alert {SLOT_NAME} the higher chance? 0.5 is a coin flip, 1.0 is perfect.</li>
</ul>
<p><b>Why "no alert running" for the next {SLOT_NAME}:</b> the next {SLOT_NAME} starts at the moment the
forecast is made, so if an alert is already running it has an alert for certain. Counting those
forecasts flatters any model (skill {every:+.1%} with them, {quiet:+.1%} without). The cards below only use
the honest part.</p>

<h2>Four oblasts, from low to high alert pressure</h2>
<p class="note">Charts show the last test week. The blue line jumps up inside alerts because an alert
was already running when that forecast was made (the freebie above).</p>
{cards(g, alerts, test, test_start)}

<h2>All oblasts: how far ahead does it help?</h2>
<p>Next {SLOT_NAME}, no alert running: <b>{quiet:+.1%}</b> Brier skill. From 2 h ahead:
{later.min():+.1%} to {later.max():+.1%}, so beyond the next {SLOT_NAME} the model is about as good as
the oblast's 28-day rhythm. Alert history cannot see a specific attack coming hours ahead.
ROC-AUC over all oblasts ({lt.loc['3 h ahead', 'ROC-AUC model']:.2f} at 3 h) is high mostly because the model
tells busy oblasts from quiet ones; inside one oblast the ranking is weak (median
{inside:.2f} over the {len(g['regions'])} oblasts at 3 h).</p>
{skill_chart(test)}
<h3>"When it says 70%" (all oblasts, all leads)</h3>
{fmt(calibration(test))}
<details><summary>Full scores by lead (all 4 baselines)</summary>
<p class="note"><b>hour-of-week</b>: share of the last 4 weeks with an alert at the same weekday and
{SLOT_NAME}. <b>persistence</b>: 1 if an alert is running when the forecast is made.
<b>yesterday</b>: alert in the same {SLOT_NAME} 24 h earlier. Brier: lower is better.</p>
{fmt(lt)}</details>

<h2>Next 24 hours</h2>
<p>Forecast made at {local(g, T):%Y-%m-%d %H:%M} Kyiv (the end of the data) by a model trained on the
last 6 months. Inputs: {chosen}.</p>
{heatmap(g, live, T)}

<h2>Checks</h2>
<ul>
<li><b>Leakage test</b>: <code>python test_leakage.py</code> cuts the data at random moments, keeps only
what was known then, rebuilds every input and asserts that nothing changes.</li>
<li><b>No training on the test</b>: each weekly fold trains only on {SLOT_NAME}s whose answer was known
before the fold starts.</li>
<li><b>The next-{SLOT_NAME} freebie</b> is disclosed above: with an alert already running the skill is
{running:+.1%}, which is free by definition.</li>
<li><b>Robustness</b>: the same pipeline on 12 earlier 4-week blocks ({rob.index[0][:10]} ..
{rob.index[-1][-10:]}) gives {rs.min():+.1%} to {rs.max():+.1%} skill over all leads, positive in
{(rs > 0).sum()} of {len(rs)}.
<details><summary>table</summary>{fmt(rob[["alert share", "Brier model", f"Brier {BASE}", "Brier skill"]])}</details></li>
<li><b>Validation</b>: on the 2 weeks before the test, weather forecasts (cloud, wind, rain at the oblast
centre) are kept only if they lower the Brier score:
{", ".join(f"{n} {b:.4f}" for n, b in vb.items())}. Chosen: <b>{chosen}</b>.</li>
</ul>

<h2>Data cleaning and limitations</h2>
<ul><li>Volunteer data means "eTryvoga reported an alert", not the official siren system; the two disagree
in the west (Volyn, Lviv). Luhansk is dropped (3 alerts in 4 years); alerts under 1 minute are dropped.</li>
<li>Alerts longer than 12 h are missed all-clear messages: every one since Oct 2025 that could be checked
against the official data was an artifact. Their slots get no label and no forecast whose last 24 h touches
them. The rule uses hindsight but only removes rows.
<details><summary>All {len(dropped)} dropped alerts (UTC)</summary>{fmt(dropped)}</details></li>
<li>Inputs are alert history and the calendar. No real-time launch signals (bomber take-offs, drone
groups), so the model cannot see a specific attack coming.</li>
<li>The history inputs are built from the same "any alert in the {SLOT_NAME}" grid, so a 5-minute alert
enters them as a whole {SLOT_NAME}.</li></ul>
</body></html>"""
    REPORT.write_text(html, encoding="utf-8")


def main():
    t0 = time.time()
    print("Downloading volunteer alert data and archived weather forecasts ...")
    download(URL, DATA_FILE)
    alerts = load_alerts()
    g = build_grid(alerts)
    g["wx"] = load_weather(g)
    f = origin_features(g)
    T = g["Y"].shape[1]
    test_start = T - TEST_WEEKS * WEEK
    print(f"Data to {local(g, T):%Y-%m-%d %H:%M} Kyiv, {len(g['regions'])} oblasts, {SLOT_MIN}-min slots")

    print("Validation: which inputs to keep (2 weekly folds) ...")
    val = {n: walk_forward(g, f, test_start - VAL, 2, fs) for n, fs in FEATURE_SETS.items()}
    vb = {n: brier_score_loss(p.y, p.model) for n, p in val.items()}
    chosen = min(vb, key=vb.get)
    feats = FEATURE_SETS[chosen]
    print(f"  validation Brier {vb} -> {chosen}")
    print("Test (4 weekly folds) ...")
    test = walk_forward(g, f, test_start, TEST_WEEKS, feats)
    print(by_lead(test)[["alert share", "Brier model", f"Brier {BASE}", "Brier skill", "ROC-AUC model",
                         f"ROC-AUC {BASE}"]].to_string())
    print("Robustness (12 blocks) ...")
    rob = robustness(g, f, test_start, feats)
    print(rob[["Brier model", f"Brier {BASE}", "Brier skill"]].to_string())

    print("Forecast for the next 24 h ...")
    X, m = rows(g, f, [T], live=True, feats=feats)
    m["model"], m["lead"] = fit(g, f, T, feats).predict_proba(X)[:, 1], X.lead.astype(int).to_numpy()
    live = to_matrix(g, m, "model")

    lg = g["long"]
    dropped = lg.assign(start=lg.start.dt.strftime("%Y-%m-%d %H:%M"),
                        end=lg.end.dt.strftime("%Y-%m-%d %H:%M")).set_index("region")
    write_report(g, alerts, live, test, vb, chosen, rob, dropped, time.time() - t0)
    print(f"Wrote {REPORT.name} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
