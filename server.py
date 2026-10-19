# -*- coding: utf-8 -*-
"""HCL MCP Server — 让 agent 通过 MCP 管理 H3C HCL 模拟器。

逆向自 HCL 5.10.3 (SimwareWrapper 16600 明文协议)。
工具集: topo_create/topo_inspect/device_add/device_remove/device_start/device_stop/
        link_add/link_remove/device_exec/device_console/list_models

运行要求:
- HCL GUI 已启动 (SimwareWrapper 监听 127.0.0.1:16600)
- 管理员权限运行本 server 可解锁 CLI 命令下发 (device_exec)，否则仅可观察 console
"""
import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hcl_client import SimwareClient, SimwareError, DEVICE_MODELS, slot_dict_str

from mcp.server.fastmcp import FastMCP

# ---------- 配置 ----------
WRAPPER_HOST = "127.0.0.1"
WRAPPER_PORT = int(os.environ.get("HCL_WRAPPER_PORT", "16600"))
# 以下两个路径随机器而异, 可用环境变量覆盖
VERSION_DIR = os.environ.get("HCL_VERSION_DIR", r"E:\Program Files (x86)\HCL\version")
VBOXMANAGE = os.environ.get("HCL_VBOXMANAGE", r"E:\Program Files\Oracle\VirtualBox\VBoxManage.exe")
# 拓扑名按进程启动唯一化: 一次失败的 create 会将该 topo 名在 Wrapper 进程内永久烧毁
# (实测 SimwareVM.py:700 IndexError 后同名拓扑所有 create 必败, 仅 Wrapper 重启可解)
_BOOT_TAG = time.strftime("%H%M%S")
TOPO_NAME = f"hcl_mcp_{_BOOT_TAG}"
WORK_DIR = os.path.join(tempfile.gettempdir(), TOPO_NAME)
# 随机 UDP_BASE 避免多 MCP 实例间 telnet/链路端口冲突
import random
UDP_BASE = random.randint(22000, 30000)
UDP_NIO_START = UDP_BASE + 200     # 链路 UDP NIO 起始
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "mcp_state.json")
LOG_FILE = os.path.join(BASE_DIR, "mcp_server.log")

IS_ADMIN = bool(ctypes.windll.shell32.IsUserAnAdmin())
UDP_BASE_HOST = WRAPPER_HOST


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------- 工程目录 ----------
def setup_workdir(workdir=None):
    workdir = workdir or WORK_DIR
    os.makedirs(workdir, exist_ok=True)
    for sub in ("ConfigDisk", "DeviceConfig", "SerialFile", "working"):
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)
    dv = os.path.join(workdir, "deviceversion.txt")
    if not os.path.exists(dv):
        with open(dv, "w") as f:
            f.write("MSR36:b75\nS6850:b70\nS5800:b75\nF1090:b64\nF1060:b64\n")
    net = os.path.join(workdir, f"{TOPO_NAME}.net")
    if not os.path.exists(net):
        with open(net, "w") as f:
            f.write(f'version = 5.10.3\n[vbox {WRAPPER_HOST}:{WRAPPER_PORT}]\n'
                    f'    workingdir = None\n    udp = {UDP_BASE}\n[Non-vBox-DATA]\n    workdir = working\n')
    pj = os.path.join(workdir, "project.json")
    if not os.path.exists(pj):
        with open(pj, "w", encoding="utf-8") as f:
            f.write(json.dumps({"projectInfo": {"name": TOPO_NAME, "path": TOPO_NAME,
                                                "visibility": "2", "introduction": "", "label": []},
                                "deviceInfoList": []}))
    return workdir


# ---------- 状态 ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"devices": {}, "next_id": 1, "next_udp": UDP_NIO_START}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


