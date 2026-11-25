from __future__ import annotations

import container_entrypoint


def test_resolve_command_uses_default_when_empty():
    assert container_entrypoint._resolve_command([]) == ["python", "main.py"]


def test_main_does_not_prepare_runtime_directories(monkeypatch):
    executed = []
    monkeypatch.setattr(container_entrypoint.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(container_entrypoint.os, "umask", lambda _mask: None)
    monkeypatch.setattr(
        container_entrypoint.os,
        "execvp",
        lambda executable, arguments: executed.append((executable, arguments)),
    )

    assert container_entrypoint.main(["python", "main.py"]) == 0
    assert executed == [("python", ["python", "main.py"])]


