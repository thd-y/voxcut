"""`make ui-envcheck`: S9 environment checks outside `make check`, run while `make ui` is up."""

import json
import re
import socket
import subprocess
import sys
import uuid
from pathlib import Path

from voxcut.jobs import JobError, ensure_slice, unit_argv
from voxcut.ui.launch import ROOT, edge_argv, edge_path

PROBE_BLOCKED = 3  # ENETUNREACH: an empty network namespace has no route at all
PROBE_OTHER = 4  # any other network error (timeout, refused): proves nothing
PROBE = ("import errno, socket, sys\n"
         "try:\n    socket.create_connection(('1.1.1.1', 53), 3)\n"
         "except OSError as e:\n"
         f"    sys.exit({PROBE_BLOCKED} if e.errno == errno.ENETUNREACH else {PROBE_OTHER})\n")


def listeners(ss_text: str, pid: int) -> list[str]:
    """Local addresses that `ss -ltnpH` shows for this pid."""
    return [line.split()[3] for line in ss_text.splitlines()
            if re.search(rf"pid={pid},", line) and len(line.split()) > 3]


def probe_verdict(rc: int | None, control: int | None) -> tuple[bool, str]:
    """D63: isolated only if the unit sees ENETUNREACH while the same unit without
    PrivateNetwork reaches the network; an offline host or a unit that never ran proves nothing."""
    what = "задача-пробник в юните до 1.1.1.1:53"
    if rc == PROBE_BLOCKED:
        if control == 0:
            return True, f"{what}: сеть отрезана (без PrivateNetwork доходит)"
        return False, (f"{what}: не проверено — без PrivateNetwork сети тоже нет "
                       f"(код {control})")
    if rc == PROBE_OTHER:
        return False, f"{what}: другая ошибка сети, не ENETUNREACH — изоляция не проверена"
    if rc == 0:
        return False, f"{what}: есть сеть (PrivateNetwork не работает)"
    if rc is None:
        return False, f"{what}: таймаут, юнит не ответил"
    return False, f"{what}: пробник не запустился (код {rc}), изоляция не проверена"


def run_probe(root: Path, isolated: bool) -> int | None:
    """PROBE in a job unit; `isolated=False` is the same unit without PrivateNetwork=yes."""
    argv = unit_argv(f"voxcut-envcheck-{uuid.uuid4().hex[:8]}", [sys.executable, "-c", PROBE],
                     root)
    if not isolated:
        i = argv.index("PrivateNetwork=yes")
        del argv[i - 1:i + 1]
    try:
        return subprocess.run([argv[0], "--wait", "--pipe", *argv[1:]], capture_output=True,
                              text=True, timeout=60, check=False).returncode
    except subprocess.TimeoutExpired:
        return None


def run_checks(root: Path) -> list[tuple[bool, str]]:
    out = []
    meta = json.loads((root / ".run" / "ui.json").read_text())
    pid, port = meta["pid"], meta["port"]

    ss = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True, check=False).stdout
    got = listeners(ss, pid)
    out.append((got == [f"127.0.0.1:{port}"], f"слушатели процесса {pid}: {got}"))

    addrs = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                           check=False).stdout.split()
    for addr in addrs:
        try:
            with socket.create_connection((addr, port), 2):
                out.append((False, f"{addr}:{port} принимает соединение"))
        except OSError as exc:
            out.append((True, f"{addr}:{port} недоступен ({type(exc).__name__})"))
    if not addrs:
        out.append((False, "hostname -I не вернул адресов: проверка LAN не выполнена"))

    argv = edge_argv(edge_path(), port, "<скрыт>")
    ok = "--inprivate" in argv and argv[-1].startswith("--app=http://127.0.0.1:")
    out.append((ok, "строка запуска Edge: " + " ".join(argv)))

    try:
        ensure_slice(subprocess.run)
        out.append((True, "voxcut.slice: MemoryMax=5368709120, MemorySwapMax=0"))
    except JobError as exc:
        out.append((False, str(exc)))

    out.append(probe_verdict(run_probe(root, True), run_probe(root, False)))
    return out


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    if not (root / ".run" / "ui.json").is_file():
        print("FAIL нет .run/ui.json: сначала запустите make ui")
        return 1
    results = run_checks(root)
    for ok, text in results:
        print(f"{'OK  ' if ok else 'FAIL'} {text}")
    print("ВРУЧНУЮ: в окне Edge откройте edge://extensions и убедитесь, что ни у одного "
          "расширения не включено «Разрешить в режиме InPrivate»")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
