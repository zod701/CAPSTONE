"""grid_layer.py 의 GeoJSON → 지도 네 장(PNG, maps/<GeoJSON 이름>/) + 같은 이름 .gpkg. QGIS 파이썬 콘솔에서 실행한다 —
지금 프로젝트를 비우고 다시 그린다. 고른 하이퍼파라미터(탑승 횟수 · 택시비 상한)는 GeoJSON 의 params 에서 읽는다.

    exec(open(r"<저장소>\\analysis\\grid_maps_qgis.py", encoding="utf-8").read(), {"GRID": r"<…>.geojson"})

- best: 상한 안에서 가장 빠른 수단 · ratio: 대중교통 ÷ 택시 시간 · save: 택시를 섞어 아끼는 분 · won: 아낀 1분당 추가 비용
"""
import json
import os

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsFillSymbol, QgsGeometry, QgsGraduatedSymbolRenderer, QgsLayoutExporter,
                       QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutMeasurement, QgsLayoutPoint,
                       QgsLayoutSize, QgsLegendStyle, QgsMarkerSymbol, QgsPointXY, QgsPrintLayout, QgsProject,
                       QgsRectangle, QgsRendererCategory, QgsRendererRange, QgsSingleSymbolRenderer, QgsTextFormat,
                       QgsUnitTypes, QgsVectorLayer)
from qgis.PyQt.QtGui import QFont

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(GRID))))  # noqa: F821 — GRID(analysis/out/…)는 exec 로 넘긴다
OUT = os.path.join(os.path.dirname(GRID), "maps", os.path.splitext(os.path.basename(GRID))[0])  # noqa: F821
PARAMS = json.load(open(GRID, encoding="utf-8"))["params"]  # noqa: F821
FONT = "Malgun Gothic"
DESTS = [("CBD", "광화문", 126.9765, 37.5709), ("GBD", "강남", 127.0276, 37.4979), ("YBD", "여의도", 126.9243, 37.5216)]
BEST = [("transit", "#2c6fbb", "대중교통만"), ("first", "#3a9d5d", "출발 택시 + 대중교통"),
        ("both", "#8e5cc4", "택시 + 대중교통 + 택시"), ("last", "#e08a2c", "대중교통 + 도착 택시"), ("taxi", "#d64545", "택시만")]
RATIO = [(0, 1.0, "#1a9850", "1.0 미만 (대중교통이 더 빠름)"), (1.0, 1.25, "#91cf60", "1.0–1.25"),
         (1.25, 1.5, "#d9ef8b", "1.25–1.5"), (1.5, 1.75, "#fee08b", "1.5–1.75"), (1.75, 2.0, "#fc8d59", "1.75–2.0"),
         (2.0, 2.5, "#e34a33", "2.0–2.5"), (2.5, 99, "#99000d", "2.5 이상")]
SAVE = [(-999, 0, "#bdd7e7", "0분 미만 (혼합이 더 느림)"), (0, 5, "#ffffcc", "0–5분"), (5, 10, "#c7e9b4", "5–10분"),
        (10, 20, "#7fcdbb", "10–20분"), (20, 30, "#41b6c4", "20–30분"), (30, 45, "#1d91c0", "30–45분"),
        (45, 999, "#0c2c84", "45분 이상")]
WON = [(0, 100, "#3f007d", "100원 미만"), (100, 200, "#6a51a3", "100–200원"), (200, 300, "#9e9ac8", "200–300원"),
       (300, 456, "#cbc9e2", "300–456원 (업무통행 VOT 아래)"), (456, 700, "#fdd0a2", "456–700원"), (700, 1000, "#fd8d3c", "700–1,000원"),
       (1000, 99999, "#d94801", "1,000원 이상")]
CLOSE = [(-1.5, -0.5, "#8e5cc4", "하이브리드로만 갈 수 있음"), (-0.5, 0, "#bdd7e7", "0% 미만 (혼합이 더 느림)"),   # −1 = 하이브리드로만
         (0, 0.25, "#d73027", "0–25%"), (0.25, 0.5, "#fc8d59", "25–50%"), (0.5, 0.75, "#d9ef8b", "50–75%"),
         (0.75, 1.0, "#91cf60", "75–100%"), (1.0, 99, "#1a9850", "100% 이상 (택시보다 빠름)")]
TYPES = [("good", "#2c6fbb", "대중교통 양호 (시간비 1.5 미만)"), ("firstlast", "#3a9d5d", "첫·끝 구간 문제 (회수율 50% 이상)"),
         ("trunk", "#d64545", "간선 부족 (회수율 50% 미만)"), ("hybrid_only", "#8e5cc4", "하이브리드로만 갈 수 있음")]
CONDITION = f"탑승 ≤ {PARAMS['max_rides']}번 · 택시비 합 ≤ {PARAMS['cap']:,.0f}원"   # 제목 오른쪽에 붙인다


def fill(color):
    return QgsFillSymbol.createSimple({"color": color, "outline_style": "no"})


def categorized(cats):
    return lambda field: QgsCategorizedSymbolRenderer(field, [QgsRendererCategory(v, fill(c), lab) for v, c, lab in cats])


