"""Installation and private invitation activation; never print credentials."""
from __future__ import annotations
import argparse
import getpass
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
import httpx
import psutil
from .client import RemoteClient
from .config import BRIDGE_PORT, ClientConfig, ClientError, load_config, save_config, state_directory
from .credentials import DeviceIdentity, OSSecretStore

DEVICE_MODULE = "quant_agent_mcp.device_bridge"
DEVICE_PROTOCOL = "quant-agent-device/1"
LAUNCH_LABEL = "com.quant-agent.device"


def device_command() -> list[str]:
    executable = Path(sys.executable)
    if sys.platform == "win32":
        hidden = executable.with_name("pythonw.exe")
        if hidden.exists():
            executable = hidden
    return [str(executable), "-I", "-B", "-m", DEVICE_MODULE, "--supervise"]


def _powershell(script):
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClientError("device_startup_failed", "Windows could not update this user's device startup task.") from exc
    if result.returncode:
        raise ClientError("device_startup_failed", "Windows could not update this user's device startup task.")
    return result


def _task_prefix():
    return "$u=[Security.Principal.WindowsIdentity]::GetCurrent();$n='QuantAgentDevice-'+$u.User.Value;"


def _ps_literal(value):
    return "'" + value.replace("'", "''") + "'"


def install_startup() -> None:
    command = device_command()
    if sys.platform == "win32":
        # Scheduler starts the component outside the installer's/Codex process
        # tree. No password is stored: it runs only in this user's login session.
        script = _task_prefix()
        script += "$a=New-ScheduledTaskAction -Execute " + _ps_literal(command[0]) + " -Argument " + _ps_literal(subprocess.list2cmdline(command[1:])) + ";"
        script += "$t=New-ScheduledTaskTrigger -AtLogOn -User $u.Name;"
        script += "$p=New-ScheduledTaskPrincipal -UserId $u.Name -LogonType Interactive -RunLevel Limited;"
        script += "$s=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        script += "Register-ScheduledTask -TaskName $n -Action $a -Trigger $t -Principal $p -Settings $s -Force -ErrorAction Stop | Out-Null;"
        _powershell(script)
        # Remove only our old Run entry after the new task has been registered.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "QuantAgentDevice")
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "LaunchAgents" / (LAUNCH_LABEL + ".plist")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps({"Label": LAUNCH_LABEL, "ProgramArguments": command,
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}))
        domain = "gui/" + str(os.getuid())
        subprocess.run(["launchctl", "bootout", domain + "/" + LAUNCH_LABEL], capture_output=True, timeout=15, check=False)
        result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True, timeout=15, check=False)
        if result.returncode:
            raise ClientError("device_startup_failed", "macOS could not load the device LaunchAgent for this login session.")
    else:
        raise ClientError("unsupported_platform", "Windows or macOS is required.")


def disable_startup() -> None:
    if sys.platform == "win32":
        _powershell(_task_prefix() + _task_lookup_script()
            + "if($task){Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction Stop}")
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, "QuantAgentDevice")
        except FileNotFoundError:
            pass
    elif sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + LAUNCH_LABEL], capture_output=True, timeout=15, check=False)
        (Path.home() / "Library" / "LaunchAgents" / (LAUNCH_LABEL + ".plist")).unlink(missing_ok=True)
    else:
        raise ClientError("unsupported_platform", "Windows or macOS is required.")


