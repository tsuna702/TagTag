# LocalTag — 작업 지침

Mp3tag 를 본뜬 음악 태그 편집기. Python 3 + PySide6(Qt) + mutagen. GitHub `tsuna702/TagTag` (공개 저장소).
사용자와는 **한국어**로 이야기하고, 앱 화면의 글도 모두 한국어로 쓴다. 기능 설명은 README.md 에 있다.

## 브랜치

- `main` — macOS 판. 릴리스는 여기서 나간다.
- `windows` — Windows 판 작업 (Windows PC 에서). 계획과 체크리스트: **[docs/WINDOWS.md](docs/WINDOWS.md)**.
  macOS 판이 계속 그대로 돌아가야 한다. 맥 전용 부분은 지우지 말고 운영체제에 따라 갈라 둔다.
  `main` 에 새 커밋이 생기면 `windows` 로 merge 해서 따라간다.
  **어느 쪽에서든 릴리스하기 전에 `git fetch` 로 다른 브랜치의 새 커밋을 확인하고 먼저 합친다**
  (두 PC 가 따로 릴리스해서 버전이 엇갈린 적이 있다 — 0.9.0 은 Windows 쪽 0.8.3 을 합치지 않고 나갔다).

## 늘 지키는 규칙

1. **고치면 릴리스까지 (묻지 않음).** 앱을 고치면 버전을 올리고, 한국어 제목으로 커밋하고, 릴리스한다.
   - 버전: `packaging/LocalTag.spec` 의 `CFBundleShortVersionString`·`CFBundleVersion` (작은 수정은 패치 버전 +1).
   - 릴리스 노트는 지난 태그 이후 **커밋 제목**으로 자동으로 만든다 → 제목을 사용자가 읽을 수 있게 쓴다
     (예: `태그 비교(⇧⌘D) 추가 — 고른 곡을 나란히 놓고 다른 줄을 노란색으로 (0.8.0)`).
   - macOS: `./release.sh` (빌드 → zip → `gh release create`). 커밋 안 한 변경이 있거나 같은 태그가 있으면 멈춘다.
   - Windows: `.\release_win.ps1` (windows 브랜치에서. 같은 버전 릴리스에 Windows zip 을 붙인다 — docs/WINDOWS.md 의 "릴리스").
   - 커밋 끝에 `Co-Authored-By:` 줄을 붙인다.
2. **테스트는 사용자의 실제 환경을 건드리지 않는다.** `tests/harness.py` 를 쓴다.
   - 설정은 임시 INI 로 바꿔 끼운다. 실제 설정(macOS plist, Windows 레지스트리 `HKCU\Software\LocalTag`)에 `clear()` 금지.
   - 앱 데이터(백업 등)는 `QStandardPaths.writableLocation` 을 실행 폴더로 돌린다.
     `setTestModeEnabled` 만 쓰면 `~/.qttest` 같은 곳에 쌓인다 — 예전에 5GB까지 커져 사용자가 지적했다.
   - Apple Music 자동 반영을 켠 채로 저장하지 않는다 (harness 가 확인한다). 실제 Music 보관함은 읽기만.
   - 테스트를 돌리면 **이번 실행 폴더만 남기고 이전 테스트 파일은 지운다** (harness 가 `tests/runs/` 에서 자동으로).
   - 샘플은 저장소 `samples/` 를 실행 폴더로 복사해서 쓴다. 원본 샘플을 고치지 않는다.
   - 실행: `python tests/smoke.py`, `python tests/test_*.py` (Windows: `.venv\Scripts\python tests\smoke.py`).
     새 기능에는 `tests/` 에 테스트를 더한다. 인터넷이 필요한 기능은 가짜 데이터로 시험한다.
3. **서명:** macOS 는 사용자가 만든 **자체 서명 인증서 `LocalTag Signing`**(맥 로그인 키체인, 2027-10-15 만료)으로
   서명한다 — `build_app.sh` 가 인증서가 있으면 자동으로 쓴다(1.0.0 부터). 서명 주체가 같아서 새 버전마다 Music 권한을
   다시 묻지 않는다. 사용자 혼자 쓰는 앱이라 유료 Developer ID·공증은 하지 않는다. 키체인의 인증서를 만들거나 지우는
   일은 사용자가 직접 한다 (만료가 다가오면 새로 만들도록 안내 — 유효 기간 3650일 권장, 바꾼 첫 버전에서 권한을 한 번 더 묻는다).
   Windows 는 서명하지 않은 `.exe` (SmartScreen 경고는 감수).
