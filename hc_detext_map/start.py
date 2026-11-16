#!/usr/bin/env python3
"""One-click local dev launcher for hc_detext_map. Works on Windows, macOS and Linux.

No Docker required — uses a pure-Python MQTT broker (amqtt) and a SQLite database, so the
three projects can run anywhere Python + Node are installed.

    python start.py --install       first time only: install Python + npm deps
    python start.py                 start broker + backend + fusion server + fake Pi + frontend
    python start.py --scenario occlusion --no-frontend
    python start.py --stop          stop everything started by a previous run

Then open http://localhost:5173  (dashboard + map editor).
Press Ctrl+C to stop all services.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server"
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
LOGS = ROOT / "logs"
PIDFILE = LOGS / "pids.json"
IS_WIN = os.name == "nt"
PY = sys.executable

DB_PATH = BACKEND / "dev.db"
COMMON_ENV = {
    "HCDMAP_STORE_ID": "store-001",
    "HCDMAP_MQTT_HOST": "127.0.0.1",
    "HCDMAP_MQTT_PORT": "1883",
    "HCDMAP_SERVICE_TOKEN": "dev-service-token",
    "HCDMAP_DB_URL": f"sqlite:///{DB_PATH.as_posix()}",
    "HCDMAP_BACKEND_URL": "http://127.0.0.1:8000",
    "HCDMAP_CORS_ORIGINS": "http://localhost:5173,http://127.0.0.1:5173",
    "PYTHONUNBUFFERED": "1",
}
PORTS = {"broker": 1883, "backend": 8000, "server": 8001, "frontend": 5173}

_procs: list[tuple[str, subprocess.Popen, object]] = []


# --------------------------------------------------------------------------- utils
def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def npm_argv(*args: str) -> list[str]:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        sys.exit("npm not found on PATH — install Node.js (https://nodejs.org) and retry.")
    return (["cmd", "/c", npm, *args] if IS_WIN else [npm, *args])


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_port(port: int, name: str, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            log(f"{name} up on :{port}")
            return True
        time.sleep(0.5)
    log(f"!! {name} did not open :{port} within {timeout:.0f}s — see logs/{name}.log")
    return False


def wait_http(url: str, name: str, timeout: float = 40) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            log(f"{name} responding at {url}")
            return True
        except urllib.error.HTTPError:
            log(f"{name} responding at {url}")
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout):
            time.sleep(0.6)
    log(f"!! {name} not responding at {url} within {timeout:.0f}s — see logs/{name}.log")
    return False


def _tail(path: Path, n: int) -> None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print(f"  --- last {n} lines of {path.name} ---")
    for line in lines[-n:]:
        print(f"  | {line}")


def spawn(name: str, argv: list[str], cwd: Path, extra_env: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ, **COMMON_ENV, **extra_env}
    logfile = open(LOGS / f"{name}.log", "w", encoding="utf-8")
    kw: dict = {"cwd": str(cwd), "env": env, "stdout": logfile, "stderr": subprocess.STDOUT}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, **kw)
    _procs.append((name, p, logfile))
    return p


def save_pids() -> None:
    PIDFILE.write_text(json.dumps({name: p.pid for name, p, _ in _procs}))


# --------------------------------------------------------------------------- stop
def stop_all() -> None:
    pids: dict[str, int] = {}
    if PIDFILE.exists():
        try:
            pids = json.loads(PIDFILE.read_text())
        except json.JSONDecodeError:
            pids = {}
    live = {name: p for name, p, _ in _procs}
    names = list(dict.fromkeys([*live, *pids]))
    for name in reversed(names):
        pid = live[name].pid if name in live else pids.get(name)
        if not pid:
            continue
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, check=False)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            log(f"stopped {name} (pid {pid})")
        except (ProcessLookupError, PermissionError, OSError):
            pass
    time.sleep(1.5)
    for name, p, f in _procs:
        if p.poll() is None:
            p.kill()
        f.close()
    if PIDFILE.exists():
        PIDFILE.unlink()


# --------------------------------------------------------------------------- install
def install() -> None:
    log("pip install -r requirements-dev.txt ...")
    subprocess.check_call([PY, "-m", "pip", "install", "-r", str(ROOT / "requirements-dev.txt")])
    log("npm install (frontend) ...")
    subprocess.check_call(npm_argv("install", "--no-audit", "--no-fund"), cwd=str(FRONTEND))
    log("done. now run:  python start.py")


def check_python_deps() -> None:
    missing = []
    for mod in ("fastapi", "uvicorn", "sqlalchemy", "pydantic_settings", "httpx", "aiomqtt", "amqtt", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        sys.exit(f"missing Python deps: {', '.join(missing)}\nrun once:  python start.py --install")
    if not (FRONTEND / "node_modules").exists():
        sys.exit("frontend/node_modules missing\nrun once:  python start.py --install")


# --------------------------------------------------------------------------- run
def run(scenario: str, with_frontend: bool, with_sim: bool) -> None:
    busy = [f":{port}" for port in PORTS.values() if port_open(port)]
    if busy:
        sys.exit(f"ports already in use: {' '.join(busy)}\n"
                 f"stop the other process, or:  python start.py --stop")

    LOGS.mkdir(exist_ok=True)
    print("Starting hc_detext_map local stack...\n")

    # 1. MQTT broker (amqtt)
    print("[1/5] MQTT broker")
    spawn("broker", [PY, str(SERVER / "scripts" / "dev_broker.py")], SERVER,
          {"PYTHONPATH": str(SERVER / "src")})
    wait_port(1883, "broker")

    # 2. backend: seed SQLite, then serve
    print("[2/5] backend API + SQLite")
    seed_env = {**os.environ, **COMMON_ENV, "PYTHONPATH": str(BACKEND / "src")}
    r = subprocess.run([PY, str(BACKEND / "scripts" / "seed_dev.py")],
                       cwd=str(BACKEND), env=seed_env, capture_output=True, text=True)
    log(r.stdout.strip() or r.stderr.strip() or "seeded")
    spawn("backend", [PY, "-m", "hcdmap_backend"], BACKEND, {"PYTHONPATH": str(BACKEND / "src")})
    wait_http("http://127.0.0.1:8000/docs", "backend")

    # 3. fusion server
    print("[3/5] fusion server")
    spawn("server", [PY, "-m", "hcdmap_server"], SERVER, {"PYTHONPATH": str(SERVER / "src")})
    wait_http("http://127.0.0.1:8001/healthz", "server")

    # 4. fake Pi
    if with_sim:
        print(f"[4/5] fake Pi (scenario: {scenario})")
        spawn("fakepi", [PY, "-m", "hcdmap_server.sim.fake_pi", "--scenario", scenario],
              SERVER, {"PYTHONPATH": str(SERVER / "src")})
        log("publishing MQTT events")
    else:
        print("[4/5] fake Pi skipped (--no-sim)")

    # 5. frontend (Vite cold start on Windows can take a while on first run)
    if with_frontend:
        print("[5/5] frontend (Vite)")
        spawn("frontend", npm_argv("run", "dev"), FRONTEND, {})
        # Vite may bind IPv6 localhost; check both and fall back to a raw port probe.
        if not (wait_http("http://127.0.0.1:5173/", "frontend", timeout=120)
                or wait_http("http://localhost:5173/", "frontend", timeout=5)
                or port_open(5173)):
            _tail(LOGS / "frontend.log", 25)
    else:
        print("[5/5] frontend skipped (--no-frontend)")

    save_pids()
    print("\n" + "=" * 52)
    print("  Dashboard / map editor : http://localhost:5173")
    print("  Backend API (Swagger)  : http://localhost:8000/docs")
    print("  Fusion live feed (WS)  : ws://localhost:8001/ws")
    print("  Logs                   : logs/*.log")
    print("=" * 52)
    print("\nCtrl+C to stop everything.\n")

    try:
        while True:
            time.sleep(1)
            for name, p, _ in _procs:
                if p.poll() is not None:
                    print(f"[{name}] exited (code {p.returncode}) — see logs/{name}.log")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        stop_all()


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="One-click local dev launcher for hc_detext_map")
    ap.add_argument("--install", action="store_true", help="install Python + npm deps, then exit")
    ap.add_argument("--stop", action="store_true", help="stop services from a previous run, then exit")
    ap.add_argument("--scenario", default="pickup", choices=["normal", "pickup", "occlusion"])
    ap.add_argument("--no-frontend", action="store_true")
    ap.add_argument("--no-sim", action="store_true", help="don't start the fake Pi")
    args = ap.parse_args()

    LOGS.mkdir(exist_ok=True)

    if args.install:
        install()
        return
    if args.stop:
        stop_all()
        print("stopped.")
        return

    check_python_deps()
    try:
        run(args.scenario, with_frontend=not args.no_frontend, with_sim=not args.no_sim)
    except KeyboardInterrupt:
        stop_all()


if __name__ == "__main__":
    main()
