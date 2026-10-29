"""grid_layer.py 의 GeoJSON → 지도 세 장(PNG). QGIS 파이썬 콘솔에서 실행한다 — 지금 프로젝트를 비우고 다시 그린다.

    exec(open(r"<저장소>\\analysis\\grid_maps_qgis.py", encoding="utf-8").read(), {"GRID": r"<…>.geojson"})

- best: 일반화 비용이 가장 작은 수단 · ratio: 대중교통 ÷ 택시 시간 · save: 택시를 섞어 아끼는 분
"""
import os

from qgis.core import (QgsCategorizedSymbolRenderer, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature,
                       QgsFillSymbol, QgsGeometry, QgsGraduatedSymbolRenderer, QgsLayerTree, QgsLayoutExporter,
                       QgsLayoutItemLabel, QgsLayoutItemLegend, QgsLayoutItemMap, QgsLayoutMeasurement, QgsLayoutPoint,
                       QgsLayoutSize, QgsLegendStyle, QgsMarkerSymbol, QgsPointXY, QgsPrintLayout, QgsProject,
                       QgsRectangle, QgsRendererCategory, QgsRendererRange, QgsSingleSymbolRenderer, QgsTextFormat,
                       QgsUnitTypes, QgsVectorLayer)
from qgis.PyQt.QtGui import QFont

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(GRID))))  # noqa: F821 — GRID(analysis/out/…)는 exec 로 넘긴다
OUT = os.path.join(os.path.dirname(GRID), "maps")  # noqa: F821
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
NOTE = "★ 도착지 · 1 km 격자, 서울·경기 (인천 제외)\n2026-10-29(목) 16:34 기준, 내부 추정\n일반화 비용 = 시간 + 요금 ÷ 456원/분"


def fill(color):
    return QgsFillSymbol.createSimple({"color": color, "outline_style": "no"})


def categorized(field):
    return QgsCategorizedSymbolRenderer(field, [QgsRendererCategory(v, fill(c), lab) for v, c, lab in BEST])


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
EXTENT = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, p).transformBoundingBox(
    QgsRectangle(126.36, 36.88, 127.88, 38.31))


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


def make(name, title, subtitle, renderer_for, show_base):
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
    lay.pageCollection().page(0).setPageSize(QgsLayoutSize(330, 128, QgsUnitTypes.LayoutMillimeters))
    text(lay, title, 8, 5, 310, 9, 15, True)
    text(lay, subtitle, 8, 13, 310, 6, 8.5)
    for i, ((k, n, *_), layer) in enumerate(zip(DESTS, lays)):
        m = QgsLayoutItemMap(lay)
        m.setCrs(crs)
        m.attemptMove(QgsLayoutPoint(8 + i * 80, 22))
        m.attemptResize(QgsLayoutSize(78, 92))
        m.setLayers([pts[k], sgg, layer] + ([base] if show_base else []))
        m.setKeepLayerSet(True)
        m.zoomToExtent(EXTENT)
        m.setFrameEnabled(True)
        m.setFrameStrokeWidth(QgsLayoutMeasurement(0.2))
        lay.addLayoutItem(m)
        text(lay, f"도착지: {n} ({k})", 8 + i * 80, 115, 78, 6, 9.5, True)
    leg = QgsLayoutItemLegend(lay)
    leg.setAutoUpdateModel(False)
    root = QgsLayerTree()
    root.addLayer(lays[0]).setName("")
    if show_base:
        root.addLayer(base)
    leg.model().setRootGroup(root)
    leg.setTitle("")
    for style in (QgsLegendStyle.SymbolLabel, QgsLegendStyle.Group, QgsLegendStyle.Subgroup):
        tf = QgsTextFormat()
        tf.setFont(QFont(FONT))
        tf.setSize(8.5)
        leg.rstyle(style).setTextFormat(tf)
    leg.attemptMove(QgsLayoutPoint(250, 24))
    lay.addLayoutItem(leg)
    text(lay, NOTE, 250, 95, 75, 20, 7.5)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = 200
    path = os.path.join(OUT, f"{name}.png")
    print(name, QgsLayoutExporter(lay).exportToImage(path, settings), path)


make("best", "도심까지 가장 유리한 수단", "일반화 비용이 가장 작은 수단 (중간 택시 제외)", categorized, False)
make("ratio", "대중교통 ÷ 택시 소요시간",
     "대중교통만 이용할 때 택시보다 몇 배 걸리는가 (회색: 1 km 안 정류장 없음 또는 4시간 안 도착 불가)", graduated(RATIO), True)
make("save", "택시를 섞으면 아끼는 시간",
     "대중교통만 − 택시가 낀 수단 중 가장 짧은 시간 (회색: 대중교통만으로는 경로 없음)", graduated(SAVE), True)


# --- GeoPackage (같은 이름 .gpkg): grid = 칸 지표 · ratio · save = 그 지표만 담은 칸 · od = 원자료 CSV(도형 없음,
# cell 로 잇는다) · sgg · dests. grid 에는 지도 9개(지표 × 도착지) 스타일을 함께 저장한다 — 기본은 best_CBD, 빈값은 그리지 않는다.
# ratio · save 에는 도착지 3개 스타일을 저장하고(기본 CBD), 빈값(대중교통 경로 없음)은 PNG 처럼 회색으로 그린다.
from qgis.core import QgsRuleBasedRenderer, QgsVectorFileWriter  # noqa: E402

GPKG = os.path.splitext(GRID)[0] + ".gpkg"  # noqa: F821


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
od = QgsVectorLayer("file:///" + (os.path.splitext(GRID)[0] + ".csv").replace("\\", "/")  # noqa: F821
                    + "?delimiter=,&geomType=none&encoding=UTF-8", "od", "delimitedtext")
write(base, "grid", first=True)
for name in ("ratio", "save"):
    write(base, name, fields=["cell", "sido", "sgg"] + [f"{name}_{k}" for k, *_ in DESTS])
write(od, "od")
write(sgg, "sgg")
write(dests, "dests")

for table, layer in (("sgg", sgg), ("dests", pts["CBD"])):
    saved = QgsVectorLayer(f"{GPKG}|layername={table}", table, "ogr")
    saved.setRenderer(layer.renderer().clone())
    saved.saveStyleToDatabase(table, "", True, "")
grid = QgsVectorLayer(f"{GPKG}|layername=grid", "grid", "ogr")
styles = [(f"{name}_{k}", rend) for name, rend in (("best", categorized), ("ratio", graduated(RATIO)), ("save", graduated(SAVE)))
          for k, *_ in DESTS]
for i, (name, rend) in enumerate(styles):
    grid.setRenderer(rend(name))
    grid.saveStyleToDatabase(name, "", i == 0, "")
for name, bins in (("ratio", RATIO), ("save", SAVE)):
    layer = QgsVectorLayer(f"{GPKG}|layername={name}", name, "ogr")
    for i, (k, *_) in enumerate(DESTS):
        rend = QgsRuleBasedRenderer.convertFromRenderer(graduated(bins)(f"{name}_{k}"))
        other = QgsRuleBasedRenderer.Rule(fill("#d9d9d9"), label="대중교통 경로 없음", elseRule=True)
        rend.rootRule().appendChild(other)
        layer.setRenderer(rend)
        layer.saveStyleToDatabase(f"{name}_{k}", "", i == 0, "")
print("gpkg", GPKG, [(n, QgsVectorLayer(f"{GPKG}|layername={n}", n, "ogr").featureCount())
                     for n in ("grid", "ratio", "save", "od", "sgg", "dests")])
