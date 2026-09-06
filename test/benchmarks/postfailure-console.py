#!/usr/bin/env python3
"""Bounded, authenticated console inspection of an already failed benchmark VM."""

import hashlib
import json
from pathlib import Path
import re
import shlex
import socket
import stat
import time
import uuid


MAX_OUTPUT = 256 * 1024
CONSOLE_SECONDS = 90
COMMANDS = (
  ("waiting-jobs", "systemctl list-jobs --no-pager"),
  ("failed-units", "systemctl --failed --no-pager"),
  ("boot-units", "systemctl show --property=Id --property=ActiveState --property=SubState "
    "--property=Result --property=Job --property=MainPID --property=ControlPID --property=TimeoutStartUSec "
    "plymouth-start.service plymouth-read-write.service plymouth-quit.service plymouth-quit-wait.service "
    "NetworkManager.service sshd.service sshdgenkeys.service ufw.service network.target sysinit.target basic.target"),
  ("process-waits", "ps -e -o pid,ppid,stat,etimes,wchan:32,comm"),
  ("addresses", "ip -brief address"),
  ("routes", "ip route show table all"),
  ("listeners", "ss -lntup"),
  ("critical-chain", "systemd-analyze --no-pager critical-chain"),
  ("recent-boot-journal", "journalctl -b -n 100 --no-pager -o short-monotonic"),
  ("plymouth-kernel-waits", """count=0
for p in /proc/[0-9]*; do
  IFS= read -r name < "$p/comm" 2>/dev/null || continue
  case "$name" in plymouth|plymouthd) ;; *) continue ;; esac
  count=$((count + 1)); [ "$count" -le 8 ] || break
  printf '\\nPROCESS %s %s\\n' "${p##*/}" "$name"
  for item in wchan stack syscall; do
    printf '%s\\n' "$item"; cat "$p/$item" 2>/dev/null
  done
  fds=0
  for fd in "$p"/fd/*; do
    fds=$((fds + 1)); [ "$fds" -le 64 ] || break
    printf 'fd %s: ' "${fd##*/}"; readlink "$fd"
  done
done"""),
)


def save(path, value):
  temporary = path.with_name(path.name + ".tmp")
  temporary.write_text(json.dumps(value, indent=2) + "\n")
  temporary.replace(path)


def digest(path):
  with path.open("rb") as stream:
    return hashlib.file_digest(stream, "sha256").hexdigest()


def plain(data):
  text = data.decode(errors="replace") if isinstance(data, (bytes, bytearray)) else data
  text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
  text = re.sub(r"\x1bP.*?\x1b\\", "", text, flags=re.S)
  text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[78]", "", text)
  text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text.replace("\r\n", "\n").replace("\r", "\n"))
  return text


def redact(data):
  text = plain(data)
  text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
    "[PRIVATE KEY REDACTED]", text, flags=re.S)
  lines = []
  for line in text.splitlines():
    if (line.strip() == "omarchy" or re.search(
        r"(?i)(password|passwd|\bpsk\b|secret|token|authorization:|authorized_keys|PRIVATE KEY)", line)):
      line = "[CREDENTIAL LINE REDACTED]"
    lines.append(line)
  return "\n".join(lines) + "\n"