def graduated(bins):
    return lambda field: QgsGraduatedSymbolRenderer(field, [QgsRendererRange(lo, hi, fill(c), lab) for lo, hi, c, lab in bins])


p = QgsProject.instance()
p.clear()
crs = QgsCoordinateReferenceSystem("EPSG:5179")
p.setCrs(crs)
os.makedirs(OUT, exist_ok=True)

sgg = QgsVectorLayer(os.path.join(ROOT, "data", "processed", "admin_sgg_web.geojson"), "시군구", "ogr")
sgg.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
    {"color": "0,0,0,0", "outline_color": "90,90,90", "outline_width": "0.12"})))
base = QgsVectorLayer(GRID, "대중교통 경로 없음", "ogr")  # noqa: F821
base.setRenderer(QgsSingleSymbolRenderer(fill("#d9d9d9")))
pts = {}   # 패널마다 그 도착지만 찍는다
for k, n, x, y in DESTS:
    layer = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", f"도착지_{k}", "memory")
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    f["name"] = n
    layer.dataProvider().addFeature(f)
    layer.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
        {"name": "star", "color": "black", "outline_color": "white", "size": "3.6"})))
    pts[k] = layer
for layer in (sgg, base, *pts.values()):
    p.addMapLayer(layer, False)
# 격자 범위 — 옹진 섬 지역(서쪽 끝)까지 넣으면 지도가 작아져 수도권 상자와 겹치는 부분만 쓴다
EXTENT = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, p).transformBoundingBox(
    base.extent().intersect(QgsRectangle(126.10, 36.88, 127.88, 38.31)).buffered(0.02))


def text(lay, s, x, y, w, h, size, bold=False):
    it = QgsLayoutItemLabel(lay)
    it.setText(s)
    fmt, font = QgsTextFormat(), QFont(FONT)
    font.setBold(bold)
    fmt.setFont(font)
    fmt.setSize(size)
    it.setTextFormat(fmt)
    it.attemptMove(QgsLayoutPoint(x, y))
    it.attemptResize(QgsLayoutSize(w, h))
    lay.addLayoutItem(it)
    return it


GRAY = {"won": "대중교통 경로 없음 · 1분 넘게 못 아낌", "close": "대중교통 · 하이브리드 경로 없음 · 메울 격차 없음",
        "type": "대중교통 · 하이브리드 경로 없음"}   # 회색 칸이 뜻하는 것 — 지도마다 다르다(그 밖은 '대중교통 경로 없음')


def make(name, title, renderer_for, show_base, formula=None):
    lays = []
    for k, *_ in DESTS:
        layer = QgsVectorLayer(GRID, f"{name}_{k}", "ogr")  # noqa: F821
        layer.setRenderer(renderer_for(f"{name}_{k}"))
        p.addMapLayer(layer, False)
        lays.append(layer)
    lay = QgsPrintLayout(p)
    lay.initializeDefaults()
    lay.setName(name)
    p.layoutManager().addLayout(lay)
    lay.pageCollection().page(0).setPageSize(QgsLayoutSize(330, 120, QgsUnitTypes.LayoutMillimeters))
    head = text(lay, title, 8, 5, 310, 9, 15, True)
    head.adjustSizeToText()
    # 제목 오른쪽: (식) · 조건
    text(lay, f"{formula}   ·   {CONDITION}" if formula else CONDITION, 8 + head.sizeWithUnits().width() + 4, 7, 230, 7, 10)
    for i, ((k, n, *_), layer) in enumerate(zip(DESTS, lays)):
        m = QgsLayoutItemMap(lay)
        m.setCrs(crs)
        m.attemptMove(QgsLayoutPoint(8 + i * 80, 16))
        m.attemptResize(QgsLayoutSize(78, 92))
        m.setLayers([pts[k], sgg, layer] + ([base] if show_base else []))
        m.setKeepLayerSet(True)
        m.zoomToExtent(EXTENT)
        m.setFrameEnabled(True)
        m.setFrameStrokeWidth(QgsLayoutMeasurement(0.2))
        lay.addLayoutItem(m)
        text(lay, f"도착지: {n} ({k})", 8 + i * 80, 109, 78, 6, 9.5, True)
    leg = QgsLayoutItemLegend(lay)
    leg.setAutoUpdateModel(False)
    # 범례가 가진 트리를 비워 쓴다 — 새 QgsLayerTree 를 넘기면 함수가 끝날 때 파이썬이 지워 범례가 빈 메모리를 가리키고
    # 몇 장 뒤에 QGIS 가 통째로 죽었다(접근 위반)
    root = leg.model().rootGroup()
    root.clear()
    root.addLayer(lays[0]).setName("")
    if show_base:
        root.addLayer(base).setName(GRAY.get(name, "대중교통 경로 없음"))
    leg.setTitle("")
    for style in (QgsLegendStyle.SymbolLabel, QgsLegendStyle.Group, QgsLegendStyle.Subgroup):
        tf = QgsTextFormat()
        tf.setFont(QFont(FONT))
        tf.setSize(8.5)
        leg.rstyle(style).setTextFormat(tf)
    leg.attemptMove(QgsLayoutPoint(250, 18))
    lay.addLayoutItem(leg)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = 200
    settings.generateWorldFile = False   # 좌표 파일(.pgw)은 쓰지 않는다
    path = os.path.join(OUT, f"{name}.png")
    print(name, QgsLayoutExporter(lay).exportToImage(path, settings), path)


