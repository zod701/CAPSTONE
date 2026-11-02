"""도로망 택시 시간의 계수 맞추기 — 자동차 응답 표본(수요일 14:00 출발 지정)에 `roadnet.py` 의 BETA · C0 · TAU 를 맞추고, 거리 곡선과 5겹 교차검증으로 견준다.

표본 CSV 열: ox, oy, dx, dy, straight_m, dur_s (파생 수치만, 응답 원문을 두지 않는다). 경로가 배수에 따라 바뀌므로 '경로 찾기 → 맞추기' 를 되풀이한다.

    .\\.venv\\Scripts\\python.exe analysis\\roadnet_fit.py 표본1.csv 표본2.csv
"""
import csv
import sys
from pathlib import Path

import numpy as np
from scipy.optimize import nnls
from scipy.sparse.csgraph import dijkstra

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "web" / "backend", ROOT / "data" / "scripts"):
    sys.path.insert(0, str(p))

from algo import anchors  # noqa: E402
from roadnet import ACCESS_MPS, RoadNet  # noqa: E402

TAUS = (200.0, 300.0, 400.0, 500.0)
ROUNDS = 4
TOLLED = 5   # 유료 일반도로는 자유 속도 그대로 — 링크 25개라 맞추지 않는다(신월여의지하도로 응답 65 km/h · 원천 64 km/h)
DEST_SGG = {"CBD": "종로구", "GBD": "강남구", "YBD": "영등포구"}


def path_features(net, rows):
    """지금 배수의 최단 경로 → 표본마다 묶음별 자유 속도 시간 합 (n × 묶음). 경로가 없으면 nan."""
    edge = {(u, v): (t, g) for u, v, t, g in zip(net.fr.tolist(), net.to.tolist(), net.free_s.tolist(), net.grp.tolist())}
    X = np.zeros((len(rows), len(net.beta)))
    origins = {}
    for i, r in enumerate(rows):
        origins.setdefault(r["o"], []).append(i)
    for o, idx in origins.items():
        dist, pred = dijkstra(net.graph, indices=o, return_predecessors=True)
        for i in idx:
            v = rows[i]["d"]
            if not np.isfinite(dist[v]):
                X[i] = np.nan
                continue
            while v != o:
                u = int(pred[v])
                t, g = edge[(u, v)]
                X[i, g] += t
                v = u
    return X


def fit(X, W, y, tau):
    """고정한 경로에서 배수 · C0 — 음수 없는 최소제곱."""
    free = [g for g in range(X.shape[1]) if g != TOLLED]
    coef, _ = nnls(np.column_stack([X[:, free], 1 - np.exp(-W / tau)]), y - X[:, TOLLED])
    return np.insert(coef, TOLLED, 1.0)


def predict(coef, X, W, tau):
    return X @ coef[:-1] + coef[-1] * (1 - np.exp(-W / tau))


def report(name, pred, y):
    e = (pred - y) / 60
    print(f"  {name}: 절대 중앙 {np.median(np.abs(e)):.1f}분 · p90 {np.percentile(np.abs(e), 90):.1f}분 · "
          f"비율 {np.median(np.abs(pred / y - 1)):.1%} · 편향 {np.median(e):+.1f}분")


def fare_constants(net, rows, tau, c0):
    """경로 도로 km 로 산식 요금을 잰 뒤 응답과의 차이 → 저속 시간요금(원/초), 통행료 환산(원/분)."""
    groups = {}
    for r in rows:
        groups.setdefault(r["o"], []).append(r)
    out = []
    for o, group in groups.items():
        w, km, won = net.trace(o)
        for r in group:
            if not np.isfinite(w[r["d"]]) or not r["taxi"]:
                continue
            road_km = km[r["d"]] + r["acc"] * ACCESS_MPS / 1000
            to = DEST_SGG.get(r["kind"].split(":")[-1], r["sgg"])
            out.append((float(r["straight_m"]) / 1000, anchors.taxi_fare(road_km, r["sgg"], to, None), float(r["taxi"]),
                         float(net.eta(w[r["d"]], r["acc"])), won[r["d"]], float(r["toll"] or 0)))
    a = np.array(out)
    low = float(np.median((a[:, 2] - a[:, 1]) / a[:, 3]))
    both = (a[:, 4] > 0) & (a[:, 5] > 0)
    scale = float(np.median(a[both, 5] / a[both, 4]))
    print(f"LOW_SPEED_WON_PER_S = {low:.3f} · TOLL 배율(현재 환산 대비) = {scale:.3f}")
    fare = a[:, 1] + low * a[:, 3]
    print("거리 | n | 요금 예측 ÷ 응답 | 절대오차 중앙 | 통행료 있음 응답 · 모델")
    for lo, hi in ((0, 3), (3, 6), (6, 12), (12, 20), (20, 30), (30, 45), (45, 200)):
        m = (a[:, 0] >= lo) & (a[:, 0] < hi)
        print(f"  {lo}–{hi} km {m.sum()} {np.median(fare[m] / a[m, 2]):.2f} {np.median(np.abs(fare[m] - a[m, 2])):,.0f}원 {np.mean(a[m, 5] > 0):.0%} · {np.mean(a[m, 4] > 0):.0%}")
    print(f"요금 + 통행료 상대오차 중앙 {np.median(np.abs((fare + a[:, 4] * scale) / (a[:, 2] + a[:, 5]) - 1)):.1%}")