def _port_open(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def device_health(config: ClientConfig | None = None, *, store=None) -> dict:
    config = config or load_config()
    store = store or OSSecretStore(config.server_url)
    if not store.get("web_key"):
        raise ClientError("device_not_configured", "Configure the device component before starting it.")
    public_key = DeviceIdentity(store, "web").public_key
    try:
        with httpx.Client(trust_env=False, timeout=1.5, follow_redirects=False) as client:
            response = client.get(f"http://127.0.0.1:{config.bridge_port}/v1/health", headers={"Origin": config.origin})
        if response.status_code != 200 or len(response.content) > 16384:
            raise ValueError("health rejected")
        data = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise ClientError("device_unavailable", "The device component is not ready or its port is occupied.") from exc
    if (not isinstance(data, dict) or data.get("ok") is not True or data.get("protocol") != DEVICE_PROTOCOL
            or data.get("server_url") != config.server_url or data.get("device_public_key") != public_key):
        raise ClientError("device_identity_mismatch", "The running component does not match this server and OS-protected device.")
    return {"ok": True, "status": "ready", "version": data.get("version"),
            "server_url": config.server_url, "bridge_url": f"http://127.0.0.1:{config.bridge_port}"}


def _expected_python_paths():
    values = {Path(sys.executable).absolute()}
    if sys.platform == "win32":
        values.add(Path(sys.executable).with_name("pythonw.exe").absolute())
        values |= {state_directory() / "python" / "Scripts" / name for name in ("python.exe", "pythonw.exe")}
    elif sys.platform == "darwin":
        values.add(state_directory() / "python" / "bin" / "python")
    return values


def _expected_process_executables():
    # Windows venv launchers keep the venv path in argv[0] while the listener
    # runs from the base interpreter. Both identities must still match: this
    # list alone never authorizes an arbitrary Python process for termination.
    paths = {path.resolve() for path in _expected_python_paths()}
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    paths.add(base)
    if sys.platform == "win32":
        paths.add(base.with_name("pythonw.exe"))
        paths.add(base.with_name("python.exe"))
    return paths


def _owned_component(process, *, supervisor=None):
    try:
        args = process.cmdline()
        paths = _expected_python_paths()
        if (not args or Path(args[0]).absolute() not in paths
                or Path(process.exe()).resolve() not in _expected_process_executables()
                or process.username().casefold() != psutil.Process().username().casefold()):
            return False
        tail = args[1:]
        base = [["-B", "-m", DEVICE_MODULE], ["-I", "-B", "-m", DEVICE_MODULE]]
        permitted = []
        if supervisor is not True:
            permitted += base + [row + ["--child"] for row in base]
        if supervisor is not False:
            permitted += [row + ["--supervise"] for row in base]
        return tail in permitted
    except (psutil.Error, OSError):
        return False


def _verified_listeners(port):
    verified = []
    for process in psutil.process_iter():
        if not _owned_component(process, supervisor=False):
            continue
        try:
            if any(item.status == psutil.CONN_LISTEN and item.laddr.ip in ("127.0.0.1", "::1")
                   and item.laddr.port == port for item in process.net_connections(kind="tcp")):
                verified.append((process, process.create_time()))
        except (psutil.Error, OSError):
            continue
    return verified


def _terminate_verified(process, created):
    try:
        if process.create_time() != created or not _owned_component(process):
            raise ClientError("device_identity_changed", "The component process identity changed; no process was stopped.")
        process.terminate()
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            if process.create_time() != created or not _owned_component(process):
                raise ClientError("device_identity_changed", "The component process identity changed; no process was killed.")
            process.kill()
            process.wait(timeout=5)
    except psutil.NoSuchProcess:
        return


def stop_device(config: ClientConfig | None = None, *, store=None) -> dict:
    config = config or load_config(allow_legacy_port=True)
    store = store or OSSecretStore(config.server_url)
    if not _port_open(config.bridge_port):
        return {"ok": True, "status": "stopped"}
    token = store.get("bridge_control")
    if token:
        try:
            with httpx.Client(trust_env=False, timeout=2, follow_redirects=False) as client:
                response = client.post(f"http://127.0.0.1:{config.bridge_port}/v1/control/stop",
                    headers={"X-Quant-Device-Control": token}, json={})
            if response.status_code == 200 and response.json().get("status") == "stopping":
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if not _port_open(config.bridge_port):
                        return {"ok": True, "status": "stopped"}
                    time.sleep(0.1)
        except (httpx.HTTPError, ValueError):
            pass
    # Older components have no authenticated control API. Never terminate an
    # arbitrary listener: require own user + exact Python/module + owning port.
    listeners = _verified_listeners(config.bridge_port)
    if len(listeners) != 1:
        raise ClientError("device_port_conflict", "Port 47631 is occupied by an unverified process; it was not stopped.")
    process, created = listeners[0]
    _stop_managed_for_fallback()
    # Windows venv redirectors add intermediary processes. Traverse only the
    # continuous, fully verified component ancestry, never unrelated parents.
    supervisors = []
    try:
        parent = process.parent()
        for _ in range(8):
            if parent is None or not _owned_component(parent):
                break
            if _owned_component(parent, supervisor=True):
                supervisors.append((parent, parent.create_time()))
            parent = parent.parent()
    except psutil.NoSuchProcess:
        pass
    for parent, parent_created in reversed(supervisors):
        _terminate_verified(parent, parent_created)
    _terminate_verified(process, created)
    if _port_open(config.bridge_port):
        raise ClientError("device_stop_failed", "The component port did not close; no new instance was started.")
    return {"ok": True, "status": "stopped"}


def _managed_task_action_script(action):
    command = device_command()
    expected_arguments = subprocess.list2cmdline(command[1:])
    script = _task_prefix() + _task_lookup_script()
    script += "if($task){$actions=@($task.Actions);if($actions.Count -ne 1 -or $actions[0].Execute -ne " + _ps_literal(command[0])
    script += " -or $actions[0].Arguments -cne " + _ps_literal(expected_arguments) + "){throw 'Device startup identity mismatch'};"
    script += action + "-ScheduledTask -TaskName $n -ErrorAction Stop;'managed'}"
    return script


def _task_lookup_script():
    # `Get-ScheduledTask -TaskName <missing> -ErrorAction SilentlyContinue`
    # still makes Windows PowerShell exit with code 1 on supported hosts. A
    # WebOnly/ManualStart install legitimately has no task, so enumerate the
    # root task folder and select our exact current-user name instead.
    return "$task=Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop|Where-Object{$_.TaskName -eq $n}|Select-Object -First 1;"


def _managed_launchagent():
    target = Path.home() / "Library" / "LaunchAgents" / (LAUNCH_LABEL + ".plist")
    if not target.is_file():
        return None
    try:
        value = plistlib.loads(target.read_bytes())
    except (ValueError, OSError, plistlib.InvalidFileException) as exc:
        raise ClientError("device_startup_failed", "The device LaunchAgent configuration is invalid.") from exc
    if value.get("Label") != LAUNCH_LABEL or value.get("ProgramArguments") != device_command():
        raise ClientError("device_startup_failed", "The LaunchAgent does not match this installed component; configure startup again.")
    return target


def _stop_managed_for_fallback():
    if sys.platform == "win32":
        # An explicit scheduler stop also suppresses its failure-restart policy.
        # The action must match this installed component before touching it.
        _powershell(_managed_task_action_script("Stop"))
    elif sys.platform == "darwin" and _managed_launchagent() is not None:
        label = "gui/" + str(os.getuid()) + "/" + LAUNCH_LABEL
        loaded = subprocess.run(["launchctl", "print", label], capture_output=True, timeout=15, check=False)
        if loaded.returncode == 0:
            result = subprocess.run(["launchctl", "bootout", label], capture_output=True, timeout=15, check=False)
            if result.returncode:
                raise ClientError("device_stop_failed", "The device LaunchAgent could not be stopped.")


def _start_managed():
    if sys.platform == "win32":
        result = _powershell(_managed_task_action_script("Start"))
        return "managed" in result.stdout
    if sys.platform == "darwin":
        target = _managed_launchagent()
        if target is not None:
            domain = "gui/" + str(os.getuid())
            loaded = subprocess.run(["launchctl", "print", domain + "/" + LAUNCH_LABEL], capture_output=True, timeout=15, check=False)
            command = ["launchctl", "kickstart", domain + "/" + LAUNCH_LABEL] if loaded.returncode == 0 else ["launchctl", "bootstrap", domain, str(target)]
            result = subprocess.run(command, capture_output=True, timeout=15, check=False)
            if result.returncode:
                raise ClientError("device_startup_failed", "The device LaunchAgent could not be started; configure startup again.")
            return True
    return False


def start_device(config: ClientConfig | None = None, *, store=None) -> dict:
    config = config or load_config()
    store = store or OSSecretStore(config.server_url)
    if _port_open(config.bridge_port):
        try:
            return {**device_health(config, store=store), "already_running": True}
        except ClientError:
            stop_device(config, store=store)
    managed = _start_managed()
    child = None
    if not managed:
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL, "close_fds": True}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        child = subprocess.Popen(device_command(), **options)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            return {**device_health(config, store=store), "startup_managed": managed}
        except ClientError:
            if child is not None and child.poll() is not None:
                break
            time.sleep(0.25)
    if child is not None and child.poll() is None:
        # Stop a verified listener before its supervisor so a failed identity
        # check cannot leave an orphaned child holding the fixed bridge port.
        try:
            if _port_open(config.bridge_port):
                stop_device(config, store=store)
        except ClientError:
            pass  # An unrelated port owner is never terminated.
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
    raise ClientError("device_start_failed", "The device component did not pass server/device identity health checks.")


