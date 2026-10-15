# PyInstaller 설정 (Windows): .\build_win.ps1 로 빌드 → dist\LocalTag\LocalTag.exe
# 버전 번호는 macOS spec(LocalTag.spec)의 CFBundleShortVersionString 한 곳에서만 올린다.
import os
import re

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
with open(os.path.join(SPECPATH, "LocalTag.spec"), encoding="utf-8") as f:
    VERSION = re.search(r'"CFBundleShortVersionString":\s*"([^"]+)"', f.read()).group(1)
# 앱이 '정보' 창에 보여 줄 버전 (빌드 때만 만드는 파일, 저장소에는 넣지 않는다)
with open(os.path.join(ROOT, "localtag", "_version.py"), "w", encoding="utf-8") as f:
    f.write(f'VERSION = "{VERSION}"\n')

# 탐색기 '속성 → 자세히' 에 보이는 버전 정보
nums = (list(map(int, re.findall(r"\d+", VERSION))) + [0, 0, 0, 0])[:4]
VERSION_FILE = os.path.join(workpath, "version_info.txt")
os.makedirs(workpath, exist_ok=True)
with open(VERSION_FILE, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(nums)}, prodvers={tuple(nums)}),
  kids=[StringFileInfo([StringTable('041204B0', [
    StringStruct('FileDescription', 'LocalTag 음악 태그 편집기'),
    StringStruct('FileVersion', '{VERSION}'),
    StringStruct('ProductName', 'LocalTag'),
    StringStruct('ProductVersion', '{VERSION}'),
    StringStruct('LegalCopyright', 'LocalTag')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])])
""")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    hiddenimports=["certifi", "localtag._version"],
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.QtPdf"],
)
# 쓰지 않는 Qt 플러그인(가상 키보드 입력기, PDF 이미지 읽기, 터치 장치)과 그 DLL,
# 소프트웨어 OpenGL(opengl32sw.dll, 약 20MB — 위젯 앱이라 쓰지 않음)은 넣지 않는다
DROP = ("/platforminputcontexts/", "/qpdf.", "/qtuiotouchplugin.", "opengl32sw.",
        "Qt6Quick", "Qt6Qml", "Qt6VirtualKeyboard", "Qt6Pdf", "Qt6Network", "Qt6OpenGL",
        "QtQuick", "QtQml", "QtPdf", "QtNetwork", "QtOpenGL")


def _keep(entry):
    dest = entry[0].replace(os.sep, "/")
    if "/translations/" in dest and not dest.endswith("_ko.qm"):
        return False  # Qt 번역 파일은 한국어만
    return not any(d in dest for d in DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]
a.datas += [("LocalTag.ico", os.path.join(SPECPATH, "LocalTag.ico"), "DATA")]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalTag",
          console=False, icon=os.path.join(SPECPATH, "LocalTag.ico"), version=VERSION_FILE)
coll = COLLECT(exe, a.binaries, a.datas, name="LocalTag")
