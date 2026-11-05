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
v1.0.8 — Windows 에서 냄 (태그 가져오기 최적화: 스토어 여러 곳·MusicBrainz 를 동시에 찾음, 검색 3초 → 1.4~1.9초).
v1.0.9 — Windows 에서 냄 (다시 실행, 일괄 변경의 자주 쓰는 정규식·정규식 도움말, `test_redo_regex.py`).
v1.0.10 — Windows 에서 냄 (태그 가져오기: 일본 스토어 등에서 앨범 이름이 로마자로 오던 것 → 곡에 붙은 원래 글자 이름을 씀, 검색 목록도). 맥용 1.0.8~1.0.10 zip 은 없다.
v1.0.11 — 맥에서 냄: 1.0.8~1.0.10 을 맥에서 tests/ 모두 PASS 확인. 맥에서 다시 실행 단축키가 Qt 기본값 ⌘Y 뿐이라 ⇧⌘Z 를 먼저 둠 (`IS_MAC` 일 때만, Windows 는 그대로).

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
v1.0.12~1.0.15 — 맥에서 냄: 정렬용 이름 열(모든 OS), MusicBrainz 커버 보충(모든 OS), **Music 보관함 점검·형식 변환은 macOS 전용**
v1.0.15 Windows zip — Windows 에서 합쳐 tests/ 16개 모두 PASS(형식 변환·보관함 점검 테스트는 Windows 에서 건너뜀), 메뉴에서 두 기능은 숨김. 같은 코드로 v1.0.15 에 Windows zip 을 붙임 (1.0.11~1.0.14 Windows zip 은 없다).
(`librarycheck.py` 는 iTunesLibrary — 메뉴도 `IS_MAC` 일 때만. `formatconv.py` 는 1.0.21 부터 Windows 도 ffmpeg 로 — 아래 v1.0.21).
v1.0.16~1.0.20 — 맥에서 냄 (실행 취소 전 다른 곳에서 바뀐 곡 묻기, 형식 변환 → Music 에 추가, 보관함 점검에서 앨범 맞추기, 태그 가져오기 정렬·확인, 필터 '찾을 곳').
v1.0.21 — Windows 에서 냄: 1.0.16~1.0.20 을 합쳐 tests/ 모두 PASS (`test_librarycheck.py` 의 Music 반영 확인은 macOS 만). **형식 변환을 Windows 에도** — 설치한 ffmpeg(PATH·winget·C:\ffmpeg) 로, ALAC 은 비트 깊이 그대로·디코딩 결과 같음, AAC 는 ffmpeg 내장 인코더(aac_mf 는 192k 까지라 안 씀), 콘솔 창 안 띄움, ffmpeg 가 없으면 winget 설치 안내. Music 에 추가는 macOS 만. **맥의 다음 릴리스는 1.0.22**.
v1.0.22~1.0.23 — 맥에서 냄 (Music 커버 다시 지정은 macOS 만, 커버 맞춤 여백 검정·흰색·투명은 모든 OS, 형식 변환 전에 한 번 더 묻기). Windows 에서 합쳐 tests/ 19개 모두 PASS (	est_cover_fix.py 의 저장 뒤 Music 반영 확인은 macOS 만), 실제 exe 로 형식 변환 확인 창(Ctrl+Z 표시)까지 확인. 같은 코드로 v1.0.23 에 Windows zip 을 붙임 (1.0.22 Windows zip 은 없다). **맥의 다음 릴리스는 1.0.24**.
v1.1.0~1.1.11 — 맥에서 냄 (도구 막대 사용자화, 앨범아트 추출 미리 보기, YouTube 업로드 순서 번호, 저장 빨라짐(오디오 위치가 그대로면 오디오를 읽지 않음), 클래식 작품·악장 나누기, 닫을 때 작업 기다리기 등). Windows zip 은 없다.
v1.1.12 — Windows 에서 냄: 1.1.0~1.1.11 을 합쳐 tests/ 27개 모두 PASS, 실제 exe 로 저장·되돌리기 확인. **Windows 에서 원본에 쓰다 실패했을 때 사본으로 바꾸는 길이 늘 실패하던 것 고침** — Windows 의 fsync(_commit)는 쓰기로 연 파일만 돼 읽기 전용으로 열면 EBADF (Windows 판 처음부터 있던 문제, `test_safewrite_skip.py` 가 찾음). 300곡 저장은 Windows 에서 2.3~3.3초로 1.1.8 전과 비슷 — 시간은 Defender 검사(파일 열기)와 fsync 에 들고 오디오 읽기는 원래 작았다. **맥의 다음 릴리스는 1.1.13**.
v1.1.13~1.1.14 — 맥에서 냄 (Music 반영 4~5배 빨라짐, 목록의 '생성된 날짜' 열). Windows 에서 합쳐 tests/ 28개 모두 PASS, 실제 exe 로 생성된 날짜 열(값·정렬·저장해도 그대로) 확인. 같은 코드로 v1.1.14 에 Windows zip 을 붙임.
v1.1.15 — Windows 에서 냄: **전체 최적화**. 실제 크기(MP3 10MB·AAC 8MB·FLAC 25MB, 커버 400KB) 300곡 4GB 를 실제 exe 에서: 저장 4.4~5.5초 → 1.3~1.4초, 실행 취소 4.1초 → 0.9초, 다시 실행 3.9~4.1초 → 0.9초, 백업 4161MB → 133MB, 창이 뜰 때까지 0.86초 → 0.47초. 태그가 커지는 저장 15.4초 → 7.3초, 그 되돌리기 13.5초 → 1.4초, 표 한 번 그리기 35 ms → 18 ms, 5000곡 불러오기 2.4초 → 1.9초.
  - 어디서 시간이 들었나: Windows 는 저장할 때마다 파일 전체를 메모리로 읽고(곡당 5~9 ms), 바뀐 파일을 처음 열 때 Defender 가 검사하고(5~10 ms), fsync 하고(2~7 ms), 백업을 통째로 복사했다(5 ms). → 쓴 부분만 메모리에 두는 사본(`MemStage`), 16개 동시 열기·fsync(`IoBatch`), 태그만 보관하는 백업(`tags.pack`).
  - 1MB 넘는 할당은 Windows 에서 쪽 오류로 느리다 → 256KB 씩 (읽어 비교 2.5 → 5.6 GB/s). mutagen 은 ID3 프레임마다 남은 태그를 복사한다 → 복사하지 않는 `_Tail` (MP3 읽기 24% 빨라짐). `mutagen.File` 은 부를 때마다 import 문 20여 개 → 형식 목록을 한 번만.
  - 글꼴: 맑은 고딕 + 대체 글꼴 2개를 처음부터 넣으면 글꼴 전체를 읽느라 0.53초, 맑은 고딕만이면 0.05초. 창을 먼저 띄우고 뒤에서 읽어 바꾼다. 일본어·중국어·드문 글자·기호까지 옛 exe 와 화면이 픽셀까지 같음을 확인. Qt 는 글꼴을 읽는 동안 GIL 을 놓지 않아 import 와 동시에 할 수는 없다.
  - GC 는 2~4초 중 0.02~0.07초뿐이라 손대지 않았다. 스레드 16 · 묶음 32 가 재 본 값 중 좋다 (8 은 느리고 32·64 는 차이가 거의 없음, 128 은 느림).
  - 함께 고친 것: 끝나지 않은 작업이 있어 바로 끝낼 때 `os._exit` 가 DLL 마무리에서 죽을 수 있던 것(`TerminateProcess` 로), 시작할 때 꺼진 네트워크 드라이브에서 멈출 수 있던 최근 폴더 확인, 시작할 때 창을 붙잡던 백업 정리(별도 스레드로).
  - **맥에 미치는 것** (맥에서 아직 돌려 보지 않았다): 남는 자리 정책(`_pad_keep`·`_pad_cover`), `_mf`·`_Tail`, 파일을 한 번만 여는 `AudioFile.load`, 실행 취소(파일이 그대로면 다시 읽지 않음·스냅샷을 함께 만듦), 표 모델 지름길, 백업 정리 스레드, 미리 불러오기를 한 조각씩, 글자 변환표를 창이 뜬 뒤에. `MemStage`·`IoBatch`·태그만 보관·256KB 조각·글꼴은 Windows 만 (맥은 `MEM_STAGE`·`TAGS_ONLY` 가 꺼져 있다 — Windows 에서 둘을 끄고 `test_fast_save.py` 로 맥과 같은 길을 시험했다. → 1.1.16 에서 맥도 `MEM_STAGE` 를 켰다). **맥의 다음 릴리스는 1.1.16** — 먼저 맥에서 tests/ 모두 돌린다.