4. **Music 에서 곡을 경로로 찾지 않는다 (`whose location is`).** 그렇게 찾으면 Music 이 보관함의 곡 파일을
   확인하다가 바뀐 파일을 한꺼번에 다시 읽고 폴더도 옮겨, 앨범 화면에서 곡이 갈라져 보인다(데이터는 맞음).
   그래서 저장하기 **전에** 제목·앨범으로 찾아 persistent ID 를 받아 두고(`music.prelookup`), 저장 뒤에는 ID 로만
   한 곡씩 refresh 한다. 0.9.7 부터 이렇게 해서 반영 속도 '빠르게'(0.8초 → 0.9.11 부터 실제 0.4초, 확인 간격 0.1초)에서도, Music 을 다시 켜지 않아도
   갈라지지 않음을 사용자가 확인했다 (2026-10-15). 경로 찾기는 ID 를 못 찾은 곡에만 쓰는 마지막 수단이다.
5. 토큰·비밀번호를 대신 입력하지 않는다. `gh` 로그인 같은 것은 사용자가 직접 한다.

## 코드 습관

- 주석·docstring·UI 문구는 한국어, 짧게. 주변 코드의 말투와 밀도에 맞춘다.
- 태그 쓰기는 **안전 저장**(`safewrite.safe_modify`): 사본에 쓰고 오디오 데이터가 바이트 단위로 같은지 확인한 뒤
  원본에는 달라진 부분만 다시 쓴다 (파일 번호가 그대로라 Music 이 곡을 놓치지 않는다). 이 흐름을 우회하지 않는다.
  Windows 는 작업 사본을 **메모리**(`MemStage`)에 둔다 — 디스크에 새로 만든 파일은 열 때마다 Defender 가 검사해
  곡당 몇 ms 씩 든다 (1.0.1: 300곡 저장 6.6초 → 2.1초). 그래서 사본을 받는 함수는 경로 대신 파일 객체도 받을 수 있고,
  mutagen 에 넘길 때는 꼭 `rewind(path)` 를 거친다 (`mf.save()` 처럼 인자 없이 부르지 말고 `mf.save(rewind(path))`).
- 백업 기록(journal.log)은 `BACKUP_BATCH` 곡씩 백업 사본을 먼저 만들고 한 번에 fsync 한다 — 원본을 바꾸기 전에 확정된다.
- 태그 가져오기의 인터넷 요청은 서로 기다릴 필요가 없으면 `tagsource._together` 로 동시에 보낸다 (스토어 여러 곳 + MusicBrainz).
- 파일을 바꾸는 작업은 `Plan` 목록을 만들어 `MainWindow.apply_plans` 로 적용한다 → 백업·실행 취소·Music 반영이 따라온다.
- 열 추가는 `model.COLUMNS` **끝에** 붙인다 (저장해 둔 열 배치가 어긋나지 않게).
- 무거운 창 모듈은 함수 안에서 import 하고 `MainWindow._preload` 목록에도 넣는다.

## 파일 지도 (localtag/)

| 파일 | 하는 일 |
|---|---|
| `tagio.py` | 파일 읽기·쓰기(mutagen), `AudioFile`, 커버, 스냅샷(실행 취소) |
| `safewrite.py` | 안전 저장, 복사(macOS `copyfile` clone) |
| `model.py` | 목록 표 모델·열·정렬 |
| `mainwindow.py` | 메인 창, 메뉴, `apply_plans`, 실행 취소 |
| `panel.py` | 왼쪽 태그 패널 |
| `script.py` / `filterexpr.py` | 포맷 문자열 / 필터 |
| `converters.py` | 변환기(태그↔파일명 등), 자동 번호 매기기 |
| `actions.py` / `actiondialogs.py` | 액션·액션 그룹 |
| `rename.py` / `renamedialog.py` | 곡명·파일명 일괄 변경 |
| `ytclean.py` / `ytdialog.py` | YouTube 제목 정리 |
| `dupes.py` / `compare.py` | 같은 곡 찾기 / 태그 비교 |
| `tagsource.py` / `tagsourcedialog.py` | Apple Music(iTunes API + 작곡가 등은 웹 플레이어 공개 토큰으로 카탈로그 amp-api)·MusicBrainz(보충, 초당 1번·연락처 User-Agent)에서 태그 가져오기, 곡 짝짓기 |
| `toolbar.py` | 도구 막대 단추 목록(ITEMS)·기본 배치·사용자화 창 (설정 `toolbar_items`) |
| `albumcheck.py` | 앨범 점검 (Music 에서 앨범이 갈라지는 태그 차이) |
| `artwork*.py`, `covermatch.py`, `extract*.py` | 앨범아트 찾기·짝짓기·추출 |
| `backup*.py` | 원본 백업 |
| `music.py`, `filetrack.py` | **macOS 전용** Apple Music 연동, 옮겨진 파일 찾기 |
| `formatconv.py` | 형식 변환 (macOS afconvert · Windows ffmpeg, FLAC → ALAC·AAC, 태그·커버 옮김. Music 에 추가는 macOS 만) |
| `librarycheck.py` | **macOS 전용** Music 보관함 점검 (iTunesLibrary 로 읽기만 — Music 에 스크립트를 보내지 않음) |
