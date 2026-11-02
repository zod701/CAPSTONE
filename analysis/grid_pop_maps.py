"""격자별 거주 인구 지도(PNG) — 수도권 · 서울. `processed/pop_grid.csv` 를 grid_layer.py 의 GeoJSON 칸에 붙여 그린다.
QGIS 화면 밖의 별도 프로세스로 돌린다(grid_maps_run.py 와 같다).

    & "C:\\Program Files\\QGIS 3.44.8\\bin\\python-qgis-ltr.bat" analysis\\grid_pop_maps.py analysis\\out\\grid_cbd_평시_500m_k4_capr0.3.geojson

→ analysis/out/pop_<크기>[_서울].geojson · analysis/out/maps/pop_<크기>[_서울]/pop.png
"""
import csv
import json
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsFillSymbol,  # noqa: E402
                       QgsGeometry, QgsGraduatedSymbolRenderer, QgsLayoutExporter, QgsLayoutItemLabel, QgsLayoutItemLegend,
                       QgsLayoutItemMap, QgsLayoutMeasurement, QgsLayoutPoint, QgsLayoutSize, QgsLegendStyle, QgsMarkerSymbol,
                       QgsPointXY, QgsPrintLayout, QgsProject, QgsRectangle, QgsRendererRange, QgsSingleSymbolRenderer,
                       QgsTextFormat, QgsUnitTypes, QgsVectorLayer)
from qgis.PyQt.QtGui import QFont  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = "Malgun Gothic"
DESTS = [("광화문", 126.9765, 37.5709), ("강남", 127.0276, 37.4979), ("여의도", 126.9243, 37.5216)]
BINS = [(1, 99, "#ffffcc", "100명 미만"), (100, 499, "#ffeda0", "100–500명"), (500, 999, "#fed976", "500–1,000명"),
        (1000, 2499, "#feb24c", "1,000–2,500명"), (2500, 4999, "#fd8d3c", "2,500–5,000명"),
        (5000, 9999, "#e31a1c", "5,000–10,000명"), (10000, 999999, "#800026", "10,000명 이상")]


def fill(color):
    return QgsFillSymbol.createSimple({"color": color, "outline_style": "no"})


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


def write_cells(features, pop, path):
    """칸 모양은 그대로, 속성은 칸 번호 · 시도 · 시군구 · 인구만 남긴다."""
    feats = [{"type": "Feature", "geometry": f["geometry"],
              "properties": {k: f["properties"][k] for k in ("cell", "sido", "sgg")} | {"pop": pop.get(f["properties"]["cell"], 0)}}
             for f in features]
    with open(path, "w", encoding="utf-8") as out:
        json.dump({"type": "FeatureCollection", "features": feats}, out, ensure_ascii=False)
    return sum(f["properties"]["pop"] for f in feats)


