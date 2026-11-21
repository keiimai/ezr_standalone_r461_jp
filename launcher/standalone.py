#!/usr/bin/env python3
# --- 第5層: Zip の中の QEMU で Linux の仮想マシンを動かし、その中の Docker で EZR を起動・停止する（README「仕組み」）---

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shlex
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent.parent
BUILD_TIMEOUT = 86400
COMPOSE = ("cd /mnt/project && COMPOSE_PROJECT_NAME=ezr-standalone docker compose "
           "-p ezr-standalone -f docker-compose.yml -f /run/ezr-override.yml")


# --- 画面に出す文の名前を持つ、起動の失敗（README「コードの説明」）---
class LaunchError(RuntimeError):
    # --- 文の名前と、詳しい表示を持たせる（README「コードの説明」）---
    def __init__(self, key: str, detail: str):
        super().__init__(detail)
        self.key = key


# --- 起動と画面の文の一覧を読む（README「コードの説明」）---
def messages(root: Path = ROOT) -> dict[str, str]:
    table = {}
    with contextlib.suppress(OSError, UnicodeError):
        for line in (root / "launcher" / "start_messages.txt").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"[a-z_]+", key):
                table.setdefault(key, value)
    return table


# --- 文の一覧の文を、画面の下の欄に出す（README「コードの説明」）---
def say(key: str, detail: str = "") -> None:
    text = messages().get(key) or f"[start_messages.txt: {key}]"
    with contextlib.suppress(BrokenPipeError):
        print("EZR: " + text.replace("{0}", detail), flush=True)


