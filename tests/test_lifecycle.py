"""Lifecycle command tests."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from lm_visual_mcp.config import AppConfig


def test_service_targets():
    from lm_visual_mcp.services.lifecycle import service_targets
    from lm_visual_mcp.services.control import default_pidfile, proxy_pidfile

    cfg = AppConfig()
    cfg.runtime.port = 6506
    cfg.proxy.port = 8787
    name, port, pidfile = service_targets(cfg, "daemon")
    assert (name, port) == ("daemon", 6506)
    assert pidfile == default_pidfile()
    name, port, pidfile = service_targets(cfg, "proxy")
    assert (name, port) == ("proxy", 8787)
    assert pidfile == proxy_pidfile()


def test_pidfile_roundtrip(tmp_path):
    from lm_visual_mcp.services.control import read_pidfile, write_pidfile

    pf = tmp_path / "svc.pid"
    write_pidfile(pf)
    assert read_pidfile(pf) == os.getpid()
    assert read_pidfile(tmp_path / "missing.pid") is None


def test_pid_on_port(unused_tcp_port):
    import socket

    from lm_visual_mcp.services.lifecycle import _pid_on_port

    s = socket.socket()
    s.bind(("127.0.0.1", unused_tcp_port))
    s.listen(1)
    try:
        assert _pid_on_port(unused_tcp_port) == os.getpid()
    finally:
        s.close()
    assert _pid_on_port(unused_tcp_port + 1) is None


def test_is_our_process_negative():
    from lm_visual_mcp.services.lifecycle import _is_our_process

    assert _is_our_process(999999999, "daemon") is False  # nonexistent pid


def test_start_service_already_running(monkeypatch):
    from lm_visual_mcp.services.lifecycle import start_service

    cfg = AppConfig()
    monkeypatch.setattr("lm_visual_mcp.services.lifecycle.probe_proxy", lambda *a, **k: True)
    monkeypatch.setattr("lm_visual_mcp.services.lifecycle.probe_primary", lambda *a, **k: True)
    assert start_service(cfg, "proxy", None)["status"] == "already-running"
    assert start_service(cfg, "daemon", None)["status"] == "already-running"


def test_stop_service_kills_pidfile_process(monkeypatch, tmp_path):
    from lm_visual_mcp.services.lifecycle import _is_our_process, stop_service

    # Spawn a real long-running child; stop_service must SIGTERM it.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pidfile = tmp_path / "svc.pid"
    pidfile.write_text(str(proc.pid))
    try:
        monkeypatch.setattr("lm_visual_mcp.services.lifecycle._is_our_process", lambda pid, n: True)
        res = stop_service("daemon", 60001, pidfile)
        assert res["status"] == "stopped"
        proc.wait(timeout=5)
        assert proc.poll() is not None  # child terminated
        assert not pidfile.exists()  # pidfile cleaned up
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_service_not_running(tmp_path):
    from lm_visual_mcp.services.lifecycle import stop_service

    res = stop_service("daemon", 60002, tmp_path / "missing.pid")
    assert res["status"] == "not-running"