# PyInstaller 설정: ./build_app.sh 로 빌드
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
AUDIO_TYPES = ["public.audio", "public.mp3", "org.xiph.flac", "public.mpeg-4-audio",
               "com.apple.m4a-audio", "public.aiff-audio", "com.microsoft.waveform-audio",
               "com.microsoft.windows-media-wma", "org.xiph.ogg-audio"]

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[ROOT],
    hiddenimports=["certifi"],
    excludes=["tkinter", "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick",
              "PySide6.QtWebEngineCore", "PySide6.QtMultimedia", "PySide6.QtPdf"],
)
# 쓰지 않는 Qt 플러그인(가상 키보드 입력기, PDF 이미지 읽기, 터치 장치)과
# 그 플러그인만 쓰는 프레임워크(QtQuick·QtQml·QtPdf·QtNetwork 등, 약 20MB)는 넣지 않는다
DROP = ("/platforminputcontexts/", "/libqpdf.", "/libqtuiotouchplugin.",
        "QtQuick", "QtQml", "QtVirtualKeyboard", "QtPdf", "QtNetwork", "QtOpenGL")


def _keep(entry):
    dest = entry[0].replace(os.sep, "/")
    if "/translations/" in dest and not dest.endswith("_ko.qm"):
        return False  # Qt 번역 파일은 한국어만
    return not any(d in dest for d in DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LocalTag",
          console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="LocalTag")
app = BUNDLE(
    coll,
    name="LocalTag.app",
    icon=os.path.join(SPECPATH, "LocalTag.icns"),
    bundle_identifier="io.github.tsuna702.localtag",
    info_plist={
        "CFBundleDisplayName": "LocalTag",
        "CFBundleShortVersionString": "1.1.7",
        "CFBundleVersion": "1.1.7",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "LocalTag",
        # Music 새로 읽기·선택한 곡 가져오기에 필요한 '자동화' 권한 안내 문구
        "NSAppleEventsUsageDescription": "편집한 곡을 Apple Music에서 새로 읽고, Music에서 선택한 곡의 파일 위치를 가져오기 위해 Music을 제어합니다.",
        # 'Apple Music 보관함 접근'(미디어 및 Apple Music) 권한 안내 문구
        "NSAppleMusicUsageDescription": "Apple Music 보관함에서 편집한 곡을 찾아 새로 읽게 하고, 곡의 파일 위치를 가져오기 위해 필요합니다.",
        # Dock 아이콘에 오디오 파일이나 폴더를 끌어다 놓을 수 있게
        "CFBundleDocumentTypes": [
            {"CFBundleTypeName": "Audio", "CFBundleTypeRole": "Editor",
             "LSHandlerRank": "Alternate", "LSItemContentTypes": AUDIO_TYPES},
            {"CFBundleTypeName": "Folder", "CFBundleTypeRole": "Viewer",
             "LSHandlerRank": "Alternate", "LSItemContentTypes": ["public.folder"]},
        ],
    },
)