class Dialogue:
  def __init__(self, connection, deadline, status, persist):
    self.connection = connection
    self.deadline = deadline
    self.status = status
    self.persist = persist
    self.received = bytearray()
    self.query_end = 0

  def send(self, data, kind, *, sensitive=False):
    if time.monotonic() >= self.deadline:
      raise TimeoutError("Console diagnostic deadline exceeded")
    if len(self.status["interventions"]) >= 128:
      raise RuntimeError("Console intervention limit exceeded")
    self.status["interventions"].append({"kind": kind, "at_monotonic_s": time.monotonic(),
      "input": "[FIXTURE CREDENTIAL OMITTED]" if sensitive else data.decode("ascii")})
    self.persist()
    self.connection.settimeout(max(0.01, min(2, self.deadline - time.monotonic())))
    self.connection.sendall(data)

  def answer_queries(self):
    # Only responses to known terminal queries, never a command or key sequence.
    pattern = rb"\x1b\[(?:18t|6n|5n|0?c)|\x1bP\+q[0-9A-Fa-f;]+\x1b\\"
    for match in re.finditer(pattern, self.received):
      if match.end() <= self.query_end:
        continue
      self.query_end = match.end()
      query = match.group()
      replies = {b"\x1b[18t": b"\x1b[8;24;80t", b"\x1b[6n": b"\x1b[24;80R",
        b"\x1b[5n": b"\x1b[0n", b"\x1b[c": b"\x1b[?1;2c", b"\x1b[0c": b"\x1b[?1;2c"}
      reply = replies.get(query)
      if query.lower() == b"\x1bp+q6e616d65\x1b\\":
        reply = b"\x1bP1+r" + query[4:-2] + b"=7674313030\x1b\\"
      if reply:
        self.send(reply, "terminal-query-response")

  def expect(self, pattern, seconds, *, reject_login_failure=False):
    deadline = min(self.deadline, time.monotonic() + seconds)
    while True:
      text = plain(self.received)
      if reject_login_failure and re.search(r"(?i)(login incorrect|authentication failure)", text):
        raise RuntimeError("Console login rejected; no diagnostic command sent")
      match = re.search(pattern, text, re.M)
      if match:
        return match
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        raise TimeoutError("Expected console boundary did not arrive before its deadline")
      self.connection.settimeout(min(1, remaining))
      try:
        data = self.connection.recv(4096)
      except socket.timeout:
        continue
      if not data:
        raise ConnectionError("Console disconnected")
      if len(self.received) + len(data) > MAX_OUTPUT:
        raise RuntimeError("Console output exceeded 256 KiB limit")
      self.received.extend(data)
      self.answer_queries()