def main(paths):
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for path in paths:
        with open(path, encoding="utf-8", newline="") as f:
            rows += list(csv.DictReader(f))
    net = RoadNet()
    for r in rows:
        r["o"], ao = net.snap(float(r["ox"]), float(r["oy"]))
        r["d"], ad = net.snap(float(r["dx"]), float(r["dy"]))
        r["acc"] = ao + ad
    y = np.array([float(r["dur_s"]) for r in rows]) - np.array([r["acc"] for r in rows])
    full = np.array([float(r["dur_s"]) for r in rows])
    km = np.array([float(r["straight_m"]) / 1000 for r in rows])
    acc = full - y
    beta = np.array([1.9, 1.2, 1.8, 1.2, 1.3, 1.0])
    tau = 300.0
    for k in range(ROUNDS):
        net.set_beta(beta)
        X = path_features(net, rows)
        ok = np.isfinite(X).all(1)
        W = X @ beta
        tau = min(TAUS, key=lambda t: np.median(np.abs(predict(fit(X[ok], W[ok], y[ok], t), X[ok], W[ok], t) - y[ok])))
        coef = fit(X[ok], W[ok], y[ok], tau)
        used = X[ok].sum(0) > 0
        beta = np.where(used & (coef[:-1] > 0), coef[:-1], beta)
        print(f"회차 {k}: BETA {np.round(beta, 2).tolist()} · C0 {coef[-1]:.0f}초 · TAU {tau:g}초", flush=True)
    net.set_beta(beta)
    X = path_features(net, rows)
    ok = np.isfinite(X).all(1)
    W = X @ beta
    coef = fit(X[ok], W[ok], y[ok], tau)
    print(f"\nBETA = {tuple(round(float(b), 3) for b in beta)}\nC0 = {coef[-1]:.1f}\nTAU = {tau:g}")
    net.c0, net.tau = coef[-1], tau
    fare_constants(net, rows, tau, coef[-1])
    # 격자가 쓰는 꼴: 경로는 BETA 로 찾고 ETA = W + C0 × 포화 + 붙이는 시간
    fixed = W + coef[-1] * (1 - np.exp(-W / tau)) + acc
    fold = np.random.default_rng(0).integers(0, 5, len(rows))
    road, curve = np.full(len(rows), np.nan), np.full(len(rows), np.nan)
    for f in range(5):
        tr, te = ok & (fold != f), ok & (fold == f)
        c = fit(X[tr], W[tr], y[tr], tau)
        road[te] = predict(c, X[te], W[te], tau) + acc[te]
        p, b = np.polyfit(np.log(km[tr]), np.log(full[tr]), 1)
        curve[te] = np.exp(b) * km[te] ** p
    print(f"\n표본 {len(rows)} · 경로 없음 {int((~ok).sum())} — 5겹 표본 밖")
    report("곡선 295 × km^0.70", 295 * km[ok] ** 0.70, full[ok])
    report("곡선 다시 맞춤", curve[ok], full[ok])
    report("도로망", road[ok], full[ok])
    report("도로망 (격자가 쓰는 꼴, 표본 안)", fixed[ok], full[ok])
    kinds = np.array([r["kind"].split(":")[0] for r in rows])
    print("\n묶음별 예측 ÷ 응답 중앙 (곡선 295 / 곡선 재적합 / 도로망)")
    bands = [("도심행 " + f"{a}–{b} km", (kinds == "dest") & (km >= a) & (km < b)) for a, b in ((0, 10), (10, 20), (20, 40), (40, 200))]
    bands += [(f"출발 구간 {a}–{b} km", np.isin(kinds, ("leg", "short_first")) & (km >= a) & (km < b)) for a, b in ((0, 1), (1, 2), (2, 3), (3, 13))]
    bands += [("도착 구간 (도심 3 km 안)", kinds == "short_last")]
    for name, m in bands:
        m = m & ok
        print(f"  {name} n={m.sum()} {np.median(295 * km[m] ** 0.70 / full[m]):.2f} / {np.median(curve[m] / full[m]):.2f} / {np.median(road[m] / full[m]):.2f}")


if __name__ == "__main__":
    main(sys.argv[1:])
