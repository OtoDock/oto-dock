"""The heartbeat's two clocks: a late tick is a sleep when the sleep clock ran
on while the monotonic one stood, a daemon that did not run when both moved,
undecided where the host has no second clock (``host/host_probe.sleep_clock``,
``gap_is_sleep`` and ``late_tick_line``)."""

import time

import pytest

from satellite import config
from satellite.host import host_probe


def test_a_gap_only_the_sleep_clock_saw_is_a_sleep():
    # Monotonic saw one interval; the boot clock saw the 10 minutes asleep.
    assert host_probe.gap_is_sleep(20.5, 620.0)


def test_a_gap_both_clocks_saw_is_a_daemon_that_did_not_run():
    # A starved daemon: 211 s on both clocks (nothing slept).
    assert not host_probe.gap_is_sleep(211.0, 211.3)
    # A VM pause moves both clocks together too.
    assert not host_probe.gap_is_sleep(90.0, 90.0)


def test_the_boundary_is_the_excess():
    assert not host_probe.gap_is_sleep(20.0, 20.0 + host_probe._SLEEP_EXCESS_S)
    assert host_probe.gap_is_sleep(20.0, 20.0 + host_probe._SLEEP_EXCESS_S + 0.1)


@pytest.mark.parametrize("row, clock_id", [
    (config.LINUX, "CLOCK_BOOTTIME"),
    (config.DARWIN, "CLOCK_MONOTONIC"),
])
def test_the_sleep_clock_is_the_platforms(monkeypatch, row, clock_id):
    monkeypatch.setattr(config, "HOST", config.ROWS[row])
    asked = []
    monkeypatch.setattr(time, clock_id, 7, raising=False)
    monkeypatch.setattr(time, "clock_gettime", lambda cid: asked.append(cid) or 1234.5)
    assert host_probe.sleep_clock() == 1234.5
    assert asked == [7]


def test_windows_has_no_sleep_clock(monkeypatch):
    # Its monotonic clock runs through a sleep, and the wall clock would read
    # a time step as one: the host cannot tell.
    monkeypatch.setattr(config, "HOST", config.ROWS[config.WINDOWS])
    monkeypatch.setattr(time, "clock_gettime", lambda cid: pytest.fail("no clock id on Windows"))
    assert host_probe.sleep_clock() is None


def test_a_linux_without_the_boot_clock_has_none(monkeypatch):
    monkeypatch.setattr(config, "HOST", config.ROWS[config.LINUX])
    monkeypatch.delattr(time, "CLOCK_BOOTTIME", raising=False)
    assert host_probe.sleep_clock() is None


def test_a_gap_without_a_sleep_clock_is_undecided():
    assert host_probe.gap_is_sleep(300.0, None) is None


def test_the_line_names_the_cause_only_when_the_clocks_can_tell():
    slept = host_probe.late_tick_line(20.5, 620.0, 20)
    assert slept.startswith("The system slept for about 620s") and "forcing reconnect" in slept
    starved = host_probe.late_tick_line(211.0, 211.3, 20)
    assert starved.startswith("The daemon did not run for 211.0s")
    undecided = host_probe.late_tick_line(300.0, None, 20)
    assert undecided.startswith("A 300s gap between heartbeats (a sleep or a daemon that did not run; "
                                "this host cannot tell")
    assert "forcing reconnect" in undecided
