"""The loading screen process's stdin protocol (uniview/ui/loading_child.py)."""
import io
import json

from uniview.ui.loading_child import _Commands


class _Sig:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def emit(self, *args):
        self.log.append((self.name, *args))


class _Bridge:
    def __init__(self, log):
        self.event, self.stage, self.progress = _Sig(log, "event"), _Sig(log, "stage"), _Sig(log, "progress")


def test_commands_route_to_bridge_and_control():
    log = []
    lines = [{"k": "event", "n": "queue", "p": [["a.assets", 10]]}, {"k": "stage", "t": "Reading"},
             {"k": "progress", "d": 3, "t": 9}, {"k": "size", "v": [800, 600]}]
    stream = io.StringIO("".join(json.dumps(m) + "\n" for m in lines) + "not json\n")
    _Commands(_Bridge(log), _Sig(log, "control")).run(stream)
    assert log == [("event", "queue", [["a.assets", 10]]), ("stage", "Reading"), ("progress", 3, 9),
                   ("control", "size", [800, 600]), ("control", "quit", None)]


def test_quit_stops_reading():
    log = []
    stream = io.StringIO('{"k":"quit"}\n{"k":"stage","t":"late"}\n')
    _Commands(_Bridge(log), _Sig(log, "control")).run(stream)
    assert log == [("control", "quit", None)]


def test_read_watch_follows_bytes_and_shows_the_files_bits(tmp_path):
    from uniview.ui.loading_child import ReadWatch
    path = tmp_path / "a.assets"
    path.write_bytes(bytes(range(256)) * 4)  # 1024 bytes
    count = [1000]
    watch = ReadWatch(lambda: count[0])
    watch.queue([["a.assets", 1024, str(path)]])
    assert watch.tick() is None  # nothing being read yet
    watch.at(0)
    count[0] += 512
    info = watch.tick()
    assert info["i"] == 0 and info["frac"] == 0.5 and info["bytes"] == 512
    assert info["bits"] == "".join(f"{b:08b}" for b in range(248, 256))  # the 8 bytes just before offset 512
    info = watch.tick()
    assert info["bytes"] == 0 and info["frac"] == 0.5
    count[0] += 5000
    assert watch.tick()["frac"] == 1.0
    watch.at(1 << 30)  # past the end: done
    assert watch.tick() is None


def test_io_counter_reads_this_process():
    import os
    import sys
    from uniview.ui.loading_child import io_counter
    counter = io_counter(os.getpid())
    if sys.platform == "win32":
        assert counter is not None and counter() >= 0