def configure_client(config, *, start_on_login=False, start=False):
    if config.bridge_port != BRIDGE_PORT:
        raise ClientError("unsupported_bridge_port", "The website requires the fixed local port 47631.")
    previous = None
    try:
        previous = load_config(allow_legacy_port=True)
    except ClientError as exc:
        if exc.code != "client_not_configured":
            raise
    if previous is not None and _port_open(previous.bridge_port):
        stop_device(previous)
    elif _port_open(BRIDGE_PORT):
        # Recognize only the same user's exact installed bridge, never an
        # unrelated application occupying the product's fixed port.
        stop_device(config)
    save_config(config)
    store = OSSecretStore(config.server_url)
    DeviceIdentity(store, "web")
    DeviceIdentity(store, "mcp")
    if start_on_login:
        install_startup()
    if start:
        return start_device(config, store=store)
    return {"ok": True, "status": "configured", "started": False}

def codex_command() -> list[str]:
    """Resolve executable entrypoints without relying on Windows shell shims."""
    if sys.platform != "win32":
        executable = shutil.which("codex")
        if executable:
            return [executable]
    else:
        executable = shutil.which("codex.exe")
        if executable:
            return [executable]
        # npm installs PowerShell/CMD shims. Neither is a native executable for
        # Python CreateProcess. Invoke its actual CLI script with Node directly;
        # paths and later arguments remain separate, with no shell interpolation.
        shim = shutil.which("codex.cmd") or shutil.which("codex.ps1")
        if shim:
            directory = Path(shim).parent
            script = directory / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            bundled_node = directory / "node.exe"
            node = str(bundled_node) if bundled_node.is_file() else shutil.which("node.exe")
            if script.is_file() and node:
                return [node, str(script)]
            raise ClientError("codex_cli_incomplete",
                "The Codex npm launcher is incomplete. Repair Codex CLI or add the STDIO command in Codex Settings > MCP servers.")
    raise ClientError("codex_cli_missing",
        "Codex CLI is not on PATH. Add the STDIO command in Codex Settings > MCP servers.")


