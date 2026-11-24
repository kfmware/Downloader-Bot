from __future__ import annotations

import os
import sys

DEFAULT_COMMAND = ["python", "main.py"]


def _resolve_command(argv: list[str]) -> list[str]:
    return argv or list(DEFAULT_COMMAND)


def _drop_privileges(*, user_name: str, group_name: str) -> None:
    import grp
    import pwd

    user = pwd.getpwnam(user_name)
    group = grp.getgrnam(group_name)

    os.setgroups([group.gr_gid])
    os.setgid(group.gr_gid)
    os.setuid(user.pw_uid)

    os.environ["HOME"] = user.pw_dir
    os.environ["USER"] = user.pw_name


def main(argv: list[str] | None = None) -> int:
    command = _resolve_command(sys.argv[1:] if argv is None else argv)

    if os.geteuid() == 0:
        import grp
        import pwd

        user = pwd.getpwnam("appuser")
        group = grp.getgrnam("appgroup")
        _drop_privileges(user_name=user.pw_name, group_name=group.gr_name)

    # Встановлюємо umask 0, щоб всі нові файли створювалися з правами 666/777
    os.umask(0o000)

    os.execvp(command[0], command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
