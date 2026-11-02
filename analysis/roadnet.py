"""택시 시간 = 도로망 최단 경로 (`processed/road_net.npz`, data/scripts/build_road_net.py).

링크 시간 = 자유 속도 시간 × 유형 묶음 배수(`BETA`). 경로 시간 W 에 출발 · 도착 지체를 더한다:

    ETA = W + C0 × (1 − exp(−W / TAU)) + 양 끝을 도로에 붙이는 시간

지체는 짧은 통행일수록 덜 쌓인다 — 고정값으로 두면 1 km 안쪽을 1.6배 길게 봤다. 계수는 평일 14:00 기준(analysis/roadnet_fit.py, method.md E49).
요금은 같은 경로의 도로 km 로 잰 산식 + 저속 시간요금 + 통행료다.
"""
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

import kgrid

ROOT = Path(__file__).resolve().parents[1]
BETA = (2.051, 1.254, 1.924, 1.218, 1.327, 1.0)   # 고속화 서울 · 고속화 밖 · 일반 서울 · 간선 밖 · 이면 밖 · 유료 일반도로
C0 = 199.8
TAU = 200.0
TOLL_WON_PER_MIN = 428.0   # 원천 통행료(분/대) → 원 — 카카오 통행료 ÷ 같은 경로의 원천 값 중앙
LOW_SPEED_WON_PER_S = 0.836   # 저속 시간요금 — (카카오 택시요금 − 경로 거리 산식 요금) ÷ ETA 중앙
ACCESS_DETOUR = 1.3
ACCESS_MPS = 20 / 3.6   # 칸 · 정류장 → 가장 가까운 일반도로 노드


class RoadNet:
    def __init__(self, beta=BETA, c0=C0, tau=TAU):
        z = np.load(ROOT / "data" / "processed" / "road_net.npz")
        self.fr, self.to, self.free_s, self.grp, self.km, self.toll_min = z["fr"], z["to"], z["free_s"], z["grp"], z["km"], z["toll_min"]
        self.n = len(z["lon"])
        self.c0, self.tau = c0, tau
        self.local = np.where(z["local"])[0]
        xy = [kgrid.to_xy(float(a), float(b)) for a, b in zip(z["lon"][self.local], z["lat"][self.local])]
        self.tree = cKDTree(np.array(xy))
        self.set_beta(beta)

    def set_beta(self, beta):
        self.beta = np.asarray(beta, float)
        self.graph = csr_matrix((self.free_s * self.beta[self.grp], (self.fr, self.to)), shape=(self.n, self.n))
        self.rev = self.graph.T.tocsr()
        size = (self.n, self.n)
        self.km_m = csr_matrix((self.km, (self.fr, self.to)), shape=size)
        self.toll_m = csr_matrix((self.toll_min, (self.fr, self.to)), shape=size)

    def snap(self, lon, lat):
        """점 → (가장 가까운 일반도로 노드, 거기까지 시간 초)."""
        d, k = self.tree.query(kgrid.to_xy(lon, lat))
        return int(self.local[k]), d * ACCESS_DETOUR / ACCESS_MPS

    def times_from(self, node, limit=np.inf, reverse=False):
        """node 에서 (reverse 면 node 까지) 모든 노드의 경로 시간 W 초 — 닿지 못하면 inf."""
        return dijkstra(self.rev if reverse else self.graph, indices=node, limit=limit)

    def trace(self, node, limit=np.inf, reverse=False):
        """times_from 에 경로의 도로 km · 통행료(원)를 더해 → (W, km, 원). 앞선 노드를 두 배씩 건너뛰며 더한다."""
        w, pred = dijkstra(self.rev if reverse else self.graph, indices=node, limit=limit, return_predecessors=True)
        hit = np.where(pred >= 0)[0]
        p = np.arange(self.n)
        p[hit] = pred[hit]
        km, won = np.zeros(self.n), np.zeros(self.n)
        a, b = (hit, pred[hit]) if reverse else (pred[hit], hit)
        km[hit] = np.asarray(self.km_m[a, b]).ravel()
        won[hit] = np.asarray(self.toll_m[a, b]).ravel() * TOLL_WON_PER_MIN
        while True:
            q = p[p]
            km, won = km + km[p], won + won[p]
            if (q == p).all():
                return w, km, won
            p = q

    def eta(self, w, access_s):
        return w + self.c0 * (1 - np.exp(-np.asarray(w) / self.tau)) + access_s
