# --- standalone の起動の流れのテスト（仮想マシンは起こさない）（README「試験」）---

import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import standalone as launcher


# --- runtime/ の照らし合わせのテスト（README「配る」）---
class IntegrityTests(unittest.TestCase):
    # --- 壊れた runtime/ では起動せず、書き換えた app/ では起動する（README「配る」）---
    def test_corrupt_runtime_is_rejected_but_project_remains_editable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime").mkdir()
            binary = root / "runtime" / "binary"
            binary.write_bytes(b"locked runtime")
            app = root / "app"
            app.mkdir()
            (app / "analysis.R").write_text("changed by researcher")
            manifest = {"files": {"runtime/binary": hashlib.sha256(binary.read_bytes()).hexdigest(),
                                  "app/analysis.R": "original-template-hash"}}
            (root / "runtime" / "manifest.json").write_text(json.dumps(manifest))
            launcher.verify_runtime(root)
            binary.write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                launcher.verify_runtime(root)

    # --- manifest.json の場所は、フォルダの外を指せない（README「配る」）---
    def test_manifest_path_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runtime").mkdir()
            (root / "runtime" / "manifest.json").write_text(json.dumps({"files": {"runtime/../../outside": "0"}}))
            with self.assertRaises(ValueError):
                launcher.verify_runtime(root)


    # --- この PC の分のファイルだけを照らす（README「配る」）---
    def test_only_the_files_of_this_pc_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {}
            for name in ("runtime/common/a", "runtime/macos-arm64/q", "runtime/python-macos-arm64/p",
                         "runtime/linux/engine-arm64.qcow2", "runtime/windows-x64/q",
                         "runtime/python-windows-x64/p", "runtime/linux/engine-amd64.qcow2"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(name.encode())
                files[name] = hashlib.sha256(name.encode()).hexdigest()
            (root / "runtime" / "manifest.json").write_text(json.dumps({"files": files}))
            (root / "runtime" / "windows-x64" / "q").write_bytes(b"other pc")
            launcher.verify_runtime(root, "macos-arm64")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                launcher.verify_runtime(root, "windows-x64")
            self.assertEqual(launcher.target_arch("windows-x64"), "amd64")
            self.assertEqual(launcher.target_arch("windows-arm64"), "arm64")

# --- 停止・起動・イメージの使い回しのテスト（README「仕組み」）---
class IsolationTests(unittest.TestCase):
    # --- 「停止」は記録を待ち、失敗しても記録を消さずにやり直せる（README「プロジェクトと記録」）---
    def test_gui_stop_retries_without_discarding_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = []

            def execute(command, **kwargs):
                commands.append(command)
                if "ps -q --status running" in command:
                    return {"stdout": "container-id"}
                if "ezr_records_status" in command:
                    attempts = sum("ezr_records_status" in item for item in commands)
                    return {"exit_code": 2 if attempts == 1 else 0}
                if " down " in command:
                    attempts = sum(" down " in item for item in commands)
                    if attempts == 1:
                        raise RuntimeError("Temporary stop error")
                return {"stdout": ""}

            output = io.StringIO()
            vm = SimpleNamespace(folder=root, execute=execute)
            with mock.patch.object(launcher.sys, "stdin", io.StringIO("stop\nstop\nstop\n")), \
                 mock.patch.object(launcher.sys, "stdout", output):
                launcher.wait_for_gui_stop(vm, {}, "http://localhost:8787/")
            separator = "|" if launcher.os.name == "nt" else "\t"
            events = [line.split(separator)[1] for line in output.getvalue().splitlines() if line.startswith("EZR_UI")]
            self.assertEqual(events, ["READY", "WAIT_RECORDS", "STOP_FAILED"])
            self.assertEqual(sum("ezr_records_status" in item for item in commands), 3)
            self.assertTrue(commands[-1].endswith("ps -aq"))

    # --- 画面が切れても、記録が確定するまで待って止める（README「プロジェクトと記録」）---
    def test_gui_disconnect_waits_for_records(self):
        vm = SimpleNamespace(process=mock.Mock())
        vm.process.poll.return_value = None
        with mock.patch.object(launcher.sys, "stdin", io.StringIO()), \
             mock.patch.object(launcher.sys, "stdout", io.StringIO()), \
             mock.patch.object(launcher.time, "sleep"), \
             mock.patch.object(launcher, "stop_ezr", side_effect=["WAIT_RECORDS", None]) as stop:
            launcher.wait_for_gui_stop(vm, {}, "http://localhost:8787/")
        self.assertEqual(stop.call_args_list, [mock.call(vm, {}), mock.call(vm, {})])

    # --- Windows では、RStudio の遅い答えも待つ（README「仕組み」）---
    def test_windows_readiness_accepts_a_slow_http_response(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(3)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        actual_monotonic = time.monotonic
        started = actual_monotonic()

        def bounded_clock():
            now = actual_monotonic()
            return now + 7200 if now - started > 8 else now

        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "TEMPLATE_VERSION.txt").write_text("template ezr_dist_jp test-commit\n")
                (root / "image.json").write_text(json.dumps({"source_commit": "test-commit"}))
                vm = SimpleNamespace(target="windows-x64", arch="amd64", accelerator="tcg",
                    folder=root, state=root, share_type="smb", web_port=server.server_port, boot_seconds=0.1,
                    http=urllib.request.build_opener(urllib.request.ProxyHandler({})),
                    execute=lambda *args, **kwargs: {"exit_code": 0, "stdout": "", "stderr": ""})
                with mock.patch.object(launcher, "ensure_engine", return_value="29.5.2"), \
                     mock.patch.object(launcher, "host_identity", return_value={"administrator": False}), \
                     mock.patch.object(launcher.time, "monotonic", side_effect=bounded_clock):
                    record = launcher.start_ezr(vm, build=False, open_browser=False, interactive=False)
                self.assertGreaterEqual(record["rstudio_seconds"], 3)
                self.assertTrue((root / "session.json").is_file())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    # --- 元のコミットが違うイメージは使わない（README「仕組み」）---
    def test_stale_image_is_not_reused_for_another_source_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "TEMPLATE_VERSION.txt").write_text("template ezr_dist_jp new\n")
            (root / "image.json").write_text(json.dumps({"source_commit": "old"}))
            vm = SimpleNamespace(folder=root, state=root, execute=lambda *args, **kwargs: {"exit_code": 0})
            with mock.patch.object(launcher, "ensure_engine"):
                with self.assertRaisesRegex(RuntimeError, "cannot be matched"):
                    launcher.start_ezr(vm, open_browser=False, interactive=False)

    # --- Windows の QEMU は .exe のまま、TCG で起こす（README「仕組み」）---
    def test_windows_qemu_override_keeps_executable_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(launcher, "os", SimpleNamespace(name="nt")), mock.patch.object(launcher, "free_port", side_effect=[1234, 5678, 9012]):
                vm = launcher.LinuxVM(root, root / "state", target="windows-x64", qemu_dir=root / "qemu")
            self.assertEqual(vm.qemu.name, "qemu-system-x86_64.exe")
            self.assertEqual(vm.qemu_img.name, "qemu-img.exe")
            command = vm.command()
            self.assertEqual(command[command.index("-accel") + 1], "tcg")
            self.assertIn("apic.vapic=false", command)
            self.assertIn("virtio-net-pci,netdev=net0,romfile=", command)

    # --- TEMPLATE_VERSION.txt の template の行から、元のコミットを読む（README「起動の記録」）---
    def test_source_commit_reads_the_template_line_of_the_zip_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(launcher.source_commit(root))
            (root / "TEMPLATE_VERSION.txt").write_text(
                "# このフォルダの元になった版\ntemplate ezr_standalone_r453_jp abc1234\ncreated 2026-11-17\n", encoding="utf-8")
            self.assertEqual(launcher.source_commit(root), "abc1234")
            (root / "TEMPLATE_VERSION.txt").write_text("template ezr_dist_jp old\n", encoding="utf-8")
            self.assertEqual(launcher.source_commit(root), "old")

    # --- この回の確定していない記録だけが、停止を待たせる（README「プロジェクトと記録」）---
    def test_only_current_unfinished_records_delay_normal_shutdown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = root / "projects/study/outputs/old/meta.json"
            old.parent.mkdir(parents=True)
            old.write_text('{}')
            previous = {old.relative_to(root).as_posix(): old.read_bytes()}
            current = old.parent.parent / "current/meta.json"
            current.parent.mkdir()
            current.write_text('{"records_finalized_at": null}')
            self.assertEqual(launcher.unfinished_records(root, previous), [current.relative_to(root).as_posix()])
            current.write_text('{"records_finalized_at": "2026-10-31T09:00:00+0900"}')
            self.assertEqual(launcher.unfinished_records(root, previous), [])
            current.write_text('{partial')
            self.assertEqual(len(launcher.unfinished_records(root, previous)), 1)

    # --- Windows では、仮想化の機能（HVF）と 9p を選べない（README「仕組み」）---
    def test_windows_never_selects_hardware_virtualization(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for target in ("windows-x64", "windows-arm64"):
                with self.assertRaises(ValueError):
                    launcher.LinuxVM(root, root / "state", target=target, accelerator="hvf")
                with self.assertRaises(ValueError):
                    launcher.LinuxVM(root, root / "state", target=target, share="9p")


# --- 作り直しと文の一覧のテスト（README「仕組み」）---
class RebuildAndMessageTests(unittest.TestCase):
    # --- テスト用の解析のフォルダを作る（テストの道具）（README「試験」）---
    def make_app(self, root):
        (root / "Dockerfile").write_text("FROM scratch AS dev\n")
        (root / ".dockerignore").write_text("*\n!DESCRIPTION\n!renv.lock\n!R/*.[Rr]\n!docker/*.R\n")
        (root / "DESCRIPTION").write_text("Package: a\n")
        (root / "renv.lock").write_text("{}\n")
        (root / "R").mkdir()
        (root / "R" / "project_ezr.R").write_text("x <- 1\n")
        (root / "docker").mkdir()
        (root / "docker" / "project_env.txt").write_text("A=1\n")
        (root / "projects" / "study" / "data").mkdir(parents=True)

    # --- イメージの材料は、.dockerignore で入るファイルだけ（README「仕組み」）---
    def test_build_inputs_follow_the_dockerignore_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_app(root)
            first = launcher.build_inputs(root)
            (root / "docker" / "project_env.txt").write_text("A=2\n")
            (root / "README.md").write_text("changed\n")
            (root / "projects" / "study" / "data" / "input.csv").write_text("a\n1\n")
            (root / "R" / "sub").mkdir()
            (root / "R" / "sub" / "deep.R").write_text("y <- 2\n")
            self.assertEqual(launcher.build_inputs(root), first)
            for name in ("DESCRIPTION", "renv.lock", "R/project_ezr.R", "Dockerfile"):
                original = (root / name).read_text()
                (root / name).write_text(original + "# changed\n")
                self.assertNotEqual(launcher.build_inputs(root), first, name)
                (root / name).write_text(original)
            self.assertEqual(launcher.build_inputs(root), first)

    # --- Mac が exFAT に書く ._ のファイルは、材料に数えない（README「仕組み」）---
    def test_build_inputs_ignore_the_dot_underscore_files_macos_writes_on_exfat(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_app(root)
            first = launcher.build_inputs(root)
            (root / "R" / "._project_ezr.R").write_bytes(b"\x00\x05\x16\x07quarantine")
            self.assertEqual(launcher.build_inputs(root), first)
            (root / "R" / "._project_ezr.R").write_bytes(b"\x00\x05\x16\x07provenance")
            self.assertEqual(launcher.build_inputs(root), first)

    # --- 材料が変わったら、画面からは作り直し、命令の行からは止める（README「仕組み」）---
    def test_changed_inputs_rebuild_from_the_window_but_stop_the_command_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_app(root)
            (root / "TEMPLATE_VERSION.txt").write_text("template ezr_standalone_r453_jp same\n")
            (root / "image.json").write_text(json.dumps({"source_commit": "same", "inputs_sha256": launcher.build_inputs(root)}))
            (root / "DESCRIPTION").write_text("Package: a\nSuggests: b\n")
            vm = SimpleNamespace(folder=root, state=root, execute=lambda *args, **kwargs: {"exit_code": 0})
            with mock.patch.object(launcher, "ensure_engine"):
                with self.assertRaisesRegex(RuntimeError, "cannot be matched"):
                    launcher.start_ezr(vm, open_browser=False, interactive=False)
            with mock.patch.object(launcher, "ensure_engine"), \
                 mock.patch.object(launcher, "build_ezr", side_effect=launcher.LaunchError("build_failed", "stop here")) as build, \
                 mock.patch.object(launcher.sys, "stdout", io.StringIO()):
                with self.assertRaises(launcher.LaunchError):
                    launcher.start_ezr(vm, open_browser=False, interactive=False, gui=True)
            build.assert_called_once()

    # --- 合わない renv.lock は、イメージの前に作り直す（README「仕組み」）---
    def test_a_lock_that_does_not_match_is_rebuilt_before_the_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_app(root)
            streamed, executed = [], []

            def execute(command, **kwargs):
                executed.append(command)
                if "make_lockfile.R --check" in command:
                    return {"exit_code": 1, "stdout": "", "stderr": "renv.lock does not match"}
                if "docker image inspect" in command:
                    return {"exit_code": 0, "stdout": "{}", "stderr": ""}
                return {"exit_code": 0, "stdout": "", "stderr": ""}

            def stream(command, **kwargs):
                streamed.append(command)
                return 0

            vm = SimpleNamespace(folder=root, state=root, arch="arm64", accelerator="hvf", execute=execute, stream=stream)
            with mock.patch.object(launcher, "ensure_engine"), \
                 mock.patch.object(launcher, "host_identity", return_value={}), \
                 mock.patch.object(launcher.sys, "stdout", io.StringIO()):
                launcher.build_ezr(vm)
            self.assertEqual(len(streamed), 4)
            self.assertIn("--target dev", streamed[0])
            self.assertIn("--target bootstrap", streamed[1])
            self.assertIn(" run --rm --user rstudio -v renv-cache:/opt/renv/cache lock ", streamed[2])
            self.assertIn("--target dev", streamed[3])
            self.assertTrue(any("ezr-override.yml" in command and "lock:" in command for command in executed))
            record = json.loads((root / "image.json").read_text())
            self.assertEqual(record["inputs_sha256"], launcher.build_inputs(root))

    # --- 起動の道具が使う文は、すべて文の一覧にある（README「コードの説明」）---
    def test_every_message_the_launcher_uses_is_in_the_message_list(self):
        source = (Path(launcher.__file__)).read_text(encoding="utf-8")
        used = set(re.findall(r'say\("([a-z_]+)"', source)) | set(re.findall(r'LaunchError\("([a-z_]+)"', source))
        used.discard("build_failed")
        used.add("launch_failed")
        mac = (Path(launcher.__file__).parents[1] / "start_mac.command").read_text(encoding="utf-8")
        used |= set(re.findall(r"msg ([a-z_]+)", mac))
        keys = set(launcher.messages(Path(launcher.__file__).parents[1]))
        self.assertGreater(len(used), 15)
        self.assertEqual(used - keys, set())

    # --- 画面の裏の仕事は、失敗を Traceback ではなく文で伝える（README「困ったとき」）---
    def test_the_window_worker_reports_a_message_instead_of_a_traceback(self):
        output = io.StringIO()
        with mock.patch.object(launcher.sys, "argv", ["standalone.py", "gui-worker"]), \
             mock.patch.object(launcher, "run", side_effect=launcher.LaunchError("vm_failed", "QEMU exited (1)")), \
             mock.patch.object(launcher.sys, "stdout", output):
            with self.assertRaises(SystemExit) as stopped:
                launcher.main()
        self.assertEqual(stopped.exception.code, 1)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "EZR: " + launcher.messages()["vm_failed"])
        self.assertEqual(lines[1], "LaunchError: QEMU exited (1)")


# --- 失敗からの戻りと、パソコンの設定のテスト（README「仕組み」）---
class RecoveryAndHostTests(unittest.TestCase):
    # --- コアダンプを共有のフォルダに書かない（README「仕組み」）---
    def test_core_dumps_are_not_written_into_the_shared_folder(self):
        commands = []
        vm = launcher.LinuxVM.__new__(launcher.LinuxVM)
        vm.share_type = "9p"
        vm.execute = lambda command, **kwargs: commands.append(command)
        vm.mount()
        self.assertEqual(commands[0], "echo '|/bin/false' > /proc/sys/kernel/core_pattern")
        self.assertTrue(commands[-1].startswith("mount "))

    # --- 仮想マシンがもう止まっていたら、止まったとみなす（README「困ったとき」）---
    def test_stopping_when_the_vm_is_gone_counts_as_stopped(self):
        def execute(command, **kwargs):
            raise urllib.error.URLError("connection refused")
        process = mock.Mock()
        process.poll.return_value = 1
        vm = SimpleNamespace(folder=Path("."), execute=execute, process=process)
        with mock.patch.object(launcher.sys, "stdout", io.StringIO()):
            self.assertIsNone(launcher.stop_ezr(vm, {}))

    # --- 応えない仮想マシンは、2 回目の「停止」で止める（README「困ったとき」）---
    def test_an_unreachable_running_vm_is_closed_on_the_second_stop(self):
        def execute(command, **kwargs):
            raise TimeoutError("timed out")
        process = mock.Mock()
        process.poll.return_value = None
        vm = SimpleNamespace(folder=Path("."), execute=execute, process=process, close=mock.Mock())
        with mock.patch.object(launcher.sys, "stdout", io.StringIO()):
            self.assertEqual(launcher.stop_ezr(vm, {}), "STOP_FAILED")
            vm.close.assert_not_called()
            self.assertIsNone(launcher.stop_ezr(vm, {}))
        vm.close.assert_called_once()

    # --- 前の回に残ったこのフォルダの QEMU だけを、起動の前に止める（README「仕組み」）---
    def test_a_leftover_qemu_of_this_folder_is_ended_before_starting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm = launcher.LinuxVM(root, root / "state", target="macos-arm64", memory=2048)
            vm.pid_file.write_text("4242\n")
            with mock.patch.object(launcher, "process_command", return_value=f"{vm.qemu} -nodefaults"), \
                 mock.patch.object(launcher, "end_process") as end, \
                 mock.patch.object(launcher.sys, "stdout", io.StringIO()):
                vm.reap_orphan()
            end.assert_called_once_with(4242)
            self.assertFalse(vm.pid_file.exists())
            vm.pid_file.write_text("4243\n")
            with mock.patch.object(launcher, "process_command", return_value="/usr/bin/other"), \
                 mock.patch.object(launcher, "end_process") as end:
                vm.reap_orphan()
            end.assert_not_called()

    # --- 日本語の Windows の tasklist の答え（CP932）も、間違えずに読む（README「仕組み」）---
    def test_a_japanese_windows_answer_about_an_ended_qemu_is_read_without_error(self):
        answer = "情報: 指定された条件に一致するタスクは実行されていません。\r\n".encode("cp932")
        done = launcher.subprocess.CompletedProcess(["tasklist"], 0, stdout=answer, stderr=b"")
        with mock.patch.object(launcher.os, "name", "nt"), \
             mock.patch.object(launcher.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
             mock.patch.object(launcher.subprocess, "run", return_value=done) as run:
            command = launcher.process_command(4242)
        self.assertEqual(run.call_args.args[0][0], "tasklist")
        self.assertNotIn("qemu-system-", command)

    # --- 確かめに失敗しても、前の回の qemu.pid は消す（README「仕組み」）---
    def test_the_leftover_record_is_removed_even_when_the_check_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm = launcher.LinuxVM(root, root / "state", target="macos-arm64", memory=2048)
            vm.pid_file.write_text("4242\n")
            with mock.patch.object(launcher, "process_command", side_effect=OSError("tasklist missing")), \
                 mock.patch.object(launcher, "end_process") as end:
                vm.reap_orphan()
            end.assert_not_called()
            self.assertFalse(vm.pid_file.exists())

    # --- 応えるのが遅い Linux を起こす（テストの道具）（README「試験」）---
    def boot_with_slow_linux(self, target, answers_after):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm = launcher.LinuxVM(root, root / "state", target=target, memory=2048,
                                  qemu_dir=root / "qemu", base=root / "base.qcow2")
            for path in (vm.qemu, vm.base, vm.disk):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
            calls = []

            def request(name, payload, timeout):
                calls.append(name)
                if len(calls) < answers_after:
                    raise OSError("not yet")
                return {"authenticated": True}
            clock = iter(range(0, 10 ** 6, 60))
            process = mock.Mock(pid=4242)
            process.poll.return_value = None
            proxy = mock.Mock(port=50000)
            vm.command = lambda: ["qemu"]
            vm.request = request
            vm.mount = mock.Mock()
            vm.close = mock.Mock()
            try:
                with mock.patch.object(launcher, "host_identity", return_value={"administrator": False}), \
                     mock.patch.object(launcher.platform, "system", return_value="Windows"), \
                     mock.patch.object(launcher.subprocess, "Popen", return_value=process), \
                     mock.patch.object(launcher, "PortProxy", return_value=proxy), \
                     mock.patch.object(launcher.time, "monotonic", side_effect=lambda: next(clock)), \
                     mock.patch.object(launcher.time, "sleep"):
                    return vm.start(), len(calls)
            finally:
                if vm.log:
                    vm.log.close()

    # --- Windows x64 では、QEMU が動いている間は Linux を待ち続ける（README「仕組み」）---
    def test_windows_x64_waits_for_a_slow_linux_while_qemu_runs(self):
        vm, calls = self.boot_with_slow_linux("windows-x64", answers_after=40)
        self.assertEqual(calls, 40)
        vm.mount.assert_called_once()

    # --- ほかの PC では、応えない Linux を決まった時間で見切る（README「仕組み」）---
    def test_other_targets_still_give_up_on_a_linux_that_does_not_answer(self):
        with self.assertRaises(launcher.LaunchError):
            self.boot_with_slow_linux("windows-arm64", answers_after=40)

    # --- ディスクの土台が変わったら、ディスクを作り直す（README「起動の記録」）---
    def test_a_new_engine_resets_the_vm_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vm = launcher.LinuxVM(root, root / "state", target="macos-arm64", memory=2048, engine_sha="new")
            vm.disk.write_bytes(b"old disk")
            (vm.state / "image.json").write_text("{}")
            (vm.state / "engine.sha256").write_text("old\n")
            with mock.patch.object(launcher.sys, "stdout", io.StringIO()):
                vm.check_engine()
            self.assertFalse(vm.disk.exists())
            self.assertFalse((vm.state / "image.json").exists())
            self.assertEqual((vm.state / "engine.sha256").read_text().strip(), "new")
            vm.disk.write_bytes(b"same engine")
            vm.check_engine()
            self.assertTrue(vm.disk.exists())

    # --- QEMU には、展開したフォルダからの相対の場所を渡す（README「仕組み」）---
    def test_qemu_gets_paths_relative_to_the_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with mock.patch.object(launcher, "ROOT", root):
                vm = launcher.LinuxVM(root / "app", root / ".state", target="windows-x64", memory=2048, vendor_root=root / "runtime")
                command = vm.command()
            self.assertEqual(command[command.index("-kernel") + 1], str(Path("runtime/windows-x64/quicksand_alpine/images/alpine-3.23-amd64.kernel")))
            self.assertEqual(command[command.index("-L") + 1], str(Path("runtime/windows-x64/quicksand_qemu/bin/share/qemu")))
            self.assertIn("file=" + str(Path(".state/disk.qcow2")) + ",", command[command.index("-drive") + 1])

    # --- メモリは、パソコンの 4 分の 1（2〜4 GiB）（README「仕組み」）---
    def test_memory_is_a_quarter_of_the_host_between_2_and_4_gib(self):
        self.assertEqual(launcher.default_memory(4096), 2048)
        self.assertEqual(launcher.default_memory(16384), 4096)
        self.assertEqual(launcher.default_memory(65536), 4096)
        self.assertEqual(launcher.default_memory(12288), 3072)
        self.assertEqual(launcher.default_memory(0), 2048)

    # --- パソコンのプロキシの設定を、仮想マシンとイメージづくりに渡す（README「困ったとき」）---
    def test_host_proxies_reach_the_vm_and_the_builds(self):
        with mock.patch.object(launcher.urllib.request, "getproxies",
                               return_value={"http": "http://127.0.0.1:3128", "https": "http://user:pw@proxy.example:8080",
                                             "no": "intranet.example;<local>"}):
            proxies = launcher.host_proxies()
        self.assertEqual(proxies["http"], "http://10.0.2.2:3128")
        self.assertEqual(proxies["https"], "http://user:pw@proxy.example:8080")
        self.assertEqual(proxies["no"], "localhost,127.0.0.1,intranet.example")
        executed = []

        def execute(command, **kwargs):
            executed.append(command)
            return {"exit_code": 0, "stdout": "changed", "stderr": ""}

        vm = SimpleNamespace(arch="amd64", proxies=proxies, execute=execute)
        command = launcher.image_command(vm, "dev", "ezr-standalone:dev", "abc")
        self.assertIn("--build-arg HTTPS_PROXY=http://user:pw@proxy.example:8080", command)
        self.assertIn("--build-arg https_proxy=http://user:pw@proxy.example:8080", command)
        with mock.patch.object(launcher, "ensure_engine") as engine, \
             mock.patch.object(launcher.sys, "stdout", io.StringIO()):
            launcher.configure_proxy(vm)
        self.assertIn("http-proxy", executed[0])
        self.assertEqual(executed[1], "rc-service docker restart")
        engine.assert_called_once()
        with mock.patch.object(launcher.urllib.request, "getproxies", return_value={}):
            self.assertEqual(launcher.host_proxies(), {})
        self.assertEqual(launcher.proxy_options(SimpleNamespace(proxies={}), "-e"), "")

    # --- 仮想マシンが止まったら、画面の裏の仕事も終わる（README「仕組み」）---
    def test_the_window_worker_leaves_when_the_vm_dies(self):
        process = mock.Mock()
        process.poll.return_value = 1
        vm = SimpleNamespace(process=process, closing=False)
        done = threading.Event()
        with mock.patch.object(launcher.os, "_exit", side_effect=SystemExit(1)) as leave, \
             mock.patch.object(launcher.sys, "stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                launcher.watch_vm(vm, done)
        leave.assert_called_once_with(1)
        vm.closing = True
        with mock.patch.object(launcher.os, "_exit") as leave:
            thread = threading.Thread(target=launcher.watch_vm, args=(vm, done))
            thread.start()
            time.sleep(2.5)
            done.set()
            thread.join(3)
        leave.assert_not_called()


# --- RStudio の前の中継のテスト（README「仕組み」）---
class PortProxyTests(unittest.TestCase):
    # --- 答えの前に切れたら、つなぎ直して要求を送り直す（README「仕組み」）---
    def test_a_reset_before_the_answer_is_retried_and_the_request_replayed(self):
        import socket
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        seen, answered = [], []

        def serve():
            first = True
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection, contextlib.suppress(OSError):
                    if first:
                        first = False
                        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                        continue
                    request = connection.recv(1024)
                    if request:
                        seen.append(request)
                        connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nok")
                        answered.append(request)

        threading.Thread(target=serve, daemon=True).start()
        proxy = launcher.PortProxy(port)
        proxy.start()
        try:
            for _ in range(2):
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{proxy.port}/x", timeout=10) as response:
                    self.assertEqual(response.read(), b"ok")
        finally:
            proxy.close()
            listener.close()
        self.assertGreaterEqual(len(answered), 2)
        self.assertTrue(all(request.startswith(b"GET /x ") for request in seen))

    # --- 長い答えも、遅い相手に最後まで届ける（README「仕組み」）---
    def test_a_long_answer_reaches_a_slow_client_whole_after_the_server_closes(self):
        import socket
        body = bytes(range(256)) * 16384
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def serve():
            connection, _ = listener.accept()
            connection.recv(1024)
            connection.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            connection.close()

        threading.Thread(target=serve, daemon=True).start()
        proxy = launcher.PortProxy(listener.getsockname()[1])
        proxy.start()
        client = socket.socket()
        try:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            client.connect(("127.0.0.1", proxy.port))
            client.settimeout(20)
            client.sendall(b"GET /big HTTP/1.0\r\n\r\n")
            time.sleep(1)
            received = bytearray()
            while chunk := client.recv(65536):
                received += chunk
        finally:
            client.close()
            proxy.close()
            listener.close()
        self.assertTrue(received.endswith(body))


if __name__ == "__main__":
    unittest.main()