# ---------- Wrapper 连接管理（持久连接 + 自动恢复） ----------
class Wrapper:
    def __init__(self):
        self.cli = None

    def get(self) -> SimwareClient:
        if self.cli is None or self.cli.sock is None:
            self._connect()
        return self.cli

    def _connect(self):
        setup_workdir()
        c = SimwareClient(WRAPPER_HOST, WRAPPER_PORT)
        c.connect()
        c.set_version_path(VERSION_DIR)
        # session 名每连接唯一: 同名 session 跨连接复用会与旧连接的 ClearTopoInfo 竞态
        c.create_topology(TOPO_NAME, WORK_DIR, f"s{uuid.uuid4().hex[:8]}", UDP_BASE)
        # 注意: 不调用 set_hcl_tcp(18600) — 否则 SimwareWrapper 会占用设备 console pipe
        # 转发到 SimwareClient，导致 MCP 无法直接读写 console pipe。
        # start_recover 用于初始化拓扑内部状态，缺失会导致 create 时 204 错误。
        c.start_recover()
        self.cli = c
        log(f"wrapper session established topo={TOPO_NAME}")

    def reset(self):
        if self.cli:
            try:
                self.cli.close()
            except OSError:
                pass
        self.cli = None

    def call(self, fn, *a, **kw):
        """执行 wrapper 操作；仅连接类错误重连重试，协议错误(SimwareError)直接抛出。"""
        try:
            return fn(self.get(), *a, **kw)
        except (ConnectionError, OSError, TimeoutError) as e:
            log(f"wrapper retry after conn error: {e!r}")
            self.reset()
            time.sleep(0.3)
            return fn(self.get(), *a, **kw)


WRAP = Wrapper()


# ---------- console pipe ----------
def pipe_read(pipe_name, seconds, handle=None):
    """读取 console 输出。handle=None 时自开自关（只读）。"""
    own = handle is None
    if own:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # WaitNamedPipe 等待 pipe 实例可用（被 SimwareWrapper 占用时可等待释放）
        full = rf"\\.\pipe\{pipe_name}"
        if not k32.WaitNamedPipeW(full, 5000):
            return None
        h = k32.CreateFileW(full, 0x80000000, 0, None, 3, 0, None)
        if h in (-1, 0xFFFFFFFFFFFFFFFF):
            return None
        handle = h
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(65536)
    rd = ctypes.c_ulong()
    out = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if k32.ReadFile(ctypes.c_void_p(handle), buf, 65536, ctypes.byref(rd), None) and rd.value:
            out += buf.raw[:rd.value]
        else:
            time.sleep(0.2)
    if own:
        k32.CloseHandle(ctypes.c_void_p(handle))
    return out.decode("utf-8", "replace")


def pipe_write(pipe_name, data: bytes):
    """写 console。需要管理员权限；返回 (ok, err)。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k32.CreateFileW(rf"\\.\pipe\{pipe_name}", 0x80000000 | 0x40000000, 0, None, 3, 0, None)
    if h in (-1, 0xFFFFFFFFFFFFFFFF):
        return False, f"CreateFile err={ctypes.get_last_error()} (需管理员权限运行 MCP server)"
    wr = ctypes.c_ulong()
    ok = k32.WriteFile(ctypes.c_void_p(h), data, len(data), ctypes.byref(wr), None)
    k32.CloseHandle(ctypes.c_void_p(h))
    if not ok:
        return False, f"WriteFile err={ctypes.get_last_error()}"
    return True, ""


def device_pipe(st, dev_id):
    d = st["devices"].get(str(dev_id))
    if not d:
        raise ValueError(f"device {dev_id} not found")
    return d["pipe"]


def device_console_port(st, dev_id):
    d = st["devices"].get(str(dev_id))
    if not d:
        raise ValueError(f"device {dev_id} not found")
    return d.get("console_port")


# ---------- console via TCP (uart2 tcpserver) ----------
def tcp_console(port, seconds=5.0, send: bytes = None) -> str:
    """通过 TCP 连接设备 console（uart2 tcpserver）。send 可选，发送后读取。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(min(max(seconds, 1.0), 60.0) + 2)
        s.connect(("127.0.0.1", port))
    except OSError as e:
        return f"[无法连接 console TCP 127.0.0.1:{port}: {e}]"
    out = b""
    try:
        if send:
            s.sendall(send)
        deadline = time.time() + seconds
        s.setblocking(False)
        while time.time() < deadline:
            try:
                chunk = s.recv(65536)
                if chunk:
                    out += chunk
                else:
                    break
            except BlockingIOError:
                time.sleep(0.15)
    finally:
        s.close()
    return out.decode("utf-8", "replace")


# ---------- MCP ----------
mcp = FastMCP("hcl-mcp", instructions="H3C HCL 模拟器控制。先 topo_create 初始化会话（或直接 device_add，会自动初始化）。")


