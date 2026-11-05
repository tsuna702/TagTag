"""osascript 실행(_run_script): 실제 osascript 로 시험한다 (Music 을 부르지 않는 스크립트만).

- log 줄과 진행 표시를 그대로 받고, 끝나면 바로 돌아온다 (쉬면서 기다리지 않는다)
- 많은 줄도 막히지 않음 · 한글 · 인자
- 오류 문구, 취소, 응답 없음

실행: python tests/test_music_script.py
"""
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, finish  # noqa: E402

from localtag import music  # noqa: E402
from localtag.osutil import IS_MAC  # noqa: E402

if not IS_MAC:
    print("맥 전용")
    finish()
    sys.exit(0)

never = lambda: False  # noqa: E731
seen = []
lines = music._run_script('''on run argv
    log "LOCALTAG_PROGRESS:1"
    log "LOCALTAG_RESULT:" & (item 1 of argv) & tab & "가나다 🎵"
    log "LOCALTAG_PROGRESS:2"
    return "표준 출력은 쓰지 않는다"
end run''', ["경로 값"], seen.append, never)
check(seen == [1, 2], f"진행 표시 {seen}")
check(lines == ["LOCALTAG_RESULT:경로 값\t가나다 🎵"], f"log 줄 · 한글 · 인자 {lines}")

# 많은 줄: 파이프가 가득 차도 막히지 않고 순서대로
count = 4000
lines = music._run_script(f'''repeat with i from 1 to {count}
    log "LOCALTAG_INFO:" & i & "{'가' * 40}"
end repeat''', [], seen.append, never)
check(len(lines) == count and lines[0].startswith("LOCALTAG_INFO:1가") and lines[-1].startswith(f"LOCALTAG_INFO:{count}가"), f"많은 줄 ({len(lines)})")

# 끝나면 바로 돌아온다: 0.1초씩 쉬며 확인하지 않는다 (프로세스가 다 끝나기를 기다리는 몇 ms 만)
real_sleep = time.sleep
slept = []


def counted_sleep(s):
    slept.append(s)
    real_sleep(s)


music.time.sleep = counted_sleep
for _ in range(10):
    slept.clear()
    music._run_script('log "x"', [], seen.append, never)
    if sum(slept) >= 0.05:
        break
music.time.sleep = real_sleep
check(sum(slept) < 0.05, f"기다리며 쉬지 않음 {slept}")


def timed(fn):
    runs = []
    for _ in range(5):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs)


base = timed(lambda: subprocess.run(["/usr/bin/osascript", "-e", 'log "x"'], capture_output=True))
ours = timed(lambda: music._run_script('log "x"', [], seen.append, never))
print(f"   osascript 직접 {base * 1000:.0f} ms · _run_script {ours * 1000:.0f} ms")
check(ours < base + 0.05, f"osascript 를 그냥 부르는 것과 거의 같은 시간 ({ours * 1000:.0f} / {base * 1000:.0f} ms)")


def error_of(script, **kw):
    try:
        music._run_script(script, [], seen.append, kw.pop("cancelled", never), **kw)
    except music.MusicError as e:
        return str(e)
    return None


check(error_of('error "권한 없음" number -1743') == music.PERMISSION_HELP, "권한 오류 문구")
check(error_of('error "꺼져 있음" number -600') == "Apple Music을 먼저 열어 주세요.", "Music 이 꺼져 있을 때 문구")
check(error_of('error "Apple Music을 먼저 열어 주세요."') == "Apple Music을 먼저 열어 주세요.", "스크립트가 알린 문구")
msg = error_of('log "앞선 줄"\nerror "다른 오류" number -50')
check(msg and msg.startswith("Music 작업을 끝내지 못했습니다.") and "앞선 줄" in msg and "다른 오류" in msg, f"그 밖의 오류 {msg}")

# 취소·응답 없음: 오래 걸리는 osascript 를 바로 끝낸다
t0 = time.perf_counter()
marks = [t0 + 0.2]
msg = error_of("delay 30", cancelled=lambda: time.perf_counter() > marks[0])
took = time.perf_counter() - t0
check(msg and msg.startswith("취소했습니다") and took < 1.0, f"취소 → 바로 끝냄 ({took:.2f}초) {msg}")
t0 = time.perf_counter()
msg = error_of("delay 30", idle_limit=0.3)
took = time.perf_counter() - t0
check(msg and "오래 응답하지 않습니다" in msg and took < 1.0, f"응답 없음 → 바로 끝냄 ({took:.2f}초)")
check(not subprocess.run(["/usr/bin/pgrep", "-f", "osascript -e delay 30"], capture_output=True).stdout, "osascript 가 남아 있지 않음")

# 끝나는 중인 osascript 를 kill 하지 않는다 — 파이프는 프로세스가 다 끝나기 전에 닫힌다. 성공·실패는 끝 코드로 가리므로 끝나기를 기다린다
kills = []
real_kill = subprocess.Popen.kill


def counted_kill(self):
    kills.append(self.pid)
    real_kill(self)


subprocess.Popen.kill = counted_kill
for _ in range(30):
    music._run_script('log "x"', [], seen.append, never)
check(not kills, f"정상으로 끝날 때는 kill 하지 않음 ({len(kills)}번)")
check(error_of('error "다른 오류" number -50') and not kills, "오류로 끝날 때도 kill 하지 않고 끝 코드를 받음")
check(error_of("delay 30", cancelled=lambda: True) and len(kills) == 1, "취소할 때만 kill")
subprocess.Popen.kill = real_kill

# 진행 표시가 오면 응답 없음 시간을 다시 잰다
seen.clear()
msg = error_of('''repeat with i from 1 to 6
    delay 0.1
    log "LOCALTAG_PROGRESS:" & i
end repeat''', idle_limit=0.5)
check(msg is None and seen == [1, 2, 3, 4, 5, 6], f"진행 중이면 끊지 않음 {msg} {seen}")
finish()
