"""도시철도 역 순서표(processed/subway_line_seq.csv)를 메모리에 올린다 — 노선군별 역 순서.

한 덩어리(chain) = 운행 노선 또는 지선의 역 순서(data/README.md). 순환선(2호선 · 6호선 응암순환)은 `loop=1` 로,
끝에서 처음으로 이어진다. 버스 노선과 달리 **방향이 없다** — 승차·하차 역이 어디냐로 정해진다.
운영이 갈려 두 덩어리로 적힌 노선(8호선 별내선 ↔ 서울 8호선, 4호선 진접선·안산선, 수인분당선 …)은 이어 붙인 덩어리도
같이 둔다 — 열차는 그대로 지나간다. 매칭이 구간의 역을 순서표에서 읽을 때 쓴다(resolver._line_window).
"""
import csv
from pathlib import Path

FILE = "subway_line_seq.csv"


def _join(a, b, phys):
    """두 덩어리를 **양쪽 다 끝인 역**에서 이어 붙인다 → 이은 순서, 못 이으면 None.

    지선은 분기역이 본선 한가운데라 붙지 않는다(5호선 마천·경춘선 광운대·2호선 성수). 끝의 역 id 가 달라도
    같은 실체(phys_id)면 잇고 한 번만 담는다 — 3호선 지축은 서울 3호선·일산선에 따로 적혀 있다.
    """
    for x in (a, a[::-1]):
        for y in (b, b[::-1]):
            if phys[x[-1]] == phys[y[0]] and not (set(x) & set(y)) - {x[-1], y[0]}:
                return x + y[1:]
    return None


class LinesDB:
    def __init__(self, rows):
        seq, loop, groups, phys = {}, {}, {}, {}
        self._stations = {}   # station_id → (이름, lat, lon)
        for r in rows:
            cid = r["chain_id"]
            if cid not in seq:
                seq[cid] = []
                loop[cid] = r["loop"].strip() == "1"
                groups.setdefault(r["line_group"], []).append(cid)
            seq[cid].append((int(r["seq"]), r["station_id"]))
            self._stations[r["station_id"]] = (r["name"], float(r["lat"]), float(r["lon"]))
            phys[r["station_id"]] = r["phys_id"]   # 같은 역의 노선별 행(환승)을 한 실체로 묶은 키
        self._seq = {cid: tuple(sid for _, sid in sorted(v)) for cid, v in seq.items()}
        self._loop = loop
        self._by_group = {g: list(v) for g, v in groups.items()}
        n_chains = len(self._seq)
        for group, cids in self._by_group.items():
            for i, joined in enumerate(self._join_group(cids, phys)):
                cid = f"{group}:이어붙임{i + 1}" if i else f"{group}:이어붙임"
                self._seq[cid] = joined
                self._loop[cid] = False
                cids.append(cid)
        self._by_group = {g: tuple(v) for g, v in self._by_group.items()}
        self.counts = {"chains": n_chains, "joined": len(self._seq) - n_chains,
                       "stations": len(self._stations)}

    def _join_group(self, chain_ids, phys):
        """노선군의 덩어리를 끝끼리 이어 붙인다 → 실제로 이어진 순서들 (원래 덩어리는 그대로 둔다)."""
        seqs = [self._seq[c] for c in chain_ids if not self._loop[c]]
        done = False
        while not done:
            done = True
            for i in range(len(seqs)):
                for j in range(i + 1, len(seqs)):
                    joined = _join(seqs[i], seqs[j], phys)
                    if joined:
                        seqs[i] = tuple(joined)
                        seqs.pop(j)
                        done = False
                        break
                if not done:
                    break
        originals = {self._seq[c] for c in chain_ids}
        return [s for s in seqs if s not in originals]

    @classmethod
    def load(cls, processed_dir):
        """processed/subway_line_seq.csv 를 읽는다. 없으면 FileNotFoundError."""
        with open(Path(processed_dir) / FILE, encoding="utf-8-sig", newline="") as f:
            return cls(csv.DictReader(f))

    def chains_for(self, line_group=None):
        """노선군의 덩어리 (지선을 따로 센다). line_group 이 없으면 전부 — 승·하차 역 id 로 어차피 좁혀진다."""
        if line_group is None:
            return tuple(self._seq)
        return self._by_group.get(line_group, ())

    def sequence(self, chain_id):
        """덩어리의 역 id 를 순서대로. 모르는 덩어리면 빈 튜플."""
        return self._seq.get(chain_id, ())

    def is_loop(self, chain_id):
        """순환선이면 True — 끝에서 처음으로 이어 붙여 구간을 뗄 수 있다."""
        return self._loop.get(chain_id, False)

    def station(self, station_id):
        """역의 (이름, lat, lon) — 순서표 값. 모르면 None."""
        return self._stations.get(station_id)