def register_codex() -> None:
    try:
        result = subprocess.run([*codex_command(), "mcp", "add", "quant-agent", "--",
            sys.executable, "-I", "-B", "-m", "quant_agent_mcp.server"],
            check=False, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ClientError("codex_cli_missing", "Codex CLI is not on PATH. Add the STDIO command in Codex Settings > MCP servers.") from exc
    if result.returncode:
        raise ClientError("codex_registration_failed", "Codex could not register the server. Review its existing quant-agent configuration.")
    print("Codex MCP registration completed.")

def main() -> None:
    parser = argparse.ArgumentParser(description="Configure the owner's remote quant research client.")
    commands = parser.add_subparsers(dest="action", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--server", required=True)
    configure.add_argument("--bridge-port", type=int, choices=[BRIDGE_PORT], default=BRIDGE_PORT)
    configure.add_argument("--start-on-login", action="store_true")
    configure.add_argument("--start", action="store_true")
    commands.add_parser("activate")
    commands.add_parser("status")
    commands.add_parser("register-codex")
    commands.add_parser("start-device")
    commands.add_parser("stop-device")
    commands.add_parser("restart-device")
    commands.add_parser("disable-startup")
    arguments = parser.parse_args()
    try:
        if arguments.action == "configure":
            config = ClientConfig(arguments.server, arguments.bridge_port)
            result = configure_client(config, start_on_login=arguments.start_on_login, start=arguments.start)
            print(json.dumps(result, ensure_ascii=False))
        elif arguments.action == "activate":
            invitation = getpass.getpass("Invitation code (hidden): ")
            client = RemoteClient()
            try:
                result = client.activate(invitation)
                print(json.dumps({"ok": True, "account_id": result["account_id"],
                    "channel": result["channel"], "expires_at": result["expires_at"]},
                    ensure_ascii=False))
            finally:
                client.close()
        elif arguments.action == "status":
            result = device_health()
            result.update(platform=sys.platform, credential_store="DPAPI" if sys.platform == "win32" else "Keychain")
            print(json.dumps(result, ensure_ascii=False))
        elif arguments.action == "register-codex":
            register_codex()
        elif arguments.action == "start-device":
            print(json.dumps(start_device(), ensure_ascii=False))
        elif arguments.action == "stop-device":
            print(json.dumps(stop_device(), ensure_ascii=False))
        elif arguments.action == "restart-device":
            stop_device()
            print(json.dumps(start_device(), ensure_ascii=False))
        elif arguments.action == "disable-startup":
            stop_device()
            disable_startup()
            print(json.dumps({"ok": True, "status": "startup_disabled", "credentials_preserved": True}))
    except ClientError as exc:
        print(json.dumps(exc.result(), ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, subprocess.SubprocessError, psutil.Error):
        print('{"ok":false,"error":{"code":"setup_failed","message":"Client setup failed; check installation permissions."}}', file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