make("best", "도심까지 가장 빠른 수단", categorized(BEST), False)
make("ratio", "대중교통 ÷ 택시 소요시간", graduated(RATIO), True)
make("save", "하이브리드 경로로 아끼는 시간", graduated(SAVE), True)
make("won", "택시를 섞을 때 아낀 1분당 추가 비용", graduated(WON), True)
make("close", "하이브리드 격차 회수율", graduated(CLOSE), True,
     "= (대중교통만 − 하이브리드) ÷ (대중교통만 − 택시만) 소요시간")
make("type", "대중교통 여건 유형", categorized(TYPES), True)


# --- GeoPackage (같은 이름 .gpkg): grid = 칸 지표 · ratio · save · won · close · type = 그 지표만 담은 칸 · sgg · dests (원자료는 grid_cbd 의
# .csv.gz — 하이퍼파라미터 조합마다 행이 있어 크다). grid 에는 지도 18개(지표 × 도착지) 스타일을 함께 저장한다 — 기본은 best_CBD, 빈값은 그리지 않는다.
# ratio · save · won · close · type 에는 도착지 3개 스타일을 저장하고(기본 CBD), 빈값(대중교통 경로 없음)은 PNG 처럼 회색으로 그린다.
from qgis.core import QgsRuleBasedRenderer, QgsVectorFileWriter  # noqa: E402

GPKG = os.path.splitext(GRID)[0] + ".gpkg"  # noqa: F821
if os.path.exists(GPKG):   # 같은 이름으로 다시 그리면 지난 실행의 레이어가 파일을 잡고 있다 — 놓게 한 뒤 지운다
    import gc
    gc.collect()
    os.remove(GPKG)


def write(layer, name, first=False, fields=None):
    o = QgsVectorFileWriter.SaveVectorOptions()
    o.driverName, o.layerName, o.fileEncoding = "GPKG", name, "UTF-8"
    if fields:
        o.attributes = [layer.fields().indexOf(f) for f in fields]
    o.actionOnExistingFile = (QgsVectorFileWriter.CreateOrOverwriteFile if first
                              else QgsVectorFileWriter.CreateOrOverwriteLayer)
    err = QgsVectorFileWriter.writeAsVectorFormatV3(layer, GPKG, p.transformContext(), o)
    if err[0] != QgsVectorFileWriter.NoError:
        raise RuntimeError(f"{name}: {err[1]}")


dests = QgsVectorLayer("Point?crs=EPSG:4326&field=dest:string&field=name:string", "dests", "memory")
for k, n, x, y in DESTS:
    f = QgsFeature(dests.fields())
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
    f["dest"], f["name"] = k, n
    dests.dataProvider().addFeature(f)
write(base, "grid", first=True)
for name in ("ratio", "save", "won", "close", "type"):
    write(base, name, fields=["cell", "sido", "sgg"] + [f"{name}_{k}" for k, *_ in DESTS])
write(sgg, "sgg")
write(dests, "dests")

for table, layer in (("sgg", sgg), ("dests", pts["CBD"])):
    saved = QgsVectorLayer(f"{GPKG}|layername={table}", table, "ogr")
    saved.setRenderer(layer.renderer().clone())
    saved.saveStyleToDatabase(table, "", True, "")
grid = QgsVectorLayer(f"{GPKG}|layername=grid", "grid", "ogr")
RENDERERS = {"best": categorized(BEST), "ratio": graduated(RATIO), "save": graduated(SAVE), "won": graduated(WON),
             "close": graduated(CLOSE), "type": categorized(TYPES)}
styles = [(f"{name}_{k}", rend) for name, rend in RENDERERS.items() for k, *_ in DESTS]
for i, (name, rend) in enumerate(styles):
    grid.setRenderer(rend(name))
    grid.saveStyleToDatabase(name, "", i == 0, "")
for name in ("ratio", "save", "won", "close", "type"):
    layer = QgsVectorLayer(f"{GPKG}|layername={name}", name, "ogr")
    for i, (k, *_) in enumerate(DESTS):
        rend = QgsRuleBasedRenderer.convertFromRenderer(RENDERERS[name](f"{name}_{k}"))
        other = QgsRuleBasedRenderer.Rule(fill("#d9d9d9"), label=GRAY.get(name, "대중교통 경로 없음"), elseRule=True)
        rend.rootRule().appendChild(other)
        layer.setRenderer(rend)
        layer.saveStyleToDatabase(f"{name}_{k}", "", i == 0, "")
print("gpkg", GPKG, [(n, QgsVectorLayer(f"{GPKG}|layername={n}", n, "ogr").featureCount())
                     for n in ("grid", "ratio", "save", "won", "close", "type", "sgg", "dests")])