# --- 同じフォルダの二重の起動を、.state/launcher.lock で防ぐ（README「困ったとき」）---
@contextlib.contextmanager
def lock_runtime(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "launcher.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise LaunchError("gui_already", "This folder is already running.") from error
        yield


# --- PC の種類から、Linux の CPU の種類（arm64・amd64）を決める（README「仕組み」）---
def target_arch(target: str) -> str:
    return "arm64" if target.endswith("arm64") else "amd64"


# --- SHA-256 を照らすファイルか（この PC の分の runtime/ と standalone.py だけ。app/ は照らさない）（README「配る」）---
def runtime_files_for(name: str, target: str | None) -> bool:
    if name == "launcher/standalone.py":
        return True
    if not name.startswith("runtime/"):
        return False
    if target is None:
        return True
    return (name.startswith(("runtime/common/", f"runtime/{target}/", f"runtime/python-{target}/"))
            or name == f"runtime/linux/engine-{target_arch(target)}.qcow2")


# --- runtime/ のファイルを、manifest.json の SHA-256 と照らす（README「配る」）---
def verify_runtime(root: Path = ROOT, target: str | None = None):
    manifest_file = root / "runtime" / "manifest.json"
    if not manifest_file.is_file():
        return
    manifest = json.loads(manifest_file.read_text())
    for name, expected in manifest["files"].items():
        candidate = (root / name).resolve()
        if not candidate.is_relative_to(root.resolve()):
            raise ValueError("Runtime manifest path escapes the distribution")
        if not runtime_files_for(name, target):
            continue
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise LaunchError("runtime_broken", f"Runtime SHA-256 mismatch: {name}")
    return manifest


# --- TEMPLATE_VERSION.txt から、Zip の元のコミットを読む（README「起動の記録」）---
def source_commit(folder: Path) -> str | None:
    marker = folder / "TEMPLATE_VERSION.txt"
    if not marker.exists():
        return None
    for line in marker.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if len(words) == 3 and words[0] == "template":
            return words[2]
    return None


# --- この PC の種類（macos-arm64・windows-x64・windows-arm64）を決める（README「仕組み」）---
def host_target() -> str:
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows":
        machine = {"arm64": "arm64", "x64": "amd64"}.get(native_windows_machine(), machine)
    if system == "Darwin" and machine == "arm64":
        return "macos-arm64"
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    if system == "Windows" and machine == "arm64":
        return "windows-arm64"
    raise RuntimeError(f"Unsupported host: {system} {machine}")


# --- Windows の本当の CPU の種類を調べる（x64 のまねで動いていても）（README「仕組み」）---
def native_windows_machine() -> str | None:
    if os.name != "nt":
        return None
    try:
        kernel = ctypes.windll.kernel32
        process, native = ctypes.c_ushort(), ctypes.c_ushort()
        if kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
            return {0xAA64: "arm64", 0x8664: "x64"}.get(native.value)
    except (AttributeError, OSError):
        pass
    return None


# --- OS・CPU・利用者と、管理者かどうか（README「起動の記録」）---
def host_identity() -> dict:
    administrator = bool(ctypes.windll.shell32.IsUserAnAdmin()) if os.name == "nt" else os.geteuid() == 0
    return {"system": platform.system(), "machine": platform.machine(),
            "uid": os.geteuid() if hasattr(os, "geteuid") else None,
            "administrator": administrator}


# --- パソコンのメモリの大きさ（MiB）（README「仕組み」）---
def host_memory_mib() -> int | None:
    if os.name == "nt":
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                        ("available", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong),
                        ("available_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]
        status = Status()
        status.length = ctypes.sizeof(Status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total // (1024 * 1024)
        except (AttributeError, OSError):
            pass
        return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        return None


# --- 仮想マシンのメモリ（パソコンの 4 分の 1、2〜4 GiB）（README「仕組み」）---
def default_memory(total: int | None = None) -> int:
    total = host_memory_mib() if total is None else total
    return max(2048, min(4096, total // 4)) if total else 2048


# --- パソコンの中のプロキシの住所を、仮想マシンから見た住所に直す（README「困ったとき」）---
def guest_proxy(url: str) -> str:
    parts = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        auth = parts.netloc.rpartition("@")[0]
        netloc = (auth + "@" if auth else "") + "10.0.2.2" + (f":{parts.port}" if parts.port else "")
        parts = parts._replace(netloc=netloc)
    return urllib.parse.urlunsplit(parts)


# --- パソコンのプロキシの設定を読む（README「困ったとき」）---
def host_proxies() -> dict[str, str]:
    try:
        found = urllib.request.getproxies()
    except Exception:
        return {}
    proxies = {scheme: guest_proxy(found[scheme]) for scheme in ("http", "https") if found.get(scheme)}
    if proxies:
        extra = [item.strip() for item in str(found.get("no", "")).replace(";", ",").split(",")
                 if item.strip() and item.strip() != "<local>"]
        proxies["no"] = ",".join(dict.fromkeys(["localhost", "127.0.0.1", *extra]))
    return proxies


# --- 番号のプロセスの命令の行を読む（無ければ空）（README「仕組み」）---
def process_command(pid: int) -> str:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True)
    return (result.stdout or b"").decode("utf-8", errors="replace").strip() if result.returncode == 0 else ""


# --- 番号のプロセスを終わらせる（README「仕組み」）---
def end_process(pid: int):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return
    import signal
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(0.2)
            os.kill(pid, 0)
        os.kill(pid, signal.SIGKILL)


# --- 空いている番号を 1 つ選ぶ（README「仕組み」）---
def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


# --- QEMU の設定の値の中のカンマを、そのまま渡せる形にする（README「仕組み」）---
def qemu_value(value: Path | str) -> str:
    return str(value).replace(",", ",,")


# --- app/ を仮想マシンへ共有する、このパソコンの中だけの SMB（README「仕組み」）---
class FolderBridge:
    # --- 共有の名前を毎回作り、待ち受けを用意する（README「仕組み」）---
    def __init__(self, folder: Path, vendor: Path):
        sys.path.insert(0, str(vendor))
        from quicksand_smb import SMBConfig, ShareConfig, serve_socket

        self.share = "EZR_" + secrets.token_hex(24)
        configuration = SMBConfig(shares={self.share: ShareConfig(host_path=str(folder.resolve()))})

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                serve_socket(self.request, configuration)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- 共有を始める（README「仕組み」）---
    def start(self):
        self.thread.start()

    # --- 共有を終える（README「仕組み」）---
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


# --- RStudio の番号の前に置く中継（つながりの取りこぼしと、Windows の切断を防ぐ）（README「仕組み」）---
class PortProxy:
    # --- 中継を用意する（答えの前に切れたら、つなぎ直して要求を送り直す）（README「仕組み」）---
    def __init__(self, upstream_port: int, port: int = 0):
        upstream = ("127.0.0.1", upstream_port)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                client = self.request
                state = {"up": None, "answered": False, "sent": bytearray(), "replay": True, "closed": False}
                lock = threading.Lock()

                def connect():
                    for attempt in range(40):
                        try:
                            connection = socket.create_connection(upstream, timeout=10)
                            connection.settimeout(None)
                            return connection
                        except OSError:
                            time.sleep(min(0.05 * (attempt + 1), 0.5))
                    return None

                def from_client():
                    try:
                        while True:
                            data = client.recv(65536)
                            with lock:
                                if not data:
                                    state["closed"] = True
                                    if state["up"]:
                                        with contextlib.suppress(OSError):
                                            state["up"].shutdown(socket.SHUT_WR)
                                    return
                                if not state["answered"] and state["replay"]:
                                    state["sent"] += data
                                    if len(state["sent"]) > 4 * 1024 * 1024:
                                        state["replay"] = False
                                if state["up"]:
                                    state["up"].sendall(data)
                    except OSError:
                        pass

                with lock:
                    state["up"] = connect()
                if state["up"] is None:
                    return
                reader = threading.Thread(target=from_client, daemon=True)
                reader.start()
                tries = 0
                while True:
                    up = state["up"]
                    try:
                        data = up.recv(65536)
                    except OSError:
                        data = None
                    if data:
                        state["answered"] = True
                        try:
                            client.sendall(data)
                        except OSError:
                            break
                        continue
                    if state["answered"] or not state["replay"] or tries >= 5:
                        break
                    tries += 1
                    with lock:
                        with contextlib.suppress(OSError):
                            up.close()
                        state["up"] = connect()
                        if state["up"] is None:
                            break
                        with contextlib.suppress(OSError):
                            if state["sent"]:
                                state["up"].sendall(bytes(state["sent"]))
                            if state["closed"]:
                                state["up"].shutdown(socket.SHUT_WR)
                with contextlib.suppress(OSError):
                    client.shutdown(socket.SHUT_WR)
                reader.join(30)
                for connection in (state["up"], client):
                    with contextlib.suppress(Exception):
                        connection.close()

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            block_on_close = False
            allow_reuse_address = True
            request_queue_size = 256

        self.server = Server(("127.0.0.1", port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # --- 中継を始める（README「仕組み」）---
    def start(self):
        self.thread.start()

    # --- 中継を終える（README「仕組み」）---
    def close(self):
        self.server.shutdown()
        self.server.server_close()


# --- 仮想マシンの名前の問い合わせに、Mac の名前解決で答える（パソコンの設定は変えない）（README「仕組み」）---
class HostDNS:
    # --- 名前の問い合わせを受ける口を用意する（README「仕組み」）---
    def __init__(self, vendor: Path):
        sys.path.insert(0, str(vendor))
        from dnslib import A, RR
        from dnslib.server import DNSLogger, DNSServer

        class Resolver:
            def resolve(self, request, _handler):
                response = request.reply()
                if request.q.qtype == 1:
                    try:
                        addresses = socket.getaddrinfo(str(request.q.qname).rstrip("."), None, socket.AF_INET, socket.SOCK_STREAM)
                        for address in sorted({x[4][0] for x in addresses}):
                            response.add_answer(RR(request.q.qname, 1, rdata=A(address), ttl=30))
                    except socket.gaierror:
                        pass
                return response

        self.port = free_port()
        self.servers = [DNSServer(Resolver(), port=self.port, address="127.0.0.1", tcp=tcp,
                                 logger=DNSLogger(log="-request,-reply,-truncated,-error")) for tcp in (False, True)]

    # --- 名前の問い合わせを受け始める（README「仕組み」）---
    def start(self):
        for server in self.servers:
            server.start_thread()

    # --- 名前の問い合わせを受けるのをやめる（README「仕組み」）---
    def close(self):
        for server in self.servers:
            server.stop()


# --- Linux の仮想マシン（README「仕組み」）---
class LinuxVM:
    # --- 使うファイル・番号・メモリ・CPU のまね方を決める（Windows は TCG だけ）（README「仕組み」）---
    def __init__(self, folder: Path, state: Path, *, share="smb", accelerator=None,
                 memory=None, cpus=2, base=None, target=None, qemu_dir=None, vendor_root=None, engine_sha=None):
        self.target = target or host_target()
        self.folder, self.state = folder.resolve(), state.resolve()
        self.state.mkdir(parents=True, exist_ok=True)
        self.share_type = share
        self.accelerator = accelerator or ("tcg" if self.target.startswith("windows-") else "hvf")
        if self.target.startswith("windows-") and self.accelerator != "tcg":
            raise ValueError("The Windows edition requires TCG; WHPX/Hyper-V/WSL are never enabled")
        if share == "9p" and self.target.startswith("windows-"):
            raise ValueError("This Windows QEMU build has no 9p host filesystem backend")
        self.memory, self.cpus = memory or default_memory(), cpus
        self.arch = target_arch(self.target)
        self.suffix = "aarch64" if self.arch == "arm64" else "x86_64"
        vendor_root = Path(vendor_root).resolve() if vendor_root else ROOT / "runtime"
        self.vendor = vendor_root / self.target
        self.common = vendor_root / "common"
        self.qemu_dir = Path(qemu_dir).resolve() if qemu_dir else self.vendor / "quicksand_qemu" / "bin"
        self.images = self.vendor / "quicksand_alpine" / "images"
        extension = ".exe" if (self.target.startswith("windows-") and (not qemu_dir or os.name == "nt")) else ""
        self.qemu = self.qemu_dir / ("qemu-system-" + self.suffix + extension)
        self.qemu_img = self.qemu_dir / ("qemu-img" + extension)
        bundled_base = ROOT / "runtime" / "linux" / f"engine-{self.arch}.qcow2"
        self.base = Path(base).resolve() if base else (bundled_base if bundled_base.exists() else self.images / f"alpine-3.23-{self.arch}.qcow2")
        self.disk = self.state / "disk.qcow2"
        self.pid_file = self.state / "qemu.pid"
        self.engine_sha = engine_sha
        self.proxies = host_proxies()
        self.closing = False
        self.unreachable = 0
        self.agent_port, self.web_port, self.web_inner = free_port(), free_port(), free_port()
        while len({self.agent_port, self.web_port, self.web_inner}) < 3:
            self.web_port, self.web_inner = free_port(), free_port()
        self.web_proxy = None
        self.token = secrets.token_hex(32)
        self.bridge = None
        self.dns = None
        self.process = None
        self.log = None
        self.boot_seconds = None
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # --- 仮想マシンの中の道具へ要求を送る（README「仕組み」）---
    def request(self, endpoint: str, params=None, timeout=5) -> dict:
        data = json.dumps(params).encode() if params is not None else None
        request = urllib.request.Request(f"http://127.0.0.1:{self.agent_port}/{endpoint}",
            data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        with self.http.open(request, timeout=timeout) as response:
            return json.load(response)

    # --- 仮想マシンの中で命令を動かし、終わるのを待つ（README「仕組み」）---
    def execute(self, command: str, timeout=120, check=True) -> dict:
        result = self.request("execute", {"command": command, "timeout": timeout,
                              "shell": "/bin/sh", "exclusive": False}, timeout=timeout + 10)
        if check and result["exit_code"] != 0:
            raise RuntimeError(f"Guest command failed ({result['exit_code']}):\n{result['stderr']}\n{result['stdout']}")
        return result

    # --- 仮想マシンの中で Python を動かす（README「仕組み」）---
    def python(self, source: str, timeout=120) -> dict:
        return self.execute("python3 -c " + shlex.quote(source), timeout)

    # --- 仮想マシンの中の命令の出力を、画面の下の欄へ流す（パソコンの空きが 4 GiB を切ったら止める）（README「仕組み」）---
    def stream(self, command: str, timeout=1800) -> int:
        body = json.dumps({"command": command, "timeout": timeout, "shell": "/bin/sh", "exclusive": False}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{self.agent_port}/execute_stream", data=body,
                    headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        with self.http.open(request, timeout=timeout + 10) as response:
            for line in response:
                if not line.startswith(b"data: "):
                    continue
                event = json.loads(line[6:])
                if "error" in event:
                    raise RuntimeError(str(event["error"]))
                if event.get("stream") in ("stdout", "stderr"):
                    print(event.get("data", ""), end="", flush=True)
                if event.get("stream") == "exit":
                    return int(event.get("exit_code", -1))
                if __import__("shutil").disk_usage(self.state).free < 4 * 1024 ** 3:
                    raise RuntimeError("Less than 4 GiB free on the host; stopping this private VM")
        raise RuntimeError("Guest stream ended without an exit status")

    # --- ファイルの場所を、展開したフォルダからの相対の場所にする（README「仕組み」）---
    def local(self, path: Path) -> str:
        try:
            return os.path.relpath(path, ROOT)
        except ValueError:
            return str(path)

    # --- 前の回に残った、このフォルダの QEMU を止める（README「仕組み」）---
    def reap_orphan(self):
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").split()[0])
        except (OSError, ValueError, IndexError):
            return
        try:
            command = process_command(pid)
        except (OSError, ValueError, subprocess.SubprocessError):
            command = ""
        ours = ("qemu-system-" in command) if os.name == "nt" else (str(self.qemu) in command)
        if ours:
            say("vm_leftover")
            end_process(pid)
        self.pid_file.unlink(missing_ok=True)

    # --- 仮想マシンのディスクの土台が変わっていたら、ディスクを作り直す（README「起動の記録」）---
    def check_engine(self):
        if not self.engine_sha:
            return
        record = self.state / "engine.sha256"
        try:
            recorded = record.read_text(encoding="utf-8").strip()
        except OSError:
            recorded = None
        if self.disk.exists() and recorded and recorded != self.engine_sha:
            say("engine_changed")
            self.disk.unlink()
            (self.state / "image.json").unlink(missing_ok=True)
        if recorded != self.engine_sha:
            record.write_text(self.engine_sha + "\n", encoding="utf-8")

    # --- QEMU を起こす命令の行を作る（README「仕組み」）---
    def command(self) -> list[str]:
        arm = self.arch == "arm64"
        device = "device" if arm else "pci"
        console = "ttyAMA0" if arm else "ttyS0"
        network = f"user,id=net0,hostfwd=tcp:127.0.0.1:{self.agent_port}-:{self.agent_port},hostfwd=tcp:127.0.0.1:{self.web_inner}-:8787"
        command = [str(self.qemu), "-nodefaults", "-machine", "virt" if arm else "q35",
                   "-accel", self.accelerator, "-cpu", "host" if self.accelerator == "hvf" else ("max" if arm else "qemu64"),
                   "-m", str(self.memory), "-smp", str(self.cpus), "-L", self.local(self.qemu_dir / "share" / "qemu"),
                   "-display", "none", "-monitor", "none", "-serial", "stdio",
                   "-drive", f"file={qemu_value(self.local(self.disk))},format=qcow2,if=none,id=root,cache=writethrough,discard=unmap",
                   "-device", f"virtio-blk-{device},drive=root",
                   "-kernel", self.local(self.images / f"alpine-3.23-{self.arch}.kernel"),
                   "-initrd", self.local(self.images / f"alpine-3.23-{self.arch}.initrd"),
                   "-append", f"root=/dev/vda rw rootflags=rw rootfstype=ext4 console={console} quiet loglevel=3 raid=noautodetect quicksand_token={self.token} quicksand_port={self.agent_port}",
                   "-netdev", network, "-device", f"virtio-net-{device},netdev=net0" + (",romfile=" if not arm else "")]
        if self.share_type == "9p":
            command += ["-fsdev", f"local,id=workspace,path={qemu_value(self.folder)},security_model=none",
                        "-device", f"virtio-9p-{device},fsdev=workspace,mount_tag=workspace"]
        if not arm:
            command += ["-global", "apic.vapic=false",
                        "-object", "rng-builtin,id=entropy",
                        "-device", "virtio-rng-pci,rng=entropy"]
            if self.target == "windows-x64":
                command[command.index("-append") + 1] += " nohpet noapic tsc=unstable clocksource=acpi_pm"
        return command

    # --- QEMU を起こし、Linux が応えるのを待って、共有をつなぐ（README「仕組み」）---
    def start(self, timeout=None):
        if timeout is None and self.target != "windows-x64":
            timeout = 180
        if host_identity()["administrator"]:
            raise LaunchError("admin", "Run this launcher as an ordinary user, not an administrator/root")
        if not self.qemu.exists() or not self.base.exists():
            raise LaunchError("runtime_broken", "Runtime missing: use the complete distribution")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.reap_orphan()
        self.check_engine()
        if not self.disk.exists():
            backing = os.path.relpath(self.base, self.disk.parent)
            subprocess.run([str(self.qemu_img), "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", backing, self.local(self.disk)],
                           check=True, creationflags=flags, cwd=ROOT)
            subprocess.run([str(self.qemu_img), "resize", "-q", self.local(self.disk), "32G"], check=True, creationflags=flags, cwd=ROOT)
        self.log = (self.state / "console.log").open("ab")
        started = time.perf_counter()
        environment = os.environ.copy()
        if platform.system() == "Darwin":
            self.dns = HostDNS(self.common)
            self.dns.start()
            environment["QUICKSAND_DNS_PROXY"] = str(self.dns.port)
        self.process = subprocess.Popen(self.command(), env=environment, stdin=subprocess.DEVNULL, stdout=self.log,
                                        stderr=subprocess.STDOUT, creationflags=flags, cwd=ROOT)
        self.pid_file.write_text(f"{self.process.pid}\n", encoding="utf-8")
        try:
            self.web_proxy = PortProxy(self.web_inner, self.web_port)
        except OSError:
            self.web_proxy = PortProxy(self.web_inner)
        self.web_port = self.web_proxy.port
        self.web_proxy.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while deadline is None or time.monotonic() < deadline:
                if self.process.poll() is not None:
                    detail = ""
                    if self.target == "windows-x64" and platform.machine().lower() in ("arm64", "aarch64"):
                        detail = " This is an x64 executable running on ARM64 Windows; use the ARM64 bundle on this PC. Intel/AMD Windows testing requires an x64 Windows environment."
                    raise LaunchError("vm_failed", f"QEMU exited ({self.process.returncode}); inspect {self.state / 'console.log'}.{detail}")
                try:
                    probe_timeout = 30 if self.target == "windows-x64" else 1
                    if self.request("authenticate", {"token": self.token}, timeout=probe_timeout).get("authenticated"):
                        self.boot_seconds = time.perf_counter() - started
                        self.mount()
                        return self
                except (OSError, urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(0.2)
            raise LaunchError("vm_failed", f"Linux boot timed out; inspect {self.state / 'console.log'}")
        except BaseException:
            self.close()
            raise

    # --- 仮想マシンの中で app/ をつなぐ（コアダンプは書かない）（README「仕組み」）---
    def mount(self):
        self.execute("echo '|/bin/false' > /proc/sys/kernel/core_pattern")
        self.execute("mkdir -p /mnt/project")
        if self.share_type == "9p":
            self.execute("mount -t 9p -o trans=virtio,version=9p2000.L,cache=none,access=any workspace /mnt/project")
        else:
            self.bridge = FolderBridge(self.folder, self.common)
            self.bridge.start()
            options = f"port={self.bridge.port},username=guest,password=,sec=none,vers=3.0,nosharesock,nobrl,cache=none,actimeo=0,uid=1000,gid=1000,file_mode=0666,dir_mode=0777"
            self.execute("mount -t cifs " + shlex.quote(f"//10.0.2.2/{self.bridge.share}") + " /mnt/project -o " + shlex.quote(options))

    # --- 仮想マシンを止め、共有・中継・名前の問い合わせを終える（README「仕組み」）---
    def close(self):
        self.closing = True
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(Exception):
                self.execute("sync; poweroff", timeout=120 if self.target == "windows-x64" else 10, check=False)
            try:
                self.process.wait(timeout=120 if self.target == "windows-x64" else 15)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.process is not None and self.process.poll() is not None:
            self.pid_file.unlink(missing_ok=True)
        if self.bridge is not None:
            self.bridge.close()
            self.bridge = None
        if self.dns is not None:
            self.dns.close()
            self.dns = None
        if self.web_proxy is not None:
            self.web_proxy.close()
            self.web_proxy = None
        if self.log is not None:
            self.log.close()
            self.log = None

    # --- with で使うときに、仮想マシンを起こす（README「仕組み」）---
    def __enter__(self):
        return self.start()

    # --- with を抜けるときに、仮想マシンを止める（README「仕組み」）---
    def __exit__(self, *_):
        self.close()


# --- 開発用: 共有の読み書きと速さを確かめる（README「リポジトリから直接動かす」）---
def benchmark(vm: LinuxVM, cpu_iterations=3000000) -> dict:
    vm.execute("command -v python3 >/dev/null || apk add --no-cache python3", timeout=300)
    folder = vm.folder
    name = "日本語 空白"
    directory = folder / name
    directory.mkdir(exist_ok=True)
    initial = "host → guest\n".encode()
    (directory / "host.txt").write_bytes(initial)
    result = {"host": host_identity(), "target": vm.target, "accelerator": vm.accelerator,
              "share": vm.share_type, "boot_seconds": vm.boot_seconds, "checks": {}}
    guest = "/mnt/project/" + name
    script = f"""
import hashlib,json,os,pathlib,time
p=pathlib.Path({guest!r})
assert (p/'host.txt').read_bytes()=={initial!r}
payload=bytes(range(256))*4096
with (p/'guest.bin').open('wb') as f:
    f.write(payload); f.flush(); os.fsync(f.fileno())
(p/'rename.tmp').write_text('new',encoding='utf-8')
(p/'rename.txt').write_text('old',encoding='utf-8')
os.replace(p/'rename.tmp',p/'rename.txt')
started=time.perf_counter()
for i in range(100):
    (p/f'small_{{i}}.tmp').write_bytes(b'x'*4096)
small_seconds=time.perf_counter()-started
for i in range(100): (p/f'small_{{i}}.tmp').unlink()
started=time.perf_counter()
with (p/'throughput.tmp').open('wb') as f:
    for i in range(16): f.write(payload)
    f.flush(); os.fsync(f.fileno())
write_seconds=time.perf_counter()-started
started=time.perf_counter()
digest=hashlib.sha256((p/'throughput.tmp').read_bytes()).hexdigest()
read_seconds=time.perf_counter()-started
(p/'throughput.tmp').unlink()
started=time.perf_counter()
total=sum(i*i for i in range({cpu_iterations}))
cpu_seconds=time.perf_counter()-started
print(json.dumps(dict(guest_sha256=hashlib.sha256(payload).hexdigest(),small_files_seconds=small_seconds,write_16mib_seconds=write_seconds,read_16mib_seconds=read_seconds,cpu_seconds=cpu_seconds,cpu_result=total)))
"""
    measured = json.loads(vm.python(script, timeout=1800 if vm.accelerator == "tcg" else 120)["stdout"])
    measured["cpu_iterations"] = cpu_iterations
    assert hashlib.sha256((directory / "guest.bin").read_bytes()).hexdigest() == measured["guest_sha256"]
    assert (directory / "rename.txt").read_text() == "new"
    (directory / "host.txt").write_text("host changed", encoding="utf-8")
    vm.python(f"from pathlib import Path; p=Path({guest!r}); assert (p/'host.txt').read_text()=='host changed'; (p/'host.txt').write_text('guest changed',encoding='utf-8')")
    assert (directory / "host.txt").read_text() == "guest changed"
    result["checks"] = {key: True for key in ("unicode_and_spaces", "host_to_guest", "guest_to_host", "overwrite_rename", "sha256_matches", "host_change_visible", "guest_change_visible", "delete")}
    try:
        vm.python(f"import os; from pathlib import Path; os.setgid(1000); os.setuid(1000); p=Path({guest!r}); assert (p/'host.txt').read_text()=='guest changed'; (p/'uid1000.txt').write_text('container user',encoding='utf-8')")
        result["checks"]["container_uid1000_read_write"] = (directory / "uid1000.txt").read_text() == "container user"
    except RuntimeError as error:
        result["checks"]["container_uid1000_read_write"] = False
        result["permission_error"] = str(error)
    result["measurements"] = measured
    if vm.execute("command -v docker", check=False)["exit_code"] == 0:
        ensure_engine(vm)
        vm.execute("cd / && tar -h -cf /tmp/ezr-probe.tar bin/busybox lib/ld-musl-*.so.1")
        image_id = vm.execute("docker import /tmp/ezr-probe.tar ezr-share-probe:local")["stdout"].strip()
        vm.execute("docker run --rm --network none --user 1000:1000 -v /mnt/project:/work ezr-share-probe:local /bin/busybox sh -c " +
                   shlex.quote("/bin/busybox cat '/work/日本語 空白/host.txt'; /bin/busybox printf 'docker user' > '/work/日本語 空白/docker.txt'"), timeout=180)
        result["checks"]["docker_container_uid1000_direct_write"] = (directory / "docker.txt").read_text() == "docker user"
        result["docker_probe_image"] = image_id
        result["docker_version"] = ensure_engine(vm)
    result["guest"] = vm.execute("uname -a; cat /etc/alpine-release; df -h /")["stdout"]
    result["qemu_version"] = subprocess.check_output([str(vm.qemu), "--version"], text=True).splitlines()[0]
    result["recorded_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["windows_standard_user_verified"] = platform.system() == "Windows" and not result["host"]["administrator"]
    return result


# --- 仮想マシンの中の Docker Engine が応えるまで待つ（README「仕組み」）---
def ensure_engine(vm: LinuxVM, timeout=None):
    if timeout is None:
        timeout = 900 if vm.target == "windows-x64" else 300
    probe = """import http.client,json,socket
c=http.client.HTTPConnection('localhost',timeout=20)
c.sock=socket.socket(socket.AF_UNIX)
c.sock.settimeout(20)
c.sock.connect('/var/run/docker.sock')
c.request('GET','/_ping')
r=c.getresponse()
if r.status != 200: raise RuntimeError('Docker API status '+str(r.status))
if r.read().strip() != b'OK': raise RuntimeError('Unexpected Docker ping response')
server=r.getheader('Server','')
if not server.startswith('Docker/'): raise RuntimeError('Docker version header missing')
print(server.split('/',1)[1].split()[0])
c.close()
"""
    command = "python3 -c " + shlex.quote(probe)
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            result = vm.execute(command, timeout=30, check=False)
        except (TimeoutError, OSError, urllib.error.URLError) as error:
            last = str(error)
            time.sleep(0.5)
            continue
        if result["exit_code"] == 0:
            return result["stdout"].strip()
        last = result["stderr"]
        time.sleep(0.5)
    raise LaunchError("engine_failed", "The guest Docker Engine did not become ready: " + last)


# --- イメージの材料（.dockerignore で入るファイル）の SHA-256（README「仕組み」）---
def build_inputs(folder: Path) -> str:
    rules = []
    ignore = folder / ".dockerignore"
    if ignore.is_file():
        rules = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.strip().startswith("#")]
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if not path.is_file() or relative.startswith(("projects/", ".state/")) or path.name.startswith("._"):
            continue
        included = relative in ("Dockerfile", ".dockerignore")
        if not included:
            candidates = [PurePosixPath(relative), *list(PurePosixPath(relative).parents)[:-1]]
            for rule in rules:
                pattern = rule[1:] if rule.startswith("!") else rule
                if any(candidate.full_match(pattern) for candidate in candidates):
                    included = rule.startswith("!")
        if included:
            digest.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


# --- compose に、CPU の種類と RStudio の番号を渡す設定を書く（README「仕組み」）---
def write_override(vm: LinuxVM):
    if vm.arch == "amd64":
        dev = "'    network_mode: host' '    ports: !reset []'"
        lock = "'  lock:' '    platform: linux/amd64' '    network_mode: host'"
    else:
        dev = "'    ports: !override' '      - \"0.0.0.0:8787:8787\"'"
        lock = "'  lock:' '    platform: linux/arm64'"
    vm.execute("printf '%s\\n' 'services:' '  dev:' '    platform: linux/" + vm.arch + "' " + dev + " " + lock
               + " > /run/ezr-override.yml")


# --- プロキシの設定を、命令の引数にする（README「困ったとき」）---
def proxy_options(vm: LinuxVM, flag: str) -> str:
    options = ""
    proxies = getattr(vm, "proxies", {}) or {}
    for name, key in (("HTTP_PROXY", "http"), ("HTTPS_PROXY", "https"), ("NO_PROXY", "no")):
        if proxies.get(key):
            for variant in (name, name.lower()):
                options += f" {flag} " + shlex.quote(f"{variant}={proxies[key]}")
    return options


# --- 仮想マシンの中の Docker に、プロキシの設定を入れる（README「困ったとき」）---
def configure_proxy(vm: LinuxVM):
    proxies = getattr(vm, "proxies", {}) or {}
    wanted = {name: proxies[key] for name, key in (("http-proxy", "http"), ("https-proxy", "https"), ("no-proxy", "no"))
              if proxies.get(key)}
    script = ("import json,pathlib,sys; p=pathlib.Path('/etc/docker/daemon.json'); d=json.loads(p.read_text()); "
              "w=json.loads(sys.argv[1]); old=d.get('proxies') or {}; "
              "d.update(proxies=w) if w else d.pop('proxies', None); p.write_text(json.dumps(d)+'\\n'); "
              "print('changed' if old != w else 'same')")
    result = vm.execute("python3 -c " + shlex.quote(script) + " " + shlex.quote(json.dumps(wanted)))
    if wanted:
        say("proxy_used", proxies.get("https") or proxies.get("http"))
    if result["stdout"].strip() == "changed":
        vm.execute("rc-service docker restart", timeout=300)
        ensure_engine(vm)


# --- イメージを作る命令の行（README「仕組み」）---
def image_command(vm: LinuxVM, target: str, tag: str, commit: str) -> str:
    command = "cd /mnt/project && docker build --progress plain --platform linux/" + vm.arch
    if vm.arch == "amd64":
        command += " --network host"
    command += f" --target {target}" + proxy_options(vm, "--build-arg")
    if target != "bootstrap":
        command += " --build-arg " + shlex.quote("VCS_REF=" + commit)
        command += " --build-arg IMAGE_NAME=ezr-standalone --build-arg " + shlex.quote("BUILD_DATE=" + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return command + f" -t {tag} ."


# --- Dockerfile の、RStudio で使う段（dev か base）を選ぶ（README「仕組み」）---
def dev_stage(folder: Path) -> str:
    dockerfile = (folder / "Dockerfile").read_text(encoding="utf-8")
    stages = re.findall(r"^\s*FROM[^\r\n]*[ \t]AS[ \t]+([\w.-]+)", dockerfile,
                        flags=re.IGNORECASE | re.MULTILINE)
    stages = {stage.lower() for stage in stages}
    target = "dev" if "dev" in stages else "base"
    if target not in stages:
        raise RuntimeError("The EZR Dockerfile has neither the dev nor the base stage.")
    return target


# --- renv.lock が DESCRIPTION と合っているか（README「仕組み」）---
def lock_matches(vm: LinuxVM) -> bool:
    result = vm.execute(COMPOSE + " run --rm --user rstudio" + proxy_options(vm, "-e") + " dev Rscript --vanilla tools/make_lockfile.R --check",
                        timeout=1800 if vm.accelerator == "tcg" else 300, check=False)
    if result["exit_code"]:
        with contextlib.suppress(BrokenPipeError):
            print(result["stderr"] or result["stdout"], flush=True)
    return result["exit_code"] == 0


# --- renv.lock を DESCRIPTION に合わせて作り直す（README「仕組み」）---
def relock(vm: LinuxVM):
    say("relocking")
    if vm.stream(image_command(vm, "bootstrap", "ezr-standalone:bootstrap", ""), timeout=BUILD_TIMEOUT):
        raise LaunchError("build_failed", "The bootstrap image could not be built")
    if vm.stream(COMPOSE + " run --rm --user rstudio -v renv-cache:/opt/renv/cache" + proxy_options(vm, "-e")
                 + " lock Rscript --vanilla tools/make_lockfile.R", timeout=BUILD_TIMEOUT):
        raise LaunchError("relock_failed", "renv.lock could not be rebuilt from DESCRIPTION")


# --- EZR のイメージを作り、.state/image.json に残す（README「起動の記録」）---
def build_ezr(vm: LinuxVM) -> tuple[str, str]:
    ensure_engine(vm)
    write_override(vm)
    configure_proxy(vm)
    commit = source_commit(vm.folder) or "unknown"
    image = "ezr-standalone:dev"
    target = dev_stage(vm.folder)
    started = time.perf_counter()
    say("preparing")
    if vm.stream(image_command(vm, target, image, commit), timeout=BUILD_TIMEOUT) or not lock_matches(vm):
        relock(vm)
        code = vm.stream(image_command(vm, target, image, commit), timeout=BUILD_TIMEOUT)
        if code:
            raise LaunchError("build_failed", f"EZR image build failed ({code})")
    information = vm.execute(f"docker image inspect {image} --format '{{{{json .}}}}'")["stdout"]
    record = {"source_commit": commit, "inputs_sha256": build_inputs(vm.folder),
              "build_seconds": time.perf_counter() - started,
              "image": json.loads(information), "host": host_identity(), "accelerator": vm.accelerator}
    (vm.state / "image.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return image, commit


# --- この回に書かれ、まだ確定していない記録（README「プロジェクトと記録」）---
def unfinished_records(folder: Path, previous: dict[str, bytes]) -> list[str]:
    unfinished = []
    for path in (folder / "projects").glob("*/outputs/*/meta.json"):
        content = path.read_bytes()
        name = path.relative_to(folder).as_posix()
        if previous.get(name) == content:
            continue
        try:
            finalized = json.loads(content).get("records_finalized_at")
        except (ValueError, UnicodeError):
            finalized = None
        if not finalized:
            unfinished.append(name)
    return unfinished


# --- 画面へ、状態（READY・WAIT_RECORDS など）を伝える（README「仕組み」）---
def send_ui_event(event: str, value: str = ""):
    separator = "|" if os.name == "nt" else "\t"
    print(separator.join(("EZR_UI", event, value)), flush=True)


# --- 記録を確かめてから、コンテナを止めて消す（README「プロジェクトと記録」）---
def stop_ezr(vm: LinuxVM, previous: dict[str, bytes], *, force=False) -> str | None:
    try:
        running = vm.execute(COMPOSE + " ps -q --status running dev")["stdout"].strip()
        if not force and running:
            check_records = "source('R/project_ezr.R'); quit(status = ezr_records_status())"
            result = vm.execute(COMPOSE + " exec -T --user rstudio dev Rscript --vanilla -e "
                                + shlex.quote(check_records), check=False)
            if result["exit_code"] == 2:
                return "WAIT_RECORDS"
            if result["exit_code"] != 0:
                raise RuntimeError(result["stderr"] or result["stdout"] or "Could not check the EZR records.")
            pending = unfinished_records(vm.folder, previous)
            if pending:
                say("records_pending", ", ".join(pending))
                return "WAIT_RECORDS"
        vm.execute(COMPOSE + " down --timeout 30", timeout=90)
        if vm.execute(COMPOSE + " ps -aq")["stdout"].strip():
            raise RuntimeError("An EZR container remains after stopping.")
    except OSError as error:
        process = getattr(vm, "process", None)
        if process is not None and process.poll() is not None:
            say("vm_gone")
            return None
        with contextlib.suppress(BrokenPipeError):
            print(error, flush=True)
        vm.unreachable = getattr(vm, "unreachable", 0) + 1
        if process is not None and vm.unreachable >= 2:
            say("vm_stopped_forcibly")
            vm.close()
            return None
        return "STOP_FAILED"
    except RuntimeError as error:
        with contextlib.suppress(BrokenPipeError):
            print(error, flush=True)
        return "STOP_FAILED"
    vm.unreachable = 0
    return None


# --- 仮想マシンが止まったら、画面に伝えて裏の仕事を終える（README「仕組み」）---
def watch_vm(vm: LinuxVM, done: threading.Event):
    while not done.wait(2):
        if vm.process.poll() is not None and not vm.closing and not done.is_set():
            say("vm_stopped")
            with contextlib.suppress(Exception):
                sys.stdout.flush()
            os._exit(1)


# --- 仮想マシンを見張りながら、画面からの「停止」を待つ（README「仕組み」）---
def wait_for_gui_stop(vm: LinuxVM, previous: dict[str, bytes], url: str):
    done = threading.Event()
    if getattr(vm, "process", None) is not None and hasattr(vm, "closing"):
        threading.Thread(target=watch_vm, args=(vm, done), daemon=True).start()
    try:
        wait_for_stop_requests(vm, previous, url)
    finally:
        done.set()


# --- 画面からの「停止」を受け、画面が切れたら記録を待って止める（README「プロジェクトと記録」）---
def wait_for_stop_requests(vm: LinuxVM, previous: dict[str, bytes], url: str):
    with contextlib.suppress(BrokenPipeError):
        send_ui_event("READY", url)
        for line in sys.stdin:
            request = line.strip()
            if request not in ("stop", "force-stop"):
                continue
            event = stop_ezr(vm, previous, force=request == "force-stop")
            if event is None:
                return
            send_ui_event(event)

    with contextlib.suppress(BrokenPipeError):
        print("The launcher window disconnected. Waiting for a safe stop.", flush=True)
    while vm.process.poll() is None:
        if stop_ezr(vm, previous) is None:
            return
        time.sleep(5)
    raise RuntimeError("The Linux VM stopped after the launcher window disconnected.")


# --- イメージを用意し、RStudio を起こして、.state/session.json に残す（README「起動の記録」）---
def start_ezr(vm: LinuxVM, build=True, open_browser=True, interactive=True, *, gui=False):
    ensure_engine(vm)
    exists = vm.execute("docker image inspect ezr-standalone:dev >/dev/null 2>&1", check=False)["exit_code"] == 0
    commit = source_commit(vm.folder)
    image_record = vm.state / "image.json"
    try:
        recorded = json.loads(image_record.read_text())
        recorded_commit, recorded_inputs = recorded.get("source_commit"), recorded.get("inputs_sha256")
    except (OSError, ValueError, AttributeError):
        recorded_commit = recorded_inputs = None
    changed = recorded_inputs is not None and build_inputs(vm.folder) != recorded_inputs
    stale = exists and (not commit or commit != recorded_commit or changed)
    if stale and not (gui and build):
        raise RuntimeError("The existing EZR image cannot be matched to this application version; run the development build command before starting.")
    if build and (not exists or stale):
        try:
            build_ezr(vm)
        except (OSError, RuntimeError):
            if gui:
                send_ui_event("BUILD_FAILED")
            raise
    elif exists:
        say("reusing")
    elif not build:
        raise RuntimeError("EZR has not been built yet; run start without --no-build")
    previous = {path.relative_to(vm.folder).as_posix(): path.read_bytes()
                for path in (vm.folder / "projects").glob("*/outputs/*/meta.json")}
    write_override(vm)
    say("starting_rstudio")
    started = time.perf_counter()
    slow_start = vm.target == "windows-x64" or (not interactive and vm.accelerator == "tcg")
    vm.execute(COMPOSE + " up -d --force-recreate dev", timeout=1800 if slow_start else 180)
    url = f"http://localhost:{vm.web_port}/" if gui else f"http://127.0.0.1:{vm.web_port}/"
    deadline = time.monotonic() + (3600 if slow_start else 180)
    while time.monotonic() < deadline:
        try:
            with vm.http.open(f"http://127.0.0.1:{vm.web_port}/", timeout=20 if vm.target == "windows-x64" else 2) as response:
                if response.status == 200:
                    break
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    else:
        raise LaunchError("no_response", "RStudio did not become ready")
    print(f"RStudio: {url}", flush=True)
    record = {"url": url, "rstudio_seconds": time.perf_counter() - started, "memory_mib": getattr(vm, "memory", None),
              "boot_seconds": vm.boot_seconds, "host": host_identity(), "share": vm.share_type}
    (vm.state / "session.json").write_text(json.dumps(record, indent=2) + "\n")
    if gui:
        wait_for_gui_stop(vm, previous, url)
    else:
        if open_browser:
            webbrowser.open(url)
        if interactive:
            while True:
                input("Close EZR and wait for its report, then press Enter to stop.\n")
                result = stop_ezr(vm, previous)
                if result is None:
                    break
                print(result, flush=True)
    return record


# --- 開発用: 仮想マシンの中で試験と解析を通し、記録を照らす（README「リポジトリから直接動かす」）---
def verify_app(vm: LinuxVM, *, skip_existing_tests=False):
    started = time.perf_counter()
    session = start_ezr(vm, build=False, open_browser=False, interactive=False)
    startup_seconds = time.perf_counter() - started
    (vm.state / "validation.json").unlink(missing_ok=True)
    container = "ezr-standalone_dev"
    tests = "testthat::test_dir('tests/testthat', filter='project_ezr', reporter='summary', stop_on_failure=TRUE)"
    if not skip_existing_tests:
        result = vm.stream("docker exec --user rstudio -w /app " + container + " Rscript --vanilla -e " + shlex.quote(tests), timeout=900)
        if result:
            raise RuntimeError("The existing EZR tests failed in the private Docker Engine")
    project = vm.folder / "projects" / "standalone_validation"
    (project / "data").mkdir(parents=True, exist_ok=True)
    (project / "data" / "input.csv").write_text("age,outcome\n40,0\n50,1\n60,1\n", encoding="utf-8")
    code = r'''
source('/app/R/project_ezr.R')
options(browser=function(url) invisible(url))
wait_for <- function(f, seconds=180) {
  for (i in seq_len(seconds*2)) { if (isTRUE(f())) return(TRUE); Sys.sleep(0.5) }
  stop('Timed out waiting for EZR')
}
run <- ezr_open('standalone_validation', root='/app')
meta_file <- file.path(run,'meta.json')
wait_for(function() file.exists(meta_file))
commands <- c("Dataset <- read.csv('data/input.csv')", 'set.seed(8642)', 'hist(Dataset$age)')
ezr_send('/tmp/ezr-stdin', c('x11(width=7,height=7); par(lwd=1,las=1)',
  sprintf('Rcmdr::doItAndPrint(%s)',vapply(commands,deparse,'')),
  "writeLines('STANDALONE_ANALYSIS_DONE')"))
wait_for(function() any(grepl('^STANDALONE_ANALYSIS_DONE', readLines(file.path(run,'console.log'),warn=FALSE))))
wait_for(function() length(list.files(file.path(run,'plots'),pattern='[.]png$'))>0)
ezr_send('/tmp/ezr-stdin','Rcmdr::closeCommander(ask=FALSE)')
wait_for(function() {
  meta <- tryCatch(jsonlite::read_json(meta_file),error=function(e) NULL)
  !is.null(meta$records_finalized_at)
})
cat('STANDALONE_RUN:',run,'\n')
'''
    analysis_started = time.perf_counter()
    if vm.accelerator == "tcg":
        code = code.replace("seconds=180", "seconds=1800")
    result = vm.execute("docker exec --user rstudio -w /app " + container + " Rscript --vanilla -e " + shlex.quote(code), timeout=7200 if vm.accelerator == "tcg" else 600)
    analysis_seconds = time.perf_counter() - analysis_started
    print(result["stdout"], flush=True)
    marker = next(line for line in result["stdout"].splitlines() if line.startswith("STANDALONE_RUN:"))
    guest_run = marker.split(":", 1)[1].strip()
    if not guest_run.startswith("/app/"):
        raise RuntimeError("Unexpected validation run path")
    run = vm.folder / guest_run[len("/app/"):]
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    verified = []
    for item in meta["records"]:
        artifact = (run / item["path"]).resolve()
        if not artifact.is_relative_to(run.resolve()):
            raise RuntimeError("Record path escaped its run directory")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != item["sha256"]:
            raise RuntimeError(f"Host/guest hash mismatch: {item['path']}")
        verified.append(item["path"])
    if not meta.get("rng_at_close") or not (run / "markdown.html").is_file():
        raise RuntimeError("RNG snapshot or completed report is missing")
    if not any(name.startswith("plots/") for name in verified):
        raise RuntimeError("No host-side plot was captured")
    console = (run / "console.log").read_text(encoding="utf-8")
    if any(word in console for word in ("エラー", "Error", "警告", "Warning")):
        raise RuntimeError("The synthetic validation analysis reported an error or warning")
    if "data:image/png;base64" not in (run / "markdown.html").read_text(encoding="utf-8"):
        raise RuntimeError("The rendered report does not contain a plot")
    report = {"status": "passed", "session": session,
              "existing_project_ezr_tests": "skipped" if skip_existing_tests else "passed",
              "linux_boot_seconds": vm.boot_seconds, "post_boot_to_rstudio_seconds": startup_seconds,
              "analysis_report_records_seconds": analysis_seconds, "host_record_hashes": verified,
              "run_directory": str(run), "records_finalized_at": meta["records_finalized_at"],
              "rng_at_close_recorded": True, "host": host_identity()}
    (vm.state / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    vm.execute(COMPOSE + " down --timeout 30", timeout=90)


# --- 命令の行を読み、画面の裏の仕事では、失敗を文で伝える（README「コードの説明」）---
def main():
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["probe", "exec", "build", "start", "verify-app", "gui-worker"])
    parser.add_argument("--share", choices=["smb", "9p"], default="smb")
    parser.add_argument("--accel", choices=["hvf", "tcg"])
    parser.add_argument("--folder", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--command", dest="guest_command", default="uname -a")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--memory", type=int, help="Guest RAM in MiB (default: a quarter of the host memory, 2048 to 4096)")
    parser.add_argument("--target", choices=["macos-arm64", "windows-x64", "windows-arm64"])
    parser.add_argument("--qemu-dir", type=Path, help="Development only: cross-architecture QEMU binaries")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--skip-existing-tests", action="store_true", help="Development verification only: run the end-to-end analysis without repeating the existing R unit tests")
    parser.add_argument("--cpu-iterations", type=int, default=3000000, help="Development probe only: CPU loop size; timings are comparable only for the same value")
    args = parser.parse_args()
    if args.cpu_iterations < 1:
        parser.error("--cpu-iterations must be positive")
    try:
        run(args, parser)
    except Exception as error:
        if args.command != "gui-worker":
            raise
        key = getattr(error, "key", "launch_failed")
        if key != "build_failed":
            say(key)
        with contextlib.suppress(BrokenPipeError):
            print(f"{type(error).__name__}: {error}", flush=True)
        raise SystemExit(1) from None


# --- 命令ごとに、仮想マシンを起こして仕事をする（README「コードの説明」）---
def run(args, parser):
    args.target = args.target or host_target()
    manifest = verify_runtime(target=args.target)
    app_command = args.command in ("start", "build", "verify-app", "gui-worker")
    folder = args.folder or (ROOT / "app" if app_command else ROOT / ".state" / "probe-share")
    if app_command and not (folder / "Dockerfile").is_file():
        parser.error("Application missing: use a complete bundle")
    folder.mkdir(parents=True, exist_ok=True)
    state = args.state or ROOT / ".state"
    if args.command == "gui-worker":
        say("vm_starting")
    with lock_runtime(state), LinuxVM(folder, state, share=args.share, accelerator=args.accel, base=args.base,
                 memory=args.memory, target=args.target, qemu_dir=args.qemu_dir,
                 engine_sha=(manifest or {}).get("engines", {}).get(target_arch(args.target))) as vm:
        if args.command == "probe":
            report = benchmark(vm, args.cpu_iterations)
            report_file = state / "benchmark.json"
            report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            if not all(report["checks"].values()):
                raise SystemExit(2)
        elif args.command == "build":
            build_ezr(vm)
        elif args.command == "start":
            start_ezr(vm, build=not args.no_build, open_browser=not args.no_browser)
        elif args.command == "gui-worker":
            start_ezr(vm, build=not args.no_build, open_browser=False, interactive=False, gui=True)
        elif args.command == "verify-app":
            verify_app(vm, skip_existing_tests=args.skip_existing_tests)
        else:
            result = vm.execute(args.guest_command, timeout=args.timeout, check=False)
            print(result["stdout"], end="", flush=True)
            print(result["stderr"], end="", file=sys.stderr, flush=True)
            if result["exit_code"]:
                raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