@mcp.tool()
def list_models() -> str:
    """列出可创建的设备型号及其接口布局摘要"""
    parts = []
    for model, info in DEVICE_MODELS.items():
        ifs = sorted(info["slots"].items())
        summary = f"{len(ifs)} 接口: " + ", ".join(n for _, (_, n) in ifs[:6])
        if len(ifs) > 6:
            summary += f" ...共{len(ifs)}个"
        parts.append(f"{model} (vmdk={info['vmdk']}, RAM={info['ram']}MB, {summary})")
    return "\n".join(parts)


@mcp.tool()
def topo_create(topo_name: str = "") -> str:
    """创建/重建 MCP 托管拓扑会话（幂等）。会清除 MCP 状态记录中的旧设备登记（不影响 VBox 中已运行 VM 的清理）。自定义 topo_name 时切换到新拓扑。"""
    global TOPO_NAME, WORK_DIR
    if topo_name:
        TOPO_NAME = topo_name
        WORK_DIR = os.path.join(os.path.dirname(WORK_DIR), topo_name)
    setup_workdir()
    # 重置连接: Wrapper 会累积拓扑状态, 多次 create_topology 后 create 会返回 204/227
    # reset 后 _connect 会用新 TOPO_NAME 创建拓扑, 无需再调 create_topology
    WRAP.reset()
    WRAP.get()  # 触发 _connect, 其内部已 create_topology(TOPO_NAME,...)
    st = load_state()
    st["devices"] = {}
    st["next_id"] = 1
    st["next_udp"] = UDP_NIO_START
    save_state(st)
    return f"拓扑会话已建立: topo={TOPO_NAME}, workdir={WORK_DIR}, udp_base={UDP_BASE} (telnet={UDP_BASE}+设备ID)"


@mcp.tool()
def topo_inspect() -> str:
    """查看当前 MCP 托管拓扑：设备列表(id/型号/名称/接口占用/telnet端口)与链路"""
    st = load_state()
    if not st["devices"]:
        return "拓扑为空 (topo=%s)。" % TOPO_NAME
    lines = [f"topo={TOPO_NAME}, workdir={WORK_DIR}"]
    for did in sorted(st["devices"], key=int):
        d = st["devices"][did]
        used = [f"{ifn}(slot{s})" for s, ifn in d.get("links", {}).items()]
        cp = d.get("console_port", "?")
        lines.append(f"[{did}] {d['name']} model={d['model']} telnet={d['telnet']} "
                     f"console=tcp://127.0.0.1:{cp} 接口占用: {', '.join(used) or '无'}")
    return "\n".join(lines)


@mcp.tool()
def device_add(model: str, name: str = "") -> str:
    """向拓扑新增设备。model 见 list_models()；name 可选（默认 型号_N）。返回设备 id。"""
    if model not in DEVICE_MODELS:
        return f"错误: 未知型号 {model}。可用: {', '.join(DEVICE_MODELS)}"
    st = load_state()
    m = DEVICE_MODELS[model]
    dev_id = st["next_id"]
    dev_name = f"{model}_{dev_id}"
    if name:
        dev_name = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_") or dev_name
    if any(d["name"] == dev_name for d in st["devices"].values()):
        return f"错误: 设备名 {dev_name} 已存在"
    telnet = UDP_BASE + dev_id
    mac = f"68-50-0c-1b-{dev_id:02d}-00"
    def _create(c):
        return c.create(dev_name, dev_id, m["ram"], m["d_ram"], slot_dict_str(m["slots"]),
                        telnet, mac, m["vmdk"])
    try:
        rep = WRAP.call(_create)
    except SimwareError as e:
        if "errorCode=218" not in str(e) and "errorCode=204" not in str(e):
            return f"创建失败: {e}"
        # 218: 连接会话状态失效(常见于 HCL 重启后老连接)
        # 204: Wrapper 拓扑状态累积导致 create 失败 —— 均重连重试一次
        log(f"create got {e.code}, reconnecting wrapper session")
        WRAP.reset()
        try:
            rep = WRAP.call(_create)
        except SimwareError as e2:
            return f"创建失败(重连后): {e2}"
    pipe = f"{TOPO_NAME}-device{dev_id}"
    # 将 uart2 从 pipe 改为 tcpserver，避免与 SimwareWrapper 竞争命名管道
    console_port = UDP_BASE + 1000 + dev_id
    vm_name = f"{TOPO_NAME}-device{dev_id}"
    try:
        subprocess.run([VBOXMANAGE, "modifyvm", vm_name, "--uartmode2", "tcpserver", str(console_port)],
                       capture_output=True, timeout=30)
    except Exception as e:
        log(f"modifyvm uartmode2 tcpserver failed: {e}")
    st["devices"][str(dev_id)] = {"name": dev_name, "model": model, "telnet": telnet,
                                  "pipe": pipe, "console_port": console_port,
                                  "started": False, "links": {}}
    st["next_id"] = dev_id + 1
    save_state(st)
    return (f"设备已创建: id={dev_id} name={dev_name} model={model} telnet={telnet} "
            f"console=tcp://127.0.0.1:{console_port} reply={rep}。用 device_start 启动。")


