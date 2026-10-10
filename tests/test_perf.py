"""uniview.perf: the background-work throttle and the title bar stats text."""

from uniview.perf import Throttle, stats_text


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_lag_raises_level_and_calm_lowers_it():
    clock = Clock()
    t = Throttle(clock)
    assert t.level == 0 and t.reason() == ""
    assert t.report_lag(300) and t.level == 1
    assert t.report_lag(2000) and t.level == 2
    clock.t += 1
    assert not t.report_lag(0) and t.level == 2  # not calm long enough yet
    clock.t += Throttle.CALM_SECONDS
    assert t.report_lag(0) and t.level == 1
    clock.t += Throttle.CALM_SECONDS
    assert t.report_lag(0) and t.level == 0


def test_memory_level_wins_and_explains():
    t = Throttle(Clock())
    assert t.report_memory(95) and t.level == 2
    assert "memory" in t.reason()
    assert t.report_memory(50) and t.level == 0


def test_pause_waits_while_user_active():
    clock = Clock()
    t = Throttle(clock)
    t.user_active(0.25)
    slept = []

    def sleep(s):
        slept.append(s)
        clock.t += s

    t.pause(sleep)
    assert sum(slept) >= 0.25 and slept[-1] == 0.0  # waited out the activity, then level-0 sleep


def test_stats_text():
    assert stats_text({}) == ""
    text = stats_text({"app_ram": 3 * 2**30, "sys_ram_pct": 71, "cpu": 14.4, "gpu": 23.0, "vram": 640 * 2**20})
    assert text == "RAM 3.0 GB (system 71%) · CPU 14% · GPU 23% · VRAM 640 MB"