v1.1.16 — 맥에서 냄: 1.1.15 를 맥에서 tests/ 모두 PASS 확인하고 **맥 전체 최적화**. 실제 크기(MP3 10MB·AAC 8MB·FLAC 25MB, 커버 400KB) 로 1.1.15 코드와 같은 벤치를 돌려 비교. 내장 APFS 300곡: 저장 0.94초 → 0.38초, 실행 취소 0.85초 → 0.32초, 다시 실행 0.88초 → 0.32초, 가사를 처음 넣어 오디오가 밀리는 저장 16.1초 → 7.1초, 아주 큰 태그 13.1초 → 1.8초, 커버 교체 7.0초 → 1.6초 (이들의 되돌리기 2.3~8.5초 → 0.35~1.2초), 가사가 있는 곡 읽기 0.33초 → 0.08초. exFAT **디스크 이미지** 90곡 (실제 외장 디스크에는 쓰지 않았다): 저장 13.1초 → 0.24초, 실행 취소 9.3초 → 0.25초, 가사 24.0초 → 4.6초, 커버 12.7초 → 2.6초. 창이 뜨기까지 0.66초 → 0.59초, 앱 71MB → 68MB (서명한 최종 빌드, 커밋 제목의 0.58초·67MB 는 서명 전 시험 빌드 값). 처음 읽기(태그가 작은 곡)와 정렬·필터는 그대로다.
  - 어디서 시간이 들었나: 맥은 저장할 때마다 작업 사본을 디스크에 만들었다. APFS 복제본을 만들고 지우는 데 곡당 약 2 ms, 복제본을 못 만드는 exFAT 은 곡 전체를 복사했다 (곡당 146 ms). → 맥도 `MEM_STAGE = True`. 백업은 복제본을 먼저 시도하고(`clone_preserving`, 복사로 넘어가지 않는다), 못 만들면 오디오 밖만, 오디오 위치를 모르는 형식(OGG 등)은 통째로.
  - UTF-16 ID3(가사·TXXX): mutagen 의 `decode_terminated` 가 두 바이트씩 파이썬으로 훑어 가사가 있는 곡의 읽기·되돌리기가 느렸다 → `tagio._decode_terminated_fast` (**모든 OS**. BOM 이 없거나 다른 인코딩은 원래 함수로. 48만 번 무작위로 원래 함수와 비교).
  - Music: `music._run_script` 가 100 ms 마다 확인해 명령 한 번에 최대 0.1초 기다리던 것을 `select.poll` 로 없앰 (맥 전용). 보내는 스크립트는 그대로다. Music 없이 실제 osascript 로만 시험했고(`test_music_script.py`) 실제 Music 반영은 돌려 보지 않았다.
  - 켜는 시간: `music.py` 의 `subprocess`·`plistlib`·`tempfile` 등을 함수 안으로 (맥), 시작할 때의 백업 정리를 2초 뒤로 (**모든 OS**), `LocalTag.spec` 의 excludes 에 `multiprocessing`·`bz2`·`lzma`·`compression.zstd`·`_pydecimal`·`tarfile`·`PySide6.QtDBus` (맥 40 ms, 앱 3MB. `QtDBus` 는 파이썬 모듈만 빠진다 — `QtGui` 가 링크하는 프레임워크는 남겨야 한다).
  - 확인: 맥 tests/ 30개 PASS, `test_fast_save.py` 를 exFAT 이미지 위에서도 PASS (복제본을 실제로 못 만드는 곳), 빌드한 앱에서 모듈 40개·형식 5개의 불러오기·저장·되돌리기·백업·HTTPS 인증서·osascript PASS.
  - **Windows 에서 할 일:** 합쳐서 tests/ 모두 돌린 뒤 v1.1.16 에 zip 을 붙인다 (`.\release_win.ps1`). Windows 에 닿는 코드는 UTF-16 지름길과 백업 정리 시각뿐이고 `TAGS_ONLY`·`IoBatch` 는 그대로다. `LocalTag-win.spec` 은 고치지 않았다 — 같은 excludes 를 넣어 볼 만하지만 Windows 빌드에서 따로 확인해야 한다.
