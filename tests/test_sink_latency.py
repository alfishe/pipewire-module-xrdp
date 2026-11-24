#!/usr/bin/env python3
"""
Check that the playback latency chansrv reports reaches the players.

Starts a private PipeWire daemon, a fake xrdp-chansrv audio socket and the
module under test, links a player to xrdp-sink and waits until the player's
port sees the reported latency.

Usage: test_sink_latency.py [path to libpipewire-module-xrdp.so]
Exit status follows automake: 0 pass, 1 fail, 77 skip.
"""

import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

SINK_MSG_DATA = 0
SINK_MSG_LATENCY = 2
REPORTED_US = 57000
TIMEOUT_S = 15
SINK_NAME = "xrdp-sink-test"
PLAYER_NAME = "xrdp-test-player"


def skip(why):
    print("SKIP: " + why)
    sys.exit(77)


def fail(why):
    print("FAIL: " + why)
    sys.exit(1)


def recv_exact(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


class FakeChansrv(threading.Thread):
    """Plays the chansrv side of the sink socket"""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(1)
        self.acked = threading.Event()
        self.got_data = threading.Event()
        self.error = None

    def run(self):
        try:
            conn, _ = self.server.accept()
            # announce latency reports, as chansrv does on connect
            conn.sendall(struct.pack("<III", SINK_MSG_LATENCY, 12, 0))
            while True:
                header = recv_exact(conn, 8)
                if header is None:
                    return
                code, size = struct.unpack("<II", header)
                if code == SINK_MSG_LATENCY:
                    if size != 8:
                        self.error = "latency answer of size %d" % size
                        return
                    self.acked.set()
                    conn.sendall(struct.pack("<III", SINK_MSG_LATENCY, 12,
                                             REPORTED_US))
                elif code == SINK_MSG_DATA:
                    if recv_exact(conn, size - 8) is None:
                        return
                    self.got_data.set()
                else:
                    if size > 8 and recv_exact(conn, size - 8) is None:
                        return
        except OSError as e:
            self.error = str(e)


def player_latency_ns(env):
    out = subprocess.run(["pw-dump"], env=env, capture_output=True,
                         text=True, timeout=10).stdout
    try:
        objects = json.loads(out)
    except ValueError:
        return None
    for o in objects:
        info = o.get("info") or {}
        props = info.get("props") or {}
        if not o.get("type", "").endswith("Node"):
            continue
        if props.get("node.name") != PLAYER_NAME:
            continue
        for lat in (info.get("params") or {}).get("Latency", []):
            if lat.get("direction") == "Input":
                return lat.get("minNs"), lat.get("maxNs")
    return None


def write_wireplumber_config(config_home):
    """WirePlumber 0.4 Lua overrides: no ALSA, Bluetooth or camera devices"""
    for sub_dir, text in (
            ("main.lua.d", "alsa_monitor.enabled = false\n"
                           "v4l2_monitor.enabled = false\n"
                           "libcamera_monitor.enabled = false\n"),
            ("bluetooth.lua.d", "bluez_monitor.enabled = false\n")):
        d = os.path.join(config_home, "wireplumber", sub_dir)
        os.makedirs(d)
        with open(os.path.join(d, "99-xrdp-test.lua"), "w") as f:
            f.write(text)


def system_module_dir():
    for d in ("/usr/lib/x86_64-linux-gnu/pipewire-0.3",
              "/usr/lib64/pipewire-0.3", "/usr/lib/pipewire-0.3"):
        if os.path.exists(os.path.join(d,
                                       "libpipewire-module-protocol-native.so")):
            return d
    return None


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    module = (sys.argv[1] if len(sys.argv) > 1 else
              os.path.join(here, "..", "src", ".libs",
                           "libpipewire-module-xrdp.so"))
    module = os.path.abspath(module)
    if not os.path.exists(module):
        skip("module not built: " + module)
    for tool in ("pipewire", "wireplumber", "dbus-run-session", "pw-cli",
                 "pw-cat", "pw-link", "pw-dump"):
        if shutil.which(tool) is None:
            skip(tool + " not installed")

    tmp = tempfile.mkdtemp(prefix="pw-xrdp-test-")
    procs = []
    try:
        env = dict(os.environ)
        for var in ("PIPEWIRE_REMOTE", "PULSE_SERVER", "DBUS_SESSION_BUS_ADDRESS"):
            env.pop(var, None)
        env["XDG_RUNTIME_DIR"] = tmp
        env["PIPEWIRE_RUNTIME_DIR"] = tmp
        env["PIPEWIRE_DEBUG"] = "2"
        env["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
        env["XDG_STATE_HOME"] = os.path.join(tmp, "state")
        env["GIO_USE_VFS"] = "local"
        env["GVFS_DISABLE_FUSE"] = "1"
        write_wireplumber_config(env["XDG_CONFIG_HOME"])

        daemon = subprocess.Popen(["pipewire"], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        procs.append(daemon)
        deadline = time.time() + TIMEOUT_S
        while not os.path.exists(os.path.join(tmp, "pipewire-0")):
            if daemon.poll() is not None or time.time() > deadline:
                skip("cannot start a private pipewire daemon")
            time.sleep(0.1)

        # the session manager configures the stream ports and links the
        # player to the only sink; keep it away from real devices
        procs.append(subprocess.Popen(
            ["dbus-run-session", "--", "wireplumber"], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        chansrv = FakeChansrv(os.path.join(tmp, "audio_out"))
        chansrv.start()

        # pw-cli only loads modules by name from the module directory, so
        # build one with the system modules plus the module under test
        moddir = os.path.join(tmp, "modules")
        os.mkdir(moddir)
        sysdir = system_module_dir()
        if sysdir is None:
            skip("cannot find the PipeWire module directory")
        for name in os.listdir(sysdir):
            os.symlink(os.path.join(sysdir, name), os.path.join(moddir, name))
        os.remove(os.path.join(moddir, "libpipewire-module-xrdp.so")) \
            if os.path.lexists(os.path.join(moddir,
                                            "libpipewire-module-xrdp.so")) \
            else None
        os.symlink(module, os.path.join(moddir, "libpipewire-module-xrdp.so"))

        menv = dict(env)
        menv["PIPEWIRE_MODULE_DIR"] = moddir
        menv["XRDP_SOCKET_PATH"] = tmp
        menv["XRDP_PULSE_SINK_SOCKET"] = "audio_out"
        menv["XRDP_PULSE_SOURCE_SOCKET"] = "audio_in"
        menv["DISPLAY"] = ":99"
        procs.append(subprocess.Popen(
            ["pw-cli", "-m", "load-module", "libpipewire-module-xrdp",
             "sink.node.latency=1024",
             "sink.stream.props={node.name=%s}" % SINK_NAME,
             "source.stream.props={node.name=xrdp-source-test}"],
            env=menv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL))

        silence = os.path.join(tmp, "silence.wav")
        with wave.open(silence, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            w.writeframes(b"\0" * 4 * 44100 * (TIMEOUT_S + 5))
        procs.append(subprocess.Popen(
            ["pw-cat", "--playback",
             "--properties", "{ node.name = %s }" % PLAYER_NAME, silence],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

        linked = False
        latency = None
        while time.time() < deadline:
            if not linked:
                links = subprocess.run(["pw-link", "-l"], env=env,
                                       capture_output=True, text=True,
                                       timeout=10).stdout
                linked = ("%s:output_FL" % PLAYER_NAME in links and
                          "%s:send_FL" % SINK_NAME in links)
            elif chansrv.acked.is_set():
                latency = player_latency_ns(env)
                if latency == (REPORTED_US * 1000, REPORTED_US * 1000):
                    break
            if chansrv.error:
                fail("chansrv side: " + chansrv.error)
            time.sleep(0.2)

        if not linked:
            fail("could not link the player to " + SINK_NAME)
        if not chansrv.acked.is_set():
            fail("module did not answer the latency announcement")
        if not chansrv.got_data.is_set():
            fail("module sent no audio")
        if latency != (REPORTED_US * 1000, REPORTED_US * 1000):
            fail("player sees latency %s, expected %d ns" %
                 (latency, REPORTED_US * 1000))
        print("PASS: player sees %d us playback latency" % REPORTED_US)
        return 0
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in reversed(procs):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
