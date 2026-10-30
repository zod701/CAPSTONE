"""grid_maps_qgis.py 를 QGIS 화면 밖의 별도 프로세스에서 돌린다 — 열려 있는 QGIS 를 건드리지 않는다.

화면 QGIS 안에서 여러 번 이어 돌리면 지난 실행의 레이어가 쥔 GeoPackage 를 지우다 QGIS 가 통째로 죽었다.
GeoJSON 하나마다 이 스크립트를 새로 띄운다.

    & "C:\\Program Files\\QGIS 3.44.8\\bin\\python-qgis-ltr.bat" analysis\\grid_maps_run.py <GeoJSON>
"""
import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 창 없이 레이아웃을 그린다
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")   # offscreen 은 시스템 글꼴을 모른다 — 없으면 한글이 네모로 나온다

from qgis.core import QgsApplication, QgsProject  # noqa: E402

app = QgsApplication([], True)
app.initQgis()
status = 0
try:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grid_maps_qgis.py")
    exec(open(script, encoding="utf-8").read(), {"GRID": os.path.abspath(sys.argv[1])})
except Exception:
    traceback.print_exc()
    status = 1
QgsProject.instance().clear()
sys.stdout.flush()
sys.stderr.flush()
# exitQgis() 가 레이아웃 · 레이어를 치우다 접근 위반으로 죽는다 — 결과는 이미 다 썼으니 정리 없이 끝낸다
os._exit(status)