@mcp.tool()
def device_remove(device_id: int) -> str:
    """删除设备（运行中会先停止）。"""
    st = load_state()
    did = str(device_id)
    d = st["devices"].get(did)
    if not d:
        return f"错误: 设备 {device_id} 不存在"
    errs = []
    if d.get("started"):
        try:
            WRAP.call(lambda c: c.stop(device_id))
        except SimwareError as e:
            errs.append(f"stop: {e}")
    try:
        WRAP.call(lambda c: c.delete(device_id))
    except SimwareError as e:
        return f"删除失败: {e}"
    # 释放链路对端登记
    for s, ifname in list(d.get("links", {}).items()):
        st["devices"][did]["links"].pop(s, None)
    del st["devices"][did]
    save_state(st)
    return f"设备 {device_id} ({d['name']}) 已删除" + (f"; 警告: {errs}" if errs else "")


@mcp.tool()
def device_start(device_id: int) -> str:
    """启动设备（VM 开机，约 1-2 分钟完成启动）"""
    st = load_state()
    did = str(device_id)
    if did not in st["devices"]:
        return f"错误: 设备 {device_id} 不存在"
    WRAP.call(lambda c: c.start(device_id))
    st["devices"][did]["started"] = True
    save_state(st)
    d = st["devices"][did]
    return f"设备 {device_id} ({d['name']}) 启动指令已执行。约 1-2 分钟后可用 device_console 或 device_exec 交互。"


@mcp.tool()
def device_stop(device_id: int) -> str:
    """停止设备（VM 关机）"""
    st = load_state()
    did = str(device_id)
    if did not in st["devices"]:
        return f"错误: 设备 {device_id} 不存在"
    WRAP.call(lambda c: c.stop(device_id))
    st["devices"][did]["started"] = False
    save_state(st)
    return f"设备 {device_id} ({st['devices'][did]['name']}) 已停止"


@mcp.tool()
def link_add(device_id1: int, interface1: str, device_id2: int, interface2: str) -> str:
    """在两台设备间建立链路。interface 用接口名如 'GE_0/1'/'XGE_0/49'（须未被占用且两台均已创建）。"""
    st = load_state()
    d1, d2 = st["devices"].get(str(device_id1)), st["devices"].get(str(device_id2))
    if not d1 or not d2:
        return f"错误: 设备不存在 ({device_id1}/{device_id2})"
    if device_id1 == device_id2:
        return "错误: 不支持同一设备的自环"
    m = DEVICE_MODELS[d1["model"]]
    m2 = DEVICE_MODELS[d2["model"]]
    s1 = s2 = None
    for s, (phy, n) in m["slots"].items():
        if n.lower() == interface1.lower():
            s1 = s
    for s, (phy, n) in m2["slots"].items():
        if n.lower() == interface2.lower():
            s2 = s
    if s1 is None:
        return f"错误: {d1['model']} 无接口 {interface1}"
    if s2 is None:
        return f"错误: {d2['model']} 无接口 {interface2}"
    if str(s1) in d1.get("links", {}):
        return f"错误: {d1['name']} {interface1} 已被占用"
    if str(s2) in d2.get("links", {}):
        return f"错误: {d2['name']} {interface2} 已被占用"

    u1, u2 = st["next_udp"], st["next_udp"] + 1
    def _a(c):
        c.create_udp(device_id1, s1, u1, "127.0.0.1", u2)
    def _b(c):
        c.create_udp(device_id2, s2, u2, "127.0.0.1", u1)
    try:
        WRAP.call(_a)
        WRAP.call(_b)
    except SimwareError as e:
        try:
            WRAP.call(lambda c: c.delete_udp(device_id1, s1))
        except SimwareError:
            pass
        return f"链路建立失败: {e}"
    st["next_udp"] = u2 + 1
    st["devices"][str(device_id1)].setdefault("links", {})[str(s1)] = f"{d2['name']}:{interface2}"
    st["devices"][str(device_id2)].setdefault("links", {})[str(s2)] = f"{d1['name']}:{interface1}"
    save_state(st)
    return f"链路已建立: {d1['name']}:{interface1} <-> {d2['name']}:{interface2} (UDP {u1}<->{u2})"


