from scripts import auto_commit_wiki


class _Handler:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_auto_commit_cli_max_cycles_stops_handler(monkeypatch):
    handler = _Handler()
    monkeypatch.setattr(auto_commit_wiki, "start_auto_commit", lambda watch_dir: handler)

    exit_code = auto_commit_wiki.main(["/tmp/wiki", "--max-cycles", "1", "--interval", "0"])

    assert exit_code == 0
    assert handler.stopped is True
