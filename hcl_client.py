# -*- coding: utf-8 -*-
"""HCL SimwareWrapper 协议客户端库。

协议（逆向自 HCL 5.10.3）：
- 请求行: `<seq> simware <command> [args...]\\n`，含空格参数用双引号
- 响应:   `<seq>-<errorCode>-<params...>\\r\\n/end\\r\\n`
  errorCode: 100=done 无参数, 101=有数据参数, 102=进度/中间数据
  params 多值时以 \\r\\n 分段（每段独立一行），直到 /end 结束符
- 端口: TCP 16600 (0.0.0.0)，GUI(SimwareClient) 是另一个客户端
"""
import re
import socket
import time


class SimwareError(Exception):
    def __init__(self, code, message, params=None):
        super().__init__(f"errorCode={code}: {message}")
        self.code = code
        self.params = params or []


class SimwareClient:
    def __init__(self, host="127.0.0.1", port=16600, timeout=60.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.seq = 0
        self._buf = b""

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        self.sock.settimeout(2.0)
        return self

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    # ---------- 底层 ----------
    def _recv_more(self):
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return False
        if not chunk:
            raise ConnectionError("SimwareWrapper closed connection")
        self._buf += chunk
        return True

    def _read_reply(self, seq, timeout):
        """读取以 <seq>- 开头、以 /end 结尾的完整响应块。"""
        deadline = time.time() + timeout
        marker = f"{seq}-".encode()
        while True:
            if marker in self._buf:
                # 丢弃 marker 之前的杂散数据
                self._buf = self._buf[self._buf.index(marker):]
                end = self._buf.find(b"/end")
                if end != -1:
                    block = self._buf[:end]
                    self._buf = self._buf[end + 4:]
                    lines = [l for l in block.split(b"\r\n") if l.strip()]
                    # 第一行: <seq>-<code>-<param1> ; 后续行是续传的 params
                    parts = lines[0].decode("utf-8", "replace").split("-", 2)
                    rseq, code = int(parts[0]), int(parts[1])
                    params = [parts[2]] if len(parts) > 2 and parts[2] != "" else []
                    params += [l.decode("utf-8", "replace") for l in lines[1:]]
                    return rseq, code, params
            if time.time() > deadline:
                raise TimeoutError(f"no /end reply for seq {seq}, buf={self._buf!r}")
            self._recv_more()

    def raw(self, line, timeout=None):
        """发送原始命令行并返回 (code, params)。"""
        self.seq += 1
        payload = f"{self.seq} {line}\n".encode("utf-8")
        self.sock.sendall(payload)
        rseq, code, params = self._read_reply(self.seq, timeout or self.timeout)
        if rseq != self.seq:
            raise SimwareError(-1, f"reply seq mismatch: {rseq} != {self.seq}")
        return code, params

    # ---------- 命令封装 ----------
    def _ok(self, cmd, timeout=None):
        code, params = self.raw(cmd, timeout)
        if code not in (100, 101):
            raise SimwareError(code, f"{cmd!r} failed", params)
        return params

    def query(self, cmd, timeout=None):
        """查询类命令，返回 params 列表。"""
        code, params = self.raw(cmd, timeout)
        if code not in (100, 101, 102):
            raise SimwareError(code, f"{cmd!r} failed", params)
        return params

    # ---- 基本信息 ----
    def version(self):
        return self.query("simwarewrapper version")

    def system_memory(self):
        return self.query("simwarewrapper system_memory")

    # ---- 拓扑 ----
    def create_topology(self, topo_name, tempdir, session_name, udp_base):
        return self._ok(f'simware create_topology {topo_name} "{tempdir}" {session_name} {udp_base}')

    def set_version_path(self, path):
        return self._ok(f'simware set_version_path "{path}"')

    def set_hcl_tcp(self, port):
        """告知 Wrapper HCL 的 TCP 端口。GUI 每会话建立后必发 set_hcl_tcp 18600；
        协议会话缺失此命令时设备 VM 引导卡死（串口 0 字节，实测 2026-10-18）。"""
        return self._ok(f"simware set_hcl_tcp {port}")

    def start_recover(self):
        """GUI 会话建立后紧跟 set_hcl_tcp 发送；无遗留设备时为 no-op。"""
        return self._ok("simware start_recover")

    def save_as(self, workingdir, name):
        return self._ok(f'simware save_as "{workingdir}" {name}')

    def save(self):
        return self._ok("simware save")

    # ---- 设备 ----
    def create(self, name, dev_id, ram, d_ram, slot_dict, telnet_port, bridge_mac, vmdk,
               chipset="2100", slot_idx=0, flags="1 0"):
        return self._ok(
            f'simware create "{name}" {dev_id} {slot_idx} {chipset} {ram} {d_ram} '
            f'{{{slot_dict}}} {telnet_port} {bridge_mac} {vmdk} {flags}', timeout=120)

    def start(self, dev_id, slot_idx=0):
        return self._ok(f"simware start {dev_id} {slot_idx}", timeout=120)

    def stop(self, dev_id, slot_idx=0):
        return self._ok(f"simware stop {dev_id} {slot_idx}", timeout=120)

    def suspend(self, dev_id, slot_idx=0):
        return self._ok(f"simware suspend {dev_id} {slot_idx}")

    def resume(self, dev_id, slot_idx=0):
        return self._ok(f"simware resume {dev_id} {slot_idx}")

    def delete(self, dev_id, slot_idx=0):
        return self._ok(f"simware delete {dev_id} {slot_idx}")

    def rename(self, dev_id, new_name, slot_idx=0):
        return self._ok(f'simware rename {dev_id} {slot_idx} "{new_name}"')

    def set_memory(self, dev_id, mem, slot_idx=0):
        return self._ok(f"simware set_memory {dev_id} {slot_idx} {mem}")

    # ---- 链路 ----
    def create_udp(self, dev_id, port_no, local_udp, remote_host, remote_udp, slot_idx=0, flag=1):
        return self._ok(f"simware create_udp {dev_id} {slot_idx} {port_no} {local_udp} "
                        f"{remote_host} {remote_udp} {flag}")

    def delete_udp(self, dev_id, port_no, slot_idx=0):
        return self._ok(f"simware delete_udp {dev_id} {slot_idx} {port_no}")

    # ---- 配置文件 ----
    def get_file_list(self, dev_id, slot_idx=0, timeout=30):
        return self.query(f"simware get_file_list {dev_id} {slot_idx}", timeout)

    def export_config(self, dev_id, slot_idx, fname):
        return self._ok(f"simware export_config {dev_id} {slot_idx} {fname}", timeout=60)

    def import_config(self, dev_id, slot_idx, fname):
        return self._ok(f"simware import_config {dev_id} {slot_idx} {fname}", timeout=60)

    def save_cfg_now(self, dev_id, slot_idx=0):
        return self._ok(f"simware save_cfg_now {dev_id} {slot_idx}", timeout=60)


# ---------- 型号库（自 deviceInfo.ini + resources 逆向） ----------
PHY_ETH_10M, PHY_FE, PHY_GE, PHY_XGE, PHY_FGE = 1, 2, 3, 4, 5

DEVICE_MODELS = {
    "MSR36-20": {
        "vmdk": "msr36_b75.vmdk", "ram": 512, "d_ram": 600,
        "slots": {**{i: (PHY_GE, f"GE_0/{i}") for i in (0, 1, 2, 7, 8, 9, 10)},
                  **{i: (56, f"Ser_1/{i-3}") for i in (3, 4, 5, 6)}},
    },
    "S5820V2-54QS-GE": {
        "vmdk": "s5800_b75.vmdk", "ram": 384, "d_ram": 600,
        "slots": {0: (1, "M-GE_0/0"), **{i: (3, f"GE_0/{i}") for i in range(1, 49)},
                  49: (4, "XGE_0/49"), 50: (4, "XGE_0/50"), 51: (4, "XGE_0/51"),
                  52: (4, "XGE_0/52"), 53: (5, "FGE_0/53"), 54: (5, "FGE_0/54")},
    },
    "S6850": {
        "vmdk": "s6850_b70.vmdk", "ram": 512, "d_ram": 600,
        "slots": {0: (1, "M-GE_0/0"), **{i: (3, f"GE_0/{i}") for i in range(1, 49)},
                  49: (4, "XGE_0/49"), 50: (4, "XGE_0/50"), 51: (4, "XGE_0/51"),
                  52: (4, "XGE_0/52"), 53: (5, "FGE_0/53"), 54: (5, "FGE_0/54")},
    },
    "PC": {"vmdk": "pc.vmdk", "ram": 512, "d_ram": 600, "slots": {1: (3, "GE_0/1")}},
    "Server": {"vmdk": "server.vmdk", "ram": 512, "d_ram": 600, "slots": {1: (3, "GE_0/1")}},
    "F1060": {"vmdk": "f1060_fw_b64.vmdk", "ram": 512, "d_ram": 600,
              "slots": {i: (3, f"GE_0/{i}") for i in range(0, 24)}},
    "VSR-88": {"vmdk": "vsr-88_b75.vmdk", "ram": 512, "d_ram": 600,
               "slots": {**{i: (PHY_GE, f"GE_0/{i}") for i in (0, 1, 2, 7, 8, 9, 10)},
                         **{i: (56, f"Ser_1/{i-3}") for i in (3, 4, 5, 6)}},
    },
}


def slot_dict_str(slots):
    """{slot_no: (phy, ifname)} -> "{0:(3,'GE_0/0'),...}"""
    items = ",".join(f"{k}:({v[0]},'{v[1]}')" for k, v in sorted(slots.items()))
    return items