@mcp.tool()
def link_remove(device_id1: int, interface1: str) -> str:
    """删除设备某接口上的链路（两端同时删除）。"""
    st = load_state()
    d1 = st["devices"].get(str(device_id1))
    if not d1:
        return f"错误: 设备 {device_id1} 不存在"
    s1 = None
    for s, (phy, n) in DEVICE_MODELS[d1["model"]]["slots"].items():
        if n.lower() == interface1.lower():
            s1 = s
    if s1 is None or str(s1) not in d1.get("links", {}):
        return f"错误: 接口 {interface1} 无链路"
    peer_desc = d1["links"][str(s1)]  # "name:if"
    peer_name, peer_if = peer_desc.rsplit(":", 1)
    s2 = None
    d2_id = None
    for did, d in st["devices"].items():
        if d["name"] == peer_name:
            d2_id = did
            for s, (phy, n) in DEVICE_MODELS[d["model"]]["slots"].items():
                if n.lower() == peer_if.lower():
                    s2 = s
            break
    if d2_id is None or s2 is None:
        return f"错误: 对端 {peer_desc} 状态不一致"
    try:
        WRAP.call(lambda c: c.delete_udp(device_id1, s1))
        WRAP.call(lambda c: c.delete_udp(int(d2_id), s2))
    except SimwareError as e:
        return f"删除失败: {e}"
    st["devices"][str(device_id1)]["links"].pop(str(s1), None)
    st["devices"][d2_id]["links"].pop(str(s2), None)
    save_state(st)
    return f"链路已删除: {d1['name']}:{interface1} <-> {peer_desc}"


@mcp.tool()
def device_console(device_id: int, seconds: float = 5.0) -> str:
    """读取设备 console 输出（最近 seconds 秒）。设备须已启动。通过 TCP(uart2 tcpserver) 访问，无需管理员权限。"""
    st = load_state()
    try:
        port = device_console_port(st, device_id)
    except ValueError as e:
        return str(e)
    if not st["devices"][str(device_id)].get("started"):
        return f"设备 {device_id} 未启动"
    if not port:
        return f"设备 {device_id} 未配置 console TCP 端口（旧拓扑创建的设备）"
    out = tcp_console(port, min(max(seconds, 1.0), 60.0))
    return out[-4000:] if out and not out.startswith("[") else (out or "(无输出)")


@mcp.tool()
def device_exec(device_id: int, command: str, wait: float = 2.0) -> str:
    """向设备 console 下发 CLI 命令并回读输出。通过 TCP(uart2 tcpserver) 访问，无需管理员权限。命令间会话状态保留（如先 system-view 再 vlan 10）。"""
    st = load_state()
    try:
        port = device_console_port(st, device_id)
    except ValueError as e:
        return str(e)
    if not st["devices"][str(device_id)].get("started"):
        return f"设备 {device_id} 未启动"
    if not port:
        return f"设备 {device_id} 未配置 console TCP 端口（旧拓扑创建的设备）"
    out = tcp_console(port, min(max(wait, 0.5), 30.0), send=command.encode("gbk", "replace") + b"\r\n")
    return (out or "(无输出)")[-4000:]


# ---------- main ----------
if __name__ == "__main__":
    log(f"=== hcl-mcp starting (admin={IS_ADMIN}) ===")
    try:
        WRAP.get()
        log("initial wrapper session OK")
        save_state({"devices": {}, "next_id": 1, "next_udp": UDP_NIO_START})  # 新拓扑名, 状态清零
    except Exception:
        log("initial connect failed (HCL GUI 未运行?): " + traceback.format_exc(limit=2))
    mcp.run(transport="stdio")