def render(path, png, total, size_m, box=None):
    p = QgsProject.instance()
    p.clear()
    crs = QgsCoordinateReferenceSystem("EPSG:5179")
    p.setCrs(crs)
    sgg = QgsVectorLayer(os.path.join(ROOT, "data", "processed", "admin_sgg_web.geojson"), "시군구", "ogr")
    sgg.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": "0,0,0,0", "outline_color": "90,90,90", "outline_width": "0.12"})))
    base = QgsVectorLayer(path, "거주 인구 0", "ogr")
    base.setRenderer(QgsSingleSymbolRenderer(fill("#e6e6e6")))
    layer = QgsVectorLayer(path, "인구", "ogr")
    layer.setRenderer(QgsGraduatedSymbolRenderer("pop", [QgsRendererRange(lo, hi, fill(c), lab) for lo, hi, c, lab in BINS]))
    pts = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string", "도착지", "memory")
    for n, x, y in DESTS:
        f = QgsFeature(pts.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        f["name"] = n
        pts.dataProvider().addFeature(f)
    pts.setRenderer(QgsSingleSymbolRenderer(QgsMarkerSymbol.createSimple(
        {"name": "star", "color": "black", "outline_color": "white", "size": "3.6"})))
    for lyr in (sgg, base, layer, pts):
        p.addMapLayer(lyr, False)
    extent = base.extent() if box is None else base.extent().intersect(box)
    extent = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), crs, p).transformBoundingBox(extent.buffered(0.02))
    lay = QgsPrintLayout(p)
    lay.initializeDefaults()
    p.layoutManager().addLayout(lay)
    lay.pageCollection().page(0).setPageSize(QgsLayoutSize(190, 130, QgsUnitTypes.LayoutMillimeters))
    head = text(lay, "거주 인구", 8, 5, 170, 9, 15, True)
    head.adjustSizeToText()
    text(lay, f"2024년 · {size_m} m 격자 · 합 {total:,}명", 8 + head.sizeWithUnits().width() + 4, 7, 130, 7, 10)
    m = QgsLayoutItemMap(lay)
    m.setCrs(crs)
    m.attemptMove(QgsLayoutPoint(8, 16))
    m.attemptResize(QgsLayoutSize(118, 108))
    m.setLayers([pts, sgg, layer, base])
    m.setKeepLayerSet(True)
    m.zoomToExtent(extent)
    m.setFrameEnabled(True)
    m.setFrameStrokeWidth(QgsLayoutMeasurement(0.2))
    lay.addLayoutItem(m)
    leg = QgsLayoutItemLegend(lay)
    leg.setAutoUpdateModel(False)
    root = leg.model().rootGroup()   # 범례가 가진 트리를 비워 쓴다 — grid_maps_qgis.py 와 같은 이유
    root.clear()
    root.addLayer(layer).setName("")
    root.addLayer(base).setName("거주 인구 0")
    root.addLayer(pts).setName("도착지 (광화문 · 강남 · 여의도)")
    leg.setTitle("")
    for style in (QgsLegendStyle.SymbolLabel, QgsLegendStyle.Group, QgsLegendStyle.Subgroup):
        tf = QgsTextFormat()
        tf.setFont(QFont(FONT))
        tf.setSize(8.5)
        leg.rstyle(style).setTextFormat(tf)
    leg.attemptMove(QgsLayoutPoint(129, 18))
    lay.addLayoutItem(leg)
    settings = QgsLayoutExporter.ImageExportSettings()
    settings.dpi = 200
    settings.generateWorldFile = False
    os.makedirs(os.path.dirname(png), exist_ok=True)
    print(QgsLayoutExporter(lay).exportToImage(png, settings), png, f"{total:,}명")


def main(grid):
    with open(os.path.join(ROOT, "data", "processed", "pop_grid.csv"), encoding="utf-8", newline="") as f:
        pop = {r["cell"]: int(r["pop"]) for r in csv.DictReader(f)}
    with open(grid, encoding="utf-8") as f:
        features = json.load(f)["features"]
    size_m = int(features[0]["properties"]["cell"].split("m_")[0])
    out = os.path.dirname(os.path.abspath(grid))
    for tag, cells, box in (("", features, QgsRectangle(126.10, 36.88, 127.88, 38.31)),
                            ("_서울", [c for c in features if c["properties"]["sido"] == "서울특별시"], None)):
        name = f"pop_{size_m}m{tag}"
        path = os.path.join(out, f"{name}.geojson")
        total = write_cells(cells, pop, path)
        render(path, os.path.join(out, "maps", name, "pop.png"), total, size_m, box)


app = QgsApplication([], True)
app.initQgis()
status = 0
try:
    main(sys.argv[1])
except Exception:
    traceback.print_exc()
    status = 1
QgsProject.instance().clear()
sys.stdout.flush()
sys.stderr.flush()
os._exit(status)   # grid_maps_run.py 와 같다 — exitQgis() 가 접근 위반으로 죽는다
