"""The log writer thread (``core/log_queue.py``): the producer never blocks
or writes, overflow is dropped and counted in-band, rotation and the
access-log file filter still apply, and the drain paths are bounded.

Every test configures a PRIVATE logger — never the root, which carries the
pytest capture handlers (and is exactly why ``configure`` is a no-op there).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid

import pytest

from core import log_queue


@pytest.fixture
def private_logger(monkeypatch):
    """A fresh logger per test; the writer configured on it is drained at
    teardown (before monkeypatch restores the module's active slot)."""
    monkeypatch.setattr(log_queue, "_active", None)
    name = f"test-log-queue-{uuid.uuid4().hex[:8]}"
    lg = logging.getLogger(name)
    lg.propagate = False
    yield lg
    log_queue.drain(5.0)
    for h in list(lg.handlers):
        lg.removeHandler(h)


def _configure(tmp_path, lg, *, queue_size, max_bytes=1_000_000, backup_count=2):
    """Called from the test BODY, never a fixture: the stream handler binds
    ``sys.stderr`` at construction, and pytest closes the setup phase's
    capture object before the call phase gets a new one."""
    lq = log_queue.configure(
        log_path=tmp_path / "proxy.log", max_bytes=max_bytes,
        backup_count=backup_count, queue_size=queue_size, logger=lg,
    )
    assert lq is not None
    return lq


def _writer(tmp_path, lg):
    """An 8-slot writer: (writer, logger, log file path)."""
    return _configure(tmp_path, lg, queue_size=8), lg, tmp_path / "proxy.log"


def _file_lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _wedge(lq) -> tuple[threading.Event, logging.Handler]:
    """Put a handler that blocks until the event is set in front of the
    writer's real handlers."""
    gate = threading.Event()
    blocker = logging.Handler()
    blocker.emit = lambda record: gate.wait()  # type: ignore[method-assign]
    lq._listener.handlers = (blocker,) + tuple(lq._listener.handlers)
    return gate, blocker


class TestConfigure:
    def test_installs_one_queue_handler_and_returns_the_writer(self, tmp_path, private_logger):
        lq, lg, _ = _writer(tmp_path, private_logger)
        assert [type(h).__name__ for h in lg.handlers] == ["_DroppingQueueHandler"]
        assert log_queue._active is lq
        assert lq.stats() == {"dropped": 0, "queued": 0, "capacity": 8}

    def test_noop_when_logger_already_has_handlers(self, tmp_path, private_logger):
        # basicConfig's contract — the pytest capture handlers on the root
        # keep the suite from starting a writer thread.
        private_logger.addHandler(logging.NullHandler())
        assert log_queue.configure(
            log_path=tmp_path / "x.log", max_bytes=1000, backup_count=1,
            logger=private_logger,
        ) is None
        assert log_queue._active is None
        assert log_queue.stats() == {"dropped": 0, "queued": 0, "capacity": 0}
        assert log_queue.drain(0.1) is True

    def test_records_reach_stderr_and_file_in_order(self, tmp_path, private_logger, capsys):
        lq, lg, path = _writer(tmp_path, private_logger)
        for i in range(5):
            lg.info("line %d", i)
        assert lq.drain(5.0)
        lines = _file_lines(path)
        assert [ln.rsplit(": ", 1)[1] for ln in lines] == [f"line {i}" for i in range(5)]
        assert all(f" INFO {lg.name}: line " in ln for ln in lines)
        err = capsys.readouterr().err
        assert err.count(f"INFO {lg.name}: line ") == 5

    def test_exception_text_travels_with_the_record(self, tmp_path, private_logger):
        lq, lg, path = _writer(tmp_path, private_logger)
        try:
            raise ValueError("boom")
        except ValueError:
            lg.exception("failed")
        assert lq.drain(5.0)
        text = path.read_text()
        assert "ERROR" in text and "failed" in text
        assert "ValueError: boom" in text


class TestOverflow:
    def test_full_queue_drops_without_blocking_or_writing(self, tmp_path, private_logger,
                                                          capsys, monkeypatch):
        lq, lg, path = _writer(tmp_path, private_logger)
        monkeypatch.setattr(logging, "raiseExceptions", True)
        handler = lg.handlers[0]
        gate, _ = _wedge(lq)
        lg.info("first")            # taken by the thread → blocks in emit
        time.sleep(0.05)
        for i in range(8):          # fills the 8-slot queue
            lg.info("fill %d", i)
        t0 = time.monotonic()
        for i in range(20):
            lg.info("overflow %d", i)
        assert time.monotonic() - t0 < 0.5      # never blocked
        assert handler.dropped_total == 20
        assert lq.stats()["dropped"] == 20 and lq.stats()["queued"] == 8
        assert capsys.readouterr().err == ""    # no handleError traceback
        gate.set()
        assert lq.drain(5.0)

    def test_overflow_line_sits_exactly_at_the_gap(self, tmp_path, private_logger):
        lq, lg, path = _writer(tmp_path, private_logger)
        gate, blocker = _wedge(lq)
        lg.info("first")
        time.sleep(0.05)
        for i in range(8):
            lg.info("fill %d", i)
        for i in range(3):
            lg.info("lost %d", i)
        gate.set()
        blocker.emit = lambda record: None  # type: ignore[method-assign]
        time.sleep(0.1)
        lg.info("after")
        assert lq.drain(5.0)
        lines = _file_lines(path)
        assert lines[0].endswith("first")
        # Exactly one WARNING, carrying the count, between the last record
        # that got a slot and the first one after the gap.
        warnings = [ln for ln in lines if "log queue overflow" in ln]
        assert len(warnings) == 1
        assert "dropped 3 records" in warnings[0]
        assert "WARNING claude-proxy.log-queue" in warnings[0]
        idx = lines.index(warnings[0])
        assert lines[idx - 1].endswith("fill 7")
        assert lines[idx + 1].endswith("after")
        assert not any("lost" in ln for ln in lines)
        assert lq.stats()["dropped"] == 3

    def test_drops_with_no_later_record_are_reported_at_stop(self, tmp_path, private_logger):
        lq, lg, path = _writer(tmp_path, private_logger)
        gate, _ = _wedge(lq)
        lg.info("first")
        time.sleep(0.05)
        for i in range(8):
            lg.info("fill %d", i)
        for i in range(5):
            lg.info("lost %d", i)
        gate.set()
        lq.stop()
        lines = _file_lines(path)
        assert lines[-1].endswith("dropped 5 records while the log writer was blocked")
        assert lines[-2].endswith("fill 7")


class TestFileHandler:
    def test_rotation_still_applies(self, tmp_path, private_logger):
        lq = _configure(tmp_path, private_logger, queue_size=1000, max_bytes=400)
        for i in range(40):
            private_logger.info("rotation filler line %03d", i)
        assert lq.drain(5.0)
        assert (tmp_path / "proxy.log.1").exists()
        assert (tmp_path / "proxy.log").stat().st_size <= 400 + 120

    def test_access_log_goes_to_stderr_not_the_file(self, tmp_path, private_logger, capsys):
        lq, lg, path = _writer(tmp_path, private_logger)
        # The filter keys on the exact logger name uvicorn uses.
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '127.0.0.1:1 - "GET /health HTTP/1.1" 200', None, None,
        )
        lg.handle(rec)
        lg.info("app line")
        assert lq.drain(5.0)
        text = path.read_text()
        assert "GET /health" not in text and "app line" in text
        err = capsys.readouterr().err
        assert "GET /health" in err and "app line" in err