def capture(directory, hostname, qmp, *, qemu_process=None, prefix="", timeout=CONSOLE_SECONDS):
  directory = Path(directory)
  # Enforce the boundary inside this helper, independent of its caller.
  manifest = json.loads((directory / "manifest.json").read_text())
  if (manifest.get("status") not in {"timeout", "standalone-reboot-failed"}
      or manifest.get("validation_passed") is not False or not manifest.get("failure")):
    raise RuntimeError("Live console diagnostics require a persisted failed measurement")
  if prefix not in {"", "standalone-"} or hostname != "omarchy-benchmark":
    raise RuntimeError("Console diagnostics require the exact disposable benchmark fixture")
  if not 0 < timeout <= CONSOLE_SECONDS:
    raise ValueError("Console diagnostic budget must be at most 90 seconds")
  output = directory / (prefix + "timeout-console.log")
  status_path = directory / (prefix + "timeout-console.json")
  if output.exists() or status_path.exists():
    raise RuntimeError("Existing console diagnostics must not be overwritten")
  started = time.monotonic()
  status = {"schema_version": 1, "measurement_valid": False, "status": "starting",
    "after_measurement_failure": True, "original_failure": manifest["failure"],
    "budget_seconds": timeout, "output_limit_bytes": MAX_OUTPUT, "authenticated_root": False,
    "commands_started": False, "interventions": [], "helper_sha256": digest(Path(__file__))}
  persist = lambda: save(status_path, status)
  persist()
  serial = directory / "serial.log"
  dialogue = None
  connection = None
  try:
    status["observed_chardevs_before_guard"] = qmp("query-chardev")
    persist()
    serial_devices = [row for row in status["observed_chardevs_before_guard"] if row.get("label") == "serial0"]
    expected = "file:" + str(serial)
    # QEMU 8.2.2's file backend reports only "file", without its output path.
    # Bind it to the actual live Popen owned by the supervisor, not manifest alone.
    argv = getattr(qemu_process, "args", None)
    recorded_argv = manifest.get("installed_boot_qemu_argv", manifest.get("qemu_argv"))
    if (not isinstance(argv, list) or argv != recorded_argv or not serial.is_absolute()
        or qemu_process.poll() is not None or argv.count("-serial") != 1
        or argv.index("-serial") + 1 >= len(argv) or argv[argv.index("-serial") + 1] != expected):
      raise RuntimeError("Console capture requires the owned live QEMU and exact recorded serial argument")
    status["owned_qemu"] = {"pid": qemu_process.pid, "live": True, "actual_argv_matches_manifest": True,
      "serial_argument": expected, "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest()}
    persist()
    if (len(serial_devices) != 1 or serial_devices[0].get("filename") not in {"file", expected}
        or serial_devices[0].get("frontend-open") is not True or not stat.S_ISREG(serial.lstat().st_mode)):
      raise RuntimeError("Unrecognized serial0 file backend; console capture refused")
    before_bytes = serial.stat().st_size
    backend = {"type": "socket", "data": {"addr": {"type": "inet", "data": {
      "host": "127.0.0.1", "port": "0", "ipv4": True}},
      "server": True, "wait": False, "telnet": False}}
    status["backend"] = backend
    status["interventions"].append({"kind": "qmp-chardev-change", "id": "serial0", "backend": backend,
      "at_monotonic_s": time.monotonic()})
    persist()
    qmp("chardev-change", {"id": "serial0", "backend": backend})
    status["original_serial"] = {"bytes_before_switch": before_bytes, "bytes": serial.stat().st_size,
      "sha256": digest(serial), "bytes_received_before_backend_detached": serial.stat().st_size - before_bytes,
      "preservation": "Original file is closed by chardev-change and never opened for writing by diagnostics"}
    persist()
    changed = [row for row in qmp("query-chardev") if row.get("label") == "serial0"]
    address = re.fullmatch(r"(?:disconnected:)?tcp:127\.0\.0\.1:([0-9]+),server=on",
      changed[0].get("filename", "")) if len(changed) == 1 else None
    if not address or changed[0].get("frontend-open") is not True or not 0 < int(address[1]) < 65536:
      raise RuntimeError("Changed serial backend is not the expected loopback listener")
    port = int(address[1])
    status["observed_backend"] = changed[0]
    status["connection"] = {"host": "127.0.0.1", "port": port}
    persist()
    connection = socket.create_connection(("127.0.0.1", port), timeout=min(2, timeout))
    dialogue = Dialogue(connection, started + timeout, status, persist)
    dialogue.send(b"\r", "refresh-login-prompt")
    dialogue.expect(r"^" + re.escape(hostname) + r" login: *$", 20)
    dialogue.send(b"root\r", "login-user")
    dialogue.expect(r"(?i)^password: *$", 10, reject_login_failure=True)
    dialogue.send(b"omarchy\r", "fixture-password", sensitive=True)
    prompt = r"^(?:\[root@" + re.escape(hostname) + r"[^\n]*\]|root@" + re.escape(hostname) + r":[^\n]*)# *$"
    dialogue.expect(prompt, 15, reject_login_failure=True)
    marker = "OMARCHY_CONSOLE_" + uuid.uuid4().hex
    challenge = "printf '\\n" + marker + "_UID\\n'; id -u; printf '" + marker + "_UID_END\\n'\r"
    dialogue.send(challenge.encode(), "root-identity-challenge")
    dialogue.expect(r"^" + marker + r"_UID\n0\n" + marker + r"_UID_END$", 5, reject_login_failure=True)
    status["authenticated_root"] = True
    status["commands_started"] = True
    status["command_names"] = [name for name, _ in COMMANDS]
    persist()
    script = []
    for name, command in COMMANDS:
      script.extend(["printf '\\nCOMMAND " + name + "\\n'",
        "env LC_ALL=C TERM=dumb SYSTEMD_COLORS=0 SYSTEMD_PAGER=cat timeout 4 bash -c " + shlex.quote(command),
        "printf 'COMMAND_EXIT %s\\n' \"$?\""])
    suite = "timeout 42 bash -c " + shlex.quote("\n".join(script))
    suite += "; printf '\\n" + marker + "_DONE:%s\\n' \"$?\"\r"
    # Quoted newlines form shell continuation lines; each fits the TTY input cap.
    status["maximum_command_line_bytes"] = max(len(line) for line in suite.encode().splitlines())
    if status["maximum_command_line_bytes"] > 4000:
      raise RuntimeError("Diagnostic command exceeds conservative TTY line limit")
    dialogue.send(suite.encode(), "readonly-diagnostic-commands")
    result = dialogue.expect(r"^" + marker + r"_DONE:([0-9]+)$", timeout)
    status.update(status="collected", suite_exit_status=int(result.group(1)))
  except Exception as error:
    status.update(status="failed", error_type=type(error).__name__, error=str(error))
  finally:
    if connection:
      connection.close()
    if dialogue:
      redacted = redact(dialogue.received).encode()
      output.write_text(redacted[:MAX_OUTPUT].decode(errors="ignore"))
      status["received_bytes"] = len(dialogue.received)
      status["output_truncated"] = len(redacted) > MAX_OUTPUT
    if "original_serial" in status:
      original = status["original_serial"]
      original["unchanged_during_console_capture"] = serial.stat().st_size == original["bytes"] and digest(serial) == original["sha256"]
      if not original["unchanged_during_console_capture"]:
        status.update(status="failed", error="Original serial file changed after detachment")
    status["elapsed_seconds"] = time.monotonic() - started
    persist()
  return status
