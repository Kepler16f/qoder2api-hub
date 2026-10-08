# -*- coding: utf-8 -*-
"""E2E test: lan_mode toggle + runtime rebind + reveal + versions."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

PORT = 18790
BASE = "http://127.0.0.1:%d" % PORT


def req(method, path, body=None, token=None, timeout=6):
    r = urllib.request.Request(BASE + path, method=method)
    if token:
        r.add_header("X-Panel-Token", token)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, data=data, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def wait_health(timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, data = req("GET", "/health")
            if code == 200 and "accounts" in data:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="qd-lan-test-")
    accounts = os.path.join(tmp, "accounts")
    os.makedirs(accounts)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["QD_DEBUG_PANEL"] = "1"
    gwlog = open(os.path.join(tmp, "gw.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "qoder_proxy.py", "--port", str(PORT),
         "--accounts-dir", accounts, "--usage-dir",
         os.path.join(tmp, "usage")],
        stdout=gwlog, stderr=subprocess.STDOUT, env=env)
    try:
        assert wait_health(), "gateway did not become healthy"
        print("[1] healthy OK")

        code, login = req("POST", "/panel/login", {"password": "admin"})
        assert code == 200 and login.get("token"), (code, login)
        token = login["token"]
        print("[2] panel login OK")

        code, s = req("GET", "/settings", token=token)
        assert code == 200, (code, s)
        assert s.get("lan_mode") is False, s.get("lan_mode")
        assert s.get("gateway_version"), "gateway_version missing"
        assert "client_version" in s, "client_version missing"
        assert s.get("bind_port") == PORT, s.get("bind_port")
        print("[3] /settings initial OK: lan_mode=False, versions present")

        # reveal with default password must be refused
        code, rv = req("GET", "/settings/reveal?id=gateway", token=token)
        assert code == 403, (code, rv)
        print("[4] reveal refused with default password OK")

        # set a custom panel password
        code, pw = req("POST", "/panel/password",
                       {"current": "admin", "new": "test-pass-123"}, token=token)
        assert code == 200 and pw.get("token"), (code, pw)
        token = pw.get("token")
        print("[5] panel password changed OK")

        # turn LAN on -> persist + rebind to 0.0.0.0
        code, r1 = req("POST", "/settings/save", {"lan_mode": True}, token=token)
        assert code == 200, (code, r1)
        assert r1.get("lan_mode") is True, r1
        assert r1.get("lan_rebinding") is True, r1
        print("[6] lan_mode on saved, rebind requested")

        time.sleep(2.5)   # 0.4s kick + ~0.5s poll + rebind
        code, h = req("GET", "/health")
        assert code == 200, "health after rebind-on failed"
        code, s = req("GET", "/settings", token=token)
        if code != 200:
            # 诊断：新密码重新登录是否可行 + 网关日志尾部
            code2, login2 = req("POST", "/panel/login",
                                {"password": "test-pass-123"})
            gwlog.flush()
            with open(os.path.join(tmp, "gw.log"), encoding="utf-8",
                      errors="replace") as fh:
                tail = fh.read()[-3000:]
            raise AssertionError(
                "after rebind-on: code=%s relogin=%s token=%r\n--- gw.log tail ---\n%s"
                % (code, code2, token, tail))
        assert s.get("lan_mode") is True, s.get("lan_mode")
        ips = s.get("lan_ips") or []
        assert isinstance(ips, list), ips
        print("[7] rebind ON OK: lan_ips=%s key=%s" % (
            ips, s.get("effective_key_masked")))
        assert s.get("effective_key_masked"), "effective key should exist in LAN mode"

        # reveal the gateway key now
        code, rv = req("GET", "/settings/reveal?id=gateway", token=token)
        assert code == 200 and rv.get("key"), (code, rv)
        print("[8] reveal gateway key OK: %s..." % rv["key"][:6])

        # turn LAN off -> rebind back to 127.0.0.1
        code, r2 = req("POST", "/settings/save", {"lan_mode": False}, token=token)
        assert code == 200 and r2.get("lan_mode") is False, (code, r2)
        time.sleep(2.5)
        code, h = req("GET", "/health")
        assert code == 200, "health after rebind-off failed"
        code, s = req("GET", "/settings", token=token)
        assert s.get("lan_mode") is False, s.get("lan_mode")
        assert s.get("lan_ips") == [], s.get("lan_ips")
        print("[9] rebind OFF OK")

        # persisted setting survives a restart
        code, r3 = req("POST", "/settings/save", {"lan_mode": True}, token=token)
        assert code == 200, (code, r3)
        time.sleep(2.5)
        proc.terminate()
        proc.wait(timeout=10)
        proc2 = subprocess.Popen(
            [sys.executable, "qoder_proxy.py", "--port", str(PORT),
             "--accounts-dir", accounts, "--usage-dir",
             os.path.join(tmp, "usage")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        try:
            assert wait_health(), "restart did not become healthy"
            # 面板会话刻意不持久化：重启后须用（已持久化的）新密码重新登录
            code, login3 = req("POST", "/panel/login",
                               {"password": "test-pass-123"})
            assert code == 200 and login3.get("token"), (code, login3)
            code, s = req("GET", "/settings", token=login3["token"])
            assert s.get("lan_mode") is True, "persisted lan_mode lost: %s" % s.get("lan_mode")
            print("[10] persisted lan_mode survives restart OK")
        finally:
            proc2.terminate()
            proc2.wait(timeout=10)
        print("ALL PASS")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