class TestSecretsStayOutOfTheLog:
    """F57: a token carried in a URL never reaches stderr or the file."""

    def test_query_tokens_are_redacted_in_both_outputs(self, tmp_path, private_logger, capsys):
        lq, lg, path = _writer(tmp_path, private_logger)
        lg.info("HTTP Request: GET http://c/cool.html?WOPISrc=x&access_token=eyJ.a.b&ui=1")
        rec = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 0,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", "/wopi/files/f?access_token=SECRET1", "1.1", 200), None,
        )
        lg.handle(rec)
        assert lq.drain(5.0)
        text = path.read_text()
        err = capsys.readouterr().err
        for out in (text, err):
            assert "eyJ.a.b" not in out
            assert "access_token=[redacted]&ui=1" in out
        assert "SECRET1" not in err and "/wopi/files/f?access_token=[redacted]" in err

    def test_encoded_tokens_in_a_socket_path_are_redacted(self, tmp_path, private_logger):
        lq, lg, path = _writer(tmp_path, private_logger)
        lg.info("%s", '"WebSocket /collabora/cool/http%3A//p/wopi/files/f%3Faccess_token%3DJWT.x.y%26permission%3Dedit/ws" [accepted]')
        assert lq.drain(5.0)
        text = path.read_text()
        assert "JWT.x.y" not in text and "%3Faccess_token%3D[redacted]%26permission" in text

    def test_other_query_parameters_are_kept(self):
        assert log_queue.redact("GET /x?a=1&_t=99&path=/y") == "GET /x?a=1&_t=99&path=/y"
        assert log_queue.redact("GET /x?key=abc&t=zz&token=q") == (
            "GET /x?key=[redacted]&t=[redacted]&token=[redacted]")

    def test_share_paths_and_other_secret_names_are_redacted(self):
        line = ('"GET /s/AbCdEf0123456789xyz/ws HTTP/1.1" /x?api_key=K1&sig=S2'
                '&X-Amz-Signature=S3&code=C4&password=P5&secret=S6&page=2')
        out = log_queue.redact(line)
        for value in ("AbCdEf0123456789xyz", "K1", "S2", "S3", "C4", "P5", "S6"):
            assert value not in out
        assert "/s/[redacted]/ws" in out and "&page=2" in out
        assert log_queue.redact("GET /s/") == "GET /s/"

    def test_the_overflow_line_still_formats(self, tmp_path, private_logger):
        lq = _configure(tmp_path, private_logger, queue_size=1000)
        rec = lq._listener._overflow_record(3)
        lq._listener.handle(rec)
        assert lq.drain(5.0)
        assert "dropped 3 records" in (tmp_path / "proxy.log").read_text()

    def test_http_client_loggers_sit_at_warning(self, tmp_path, private_logger):
        _configure(tmp_path, private_logger, queue_size=8)
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

    def test_the_log_file_is_private(self, tmp_path, private_logger):
        import os
        import stat
        lq = _configure(tmp_path, private_logger, queue_size=1000, max_bytes=300)
        for i in range(20):
            private_logger.info("rotate me %d %s", i, "x" * 40)
        assert lq.drain(5.0)
        files = sorted(tmp_path.glob("proxy.log*"))
        assert len(files) >= 2
        for f in files:
            assert stat.S_IMODE(os.stat(f).st_mode) == 0o600, f


