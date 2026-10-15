# Windows 판 만들기

Windows PC 에서 `windows` 브랜치로 작업한다. 기준 버전은 0.9.1 (macOS·Windows 합침).
화면(PySide6)과 태그 읽기·쓰기(mutagen)는 그대로 Windows 에서 돈다. 손볼 곳은 아래 맥 전용 부분이다.

## 시작하기 (Windows PC)

```powershell
git clone git@github.com:tsuna702/TagTag.git
cd TagTag
git switch windows
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt pyinstaller
.venv\Scripts\python -m localtag          # 앱 실행
.venv\Scripts\python tests\smoke.py       # 빠른 확인 (지금은 맥 전용 부분 때문에 실패할 수 있음 — 1단계에서 고친다)
```

- 한글 파일명이 깨져 보이면 `git config core.quotepath false`.
- 줄 끝: `.gitattributes` 가 `*.sh` 는 LF 로 고정한다 (맥에서 `release.sh` 가 깨지지 않게).

## 1단계: 맥 전용 부분 갈라 두기

운영체제 판별은 `sys.platform == "darwin"` / `"win32"` 로 한다. 맥 동작은 바꾸지 않는다.

| 곳 | 지금 (macOS) | Windows 에서 |
|---|---|---|
| `safewrite.py` `copy_preserving` | `libSystem` `copyfile`(APFS clone) → 안 되면 `/bin/cp -p` | `shutil.copy2` (시각 보존). 이미 있으면 거부하는 동작은 유지 |
| `safewrite.py` `SF_DATALESS` (iCloud 미다운로드 확인) | `st_flags` | Windows 에는 `st_flags` 가 없어 저절로 건너뜀 — 확인만. OneDrive "온라인 전용" 파일은 `st_file_attributes` 의 `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`(0x400000) 로 같은 처리를 할 수 있다 |
| `safewrite.py` `write_back` · `os.replace` | 원본에 달라진 부분만 다시 씀 | Windows 는 다른 프로그램이 열어 둔 파일을 바꿀 수 없다 (`PermissionError`) → 오류 문구를 "다른 프로그램이 파일을 쓰는 중" 으로 |
| `mainwindow.py` Finder 에서 보기 (`open -R`) | `subprocess.run(["open","-R",path])` | `explorer /select,"경로"` |
| `backupdialog.py` 폴더 열기 · `extractdialog.py` 저장 폴더 보기 | `open` | `os.startfile(folder)` / `explorer /select,` |
| `extractdialog.py` `default_folder` | `/usr/bin/defaults read local.albumart-extractor` | 건너뛰고 `사진\앨범아트` (QStandardPaths.PicturesLocation) |
| `music.py` 전체 (Music 메뉴, 저장 후 자동 반영, 권한, 끌어다 놓기) | AppleScript · TCC · iTunesLibrary | Windows 에서는 **Music 메뉴와 관련 설정을 숨긴다**. `music_auto` 는 늘 꺼짐. `drag_has_music` 은 False |
| `extract.py` 내 보관함 탭 | Music 보관함 읽기 (JXA) | 탭을 숨긴다 (링크·목록의 곡·YouTube 탭은 그대로 됨) |
| `mainwindow.py` `_maybe_ask_permissions` | `platformName() == "cocoa"` 일 때만 | 이미 맥에서만 돈다 — 확인만 |
| `filetrack.py` 옮겨진 파일 찾기 | inode, Music 미디어 폴더 이름(`/Media/`) | NTFS 도 `st_ino`(파일 번호)가 있다. 경로 구분자 `\` 처리 확인. 미디어 폴더 대신 같은 드라이브의 원래 상위 폴더에서 찾기 |
| `mainwindow.py` `app_version` | `.app` 의 `Info.plist` | `.exe` 옆에 둔 버전(빌드 때 `localtag/_version.py` 를 만들어 넣는 식)으로 |
| `mainwindow.py` `_app_icon` | `LocalTag.icns` | `packaging/LocalTag.ico` (make_icon.py 로 png → ico) |
| 단축키 표기 | ⌘ ⌥ ⇧ | Qt 가 Ctrl/Alt 로 바꿔 준다. 다만 **UI 문구·툴팁·README 에 직접 쓴 ⌘ 기호**(예: "⌘R", "⌥⌘↑")는 OS 에 맞춰 보이게 바꿔야 한다 — 작은 도우미 함수 하나로 |
| 메뉴 | macOS 는 About·종료가 앱 메뉴로 옮겨짐 (`AboutRole`, `QuitRole`) | Windows 는 그대로 파일 메뉴 / 도움말 메뉴에 둔다 |
| 폰트 | 시스템 기본 | 한글·일본어가 섞여 보이므로 `Malgun Gothic` / `Yu Gothic UI` 확인 |

**진행 (2026-10-14, Windows PC):** 위 표를 거의 다 했다. 운영체제별 도우미는 `localtag/osutil.py`
(`IS_MAC`·`IS_WIN`, `reveal`, `open_folder`, `keys()` — ⌘ 기호를 Ctrl 표기로). 표에 없던 것도 고쳤다:
Windows 에는 `os.pread`/`os.pwrite` 가 없고, `os.open` 은 `O_BINARY` 없이 텍스트 모드로 열며, 폴더 fsync 가 안 된다
(`safewrite.py`). 목록에서 제거는 Windows 에서 Delete 키. `make_icon.py` 가 `LocalTag.ico` 도 만든다.
남은 것: `filetrack.py` 를 실제 옮긴 파일로 확인, `_version.py` 는 2단계 빌드에서 만든다. 맥에서 smoke PASS 확인.

**끝났는지 확인:** `tests\smoke.py` 가 Windows 에서 PASS. 맥에서도 PASS 여야 한다 (맥 쪽은 사용자가 맥에서 확인하거나 다음에 맥 세션에서).

## 2단계: 빌드

- `packaging/LocalTag-win.spec` 을 새로 만든다 (`packaging/LocalTag.spec` 은 macOS 용 — `BUNDLE`·`Info.plist`).
  - `--windowed` (콘솔 창 없음), 아이콘 `LocalTag.ico`, onedir (`dist\LocalTag\LocalTag.exe`).
  - macOS spec 처럼 쓰지 않는 Qt 플러그인·번역을 빼서 크기를 줄인다 (DROP 목록 참고).
  - 버전은 macOS spec 의 `CFBundleShortVersionString` 을 읽어 같은 번호를 쓴다 (버전 번호는 한 곳에서만 올린다).
- `build_win.ps1`: venv 만들기 → 설치 → pyinstaller → `dist\LocalTag-<버전>-windows.zip`.

**진행 (2026-10-14):** 됐다. `.\build_win.ps1` → `dist\LocalTag\LocalTag.exe` (폴더 약 64MB, zip 약 27MB).
spec 이 빌드 때 `localtag/_version.py`(저장소에 넣지 않음)와 exe 버전 정보(탐색기 속성 → 자세히)를 만든다.
Windows DLL 이름은 `Qt6Quick.dll` 처럼 `Qt6` 으로 시작해서 DROP 목록을 따로 두었고, `opengl32sw.dll`(약 20MB)도 뺀다.

## 3단계: 릴리스

버전 번호는 macOS 와 같이 쓴다 (`packaging/LocalTag.spec`). 두 가지 중 하나 — 사용자와 정한다:

- **A. Windows PC 에서 올리기:** `release_win.ps1` — 같은 태그의 릴리스가 있으면 `gh release upload v<버전> 파일.zip` 으로
  Windows zip 을 붙이고, 없으면 새로 만든다 (노트는 release.sh 와 같은 방식, 설치 안내만 Windows 용).
- **B. GitHub Actions:** `.github/workflows/windows.yml` — 태그가 올라오면 windows-latest 에서 빌드해 릴리스에 붙인다.
  저장소에 워크플로를 올리는 일이라 **사용자에게 먼저 묻는다.**

**정함 (2026-10-14): A.** `.\release_win.ps1` — windows 브랜치·커밋 안 한 변경 없음 확인 → 빌드 → push →
같은 태그가 있으면 zip 을 붙이고 노트 끝에 "Windows 판"(그 태그 이후 windows 브랜치 커밋)과 설치 안내를 더한다.
이미 Windows zip 이 붙어 있으면 멈춘다. 태그가 없으면 windows 브랜치를 대상으로 새로 만든다
(그러면 맥의 release.sh 는 같은 태그가 있어 멈추므로, 보통은 맥에서 먼저 릴리스하고 Windows 를 붙인다).
필요: `gh` (winget install GitHub.cli) 와 사용자가 직접 한 `gh auth login`.

**릴리스 기록:** v0.8.2 — macOS 릴리스에 Windows zip 을 붙임. v0.8.3 — Windows 만 (windows 브랜치에서 새로 만듦,
옮겨진 파일 찾기 고침). v0.9.0 — macOS 만 (맥에서 windows 를 합치기 전에 나감: 태그 가져오기·앨범 점검·칸에서 고치기).
v0.9.1 — 맥에서 `windows` 를 `main` 으로 합친 첫 릴리스 (맥에서 테스트 모두 PASS). **이 릴리스에 Windows zip 이 아직 없다**
→ Windows PC 에서 `main` 을 받아 `.\release_win.ps1` 로 v0.9.1 에 붙인다. 이제 두 브랜치는 같으니, 앞으로는 어느 쪽에서든
릴리스하기 전에 `git fetch` 로 다른 쪽 커밋을 먼저 합친다.
v1.0.0 — 맥 릴리스에 Windows zip 을 붙임 (2026-10-15). Windows 에서 tests/ 모두 PASS, 받은 exe 로 저장·되돌리기·칸에서 F2 로 고치기·앨범 점검·태그 가져오기 창을 실제로 확인. v0.9.1~0.9.11 에는 Windows zip 이 없다.
v1.0.1 — Windows 에서 냄 (저장 최적화: 메모리 작업 사본·백업 기록 묶음, 맥에도 해당하는 백업 묶음 포함). 맥에서 tests/ (새 `test_save_stage_backup.py` 포함) PASS 를 먼저 확인한다.
v1.0.2 — Windows 에서 냄 (태그 가져오기: '한국'을 고르면 일본 스토어에서 찾았어도 곡 ID 로 한국 스토어 이름을 받음, `test_tagsource_country.py`). 맥에서 tests/ 모두 PASS 확인 뒤 같은 코드로 맥용 zip(서명)을 v1.0.2 에 붙임 — 맥용 1.0.1 zip 만 없다. 맥의 다음 릴리스는 1.0.3.
v1.0.3~1.0.7 — 맥에서 냄 (태그 가져오기 표·MusicBrainz 등). Windows zip 은 없다 — Windows 에서 합쳐 tests/ 모두 PASS 확인 뒤 1.0.8 로 이어 감.
v1.0.8 — Windows 에서 냄 (태그 가져오기 최적화: 스토어 여러 곳·MusicBrainz 를 동시에 찾음, 검색 3초 → 1.4~1.9초). **맥의 다음 릴리스는 1.0.9** — 맥에서 tests/ PASS 를 먼저 확인한다.

설치 안내 (릴리스 노트): zip 을 풀고 `LocalTag.exe` 실행. 서명하지 않은 앱이라 처음에 "Windows의 PC 보호" 가 뜨면
**추가 정보 → 실행**.

## 나중에 (원하면)

- iTunes for Windows 연동 (COM: `win32com.client.Dispatch("iTunes.Application")`) — 새 Apple Music 윈도우 앱은 외부 조종 API 가 없다.
- 탐색기 우클릭 메뉴 "LocalTag 로 열기".

## 지금까지의 사정 (맥 세션에서 넘어온 것)

- 사용자는 YouTube 에서 JDownloader 로 받은 m4a 와 imojist 등의 고음질 곡을 Apple Music 에 넣어 쓴다.
  그래서 YouTube 제목 정리·커버 짝짓기·같은 곡 찾기·일괄 변경을 만들었다.
- 앨범아트 추출기(예전 별도 앱)는 0.7.0 에서 LocalTag 에 합쳤고, 예전 앱은 지웠다.
- macOS 테스트 실행 폴더는 맥의 임시 폴더에 있었고 저장소에는 없다. 저장소의 `tests/` 가 앞으로의 기준이다.