class TestExitSignal:
    def test_handler_drains_then_dies_by_the_signal(self, monkeypatch):
        """uvicorn restores this handler and re-raises the served signal after
        its graceful shutdown; the queue must be drained BEFORE the default
        disposition ends the process."""
        import signal
        calls: list = []
        monkeypatch.setattr(log_queue, "drain",
                            lambda timeout_s: calls.append(("drain", timeout_s)) or True)
        monkeypatch.setattr(signal, "signal",
                            lambda signum, handler: calls.append(("signal", signum, handler)))
        monkeypatch.setattr(signal, "raise_signal",
                            lambda signum: calls.append(("raise", signum)))
        log_queue.exit_signal_handler(signal.SIGTERM, None)
        assert calls == [
            ("drain", log_queue.ATEXIT_DRAIN_S),
            ("signal", signal.SIGTERM, signal.SIG_DFL),
            ("raise", signal.SIGTERM),
        ]


class TestDrain:
    def test_stop_flushes_everything_and_is_idempotent(self, tmp_path, private_logger):
        lq = _configure(tmp_path, private_logger, queue_size=1000)
        for i in range(200):
            private_logger.info("n=%03d", i)
        lq.stop()
        lq.stop()
        lines = _file_lines(tmp_path / "proxy.log")
        assert len(lines) == 200
        assert [ln[-5:] for ln in lines] == [f"n={i:03d}" for i in range(200)]
        assert lq.stats()["dropped"] == 0

    def test_stop_waits_for_a_busy_writer_instead_of_raising(self, tmp_path, private_logger):
        lq, lg, path = _writer(tmp_path, private_logger)
        gate, _ = _wedge(lq)
        lg.info("first")
        time.sleep(0.05)
        for i in range(8):
            lg.info("fill %d", i)
        assert lq._handler.queue.full()
        done = threading.Event()
        threading.Thread(target=lambda: (lq.stop(), done.set()), daemon=True).start()
        assert not done.wait(0.2)              # blocked on the sentinel slot
        gate.set()
        assert done.wait(5.0)
        assert _file_lines(path)[-1].endswith("fill 7")

    def test_drain_times_out_on_a_wedged_writer(self, tmp_path, private_logger):
        lq, lg, _ = _writer(tmp_path, private_logger)
        gate, _ = _wedge(lq)
        lg.info("wedge")
        time.sleep(0.05)
        t0 = time.monotonic()
        assert lq.drain(0.2) is False
        assert time.monotonic() - t0 < 1.0
        assert lq._listener._thread is not None and lq._listener._thread.is_alive()
        gate.set()
        assert lq.drain(5.0) is True

    def test_drain_reports_false_when_the_sentinel_cannot_be_placed(self, tmp_path, private_logger):
        lq, lg, _ = _writer(tmp_path, private_logger)
        gate, _ = _wedge(lq)
        lg.info("wedge")
        time.sleep(0.05)
        for i in range(8):
            lg.info("fill %d", i)
        assert lq._handler.queue.full()
        with pytest.raises(queue.Full):
            lq._handler.queue.put_nowait(object())
        assert lq.drain(0.2) is False
        gate.set()
        assert lq.drain(5.0) is True
