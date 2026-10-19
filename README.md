# hcl-mcp

让 AI agent 通过 [MCP (Model Context Protocol)](https://modelcontextprotocol.io) 直接控制 **H3C HCL 模拟器**（H3C Cloud Lab 5.10.3）：创建拓扑、增删设备、启停设备、建立链路、读写设备 console、下发 CLI 命令。

协议基于对 HCL 后端 `SimwareWrapper`（TCP 16600 明文协议）的逆向分析，无需修改 HCL 安装目录。

## 功能

| 工具 | 说明 |
|------|------|
| `list_models` | 列出可创建的设备型号（MSR36-20 / S5820V2-54QS-GE / S6850 / VSR-88 / F1060 / PC / Server） |
| `topo_create` | 创建/重建 MCP 托管拓扑会话 |
| `topo_inspect` | 查看设备列表与链路占用 |
| `device_add` / `device_remove` | 添加 / 删除设备 |
| `device_start` / `device_stop` | 启动 / 停止设备 VM |
| `link_add` / `link_remove` | 在两台设备的指定接口间建立 / 删除链路 |
| `device_console` | 读取设备 console 输出（TCP 方式，无需管理员权限） |
| `device_exec` | 下发 CLI 命令并回读输出（如 `system-view`、`display ip interface brief`） |

## 环境要求

- Windows + H3C HCL 5.10.3（需先启动 HCL GUI，使 `SimwareWrapper` 监听 `127.0.0.1:16600`）
- VirtualBox（HCL 自带的 6.0.14 即可，`VBoxManage.exe` 须在配置路径可找到）
- Python 3.10+，`pip install -r requirements.txt`

## 安装配置

在 MCP 客户端（Trae / Claude Desktop / Cursor 等）的 MCP 配置中添加：

```json
{
  "mcpServers": {
    "hcl-mcp": {
      "command": "python",
      "args": ["E:\\mcps\\hcl-mcp\\server.py"],
      "env": {
        "HCL_VERSION_DIR": "C:\\Program Files (x86)\\HCL\\version",
        "HCL_VBOXMANAGE": "C:\\Program Files\\Oracle\\VirtualBox\\VBoxManage.exe"
      }
    }
  }
}
```

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HCL_WRAPPER_PORT` | `16600` | SimwareWrapper 端口 |
| `HCL_VERSION_DIR` | `E:\Program Files (x86)\HCL\version` | HCL version 目录 |
| `HCL_VBOXMANAGE` | `E:\Program Files\Oracle\VirtualBox\VBoxManage.exe` | VBoxManage 路径 |

## 快速上手（agent 视角）

```
1. topo_create                                  # 建立拓扑会话
2. device_add(model="MSR36-20", name="R1")      # 添加路由器 → id=1
3. device_add(model="S5820V2-54QS-GE", name="SW1")  # 添加交换机 → id=2
4. link_add(1, "GE_0/1", 2, "GE_0/1")           # 建立链路
5. device_start(1); device_start(2)             # 启动（约 1-2 分钟）
6. device_console(1, seconds=10)                # 观察启动输出
7. device_exec(1, "display version")            # 执行 CLI
```

### 注意事项

- **建链请按端口升序**：SimwareWrapper 按链路创建顺序分配主机网卡（第 1 条 → NIC2，第 2 条 → NIC3 …），而设备固件按端口顺序映射接口（第 1 个端口 → NIC2 …）。若跨过第 1 个端口先建后续端口的链路，网卡序号与固件错位，接口会保持物理 DOWN。即：先建每台设备的第 1 个端口对，再建第 2 对。
- **拓扑名一次失败即烧毁**：create 失败后同名拓扑在 Wrapper 进程内永久不可用，MCP 已按进程启动时间自动生成唯一拓扑名规避。
- **S5820V2 内存须为 384MB**：512MB 会导致启动失败（errorCode=204），型号库已内置正确值。
- **不要调用 `set_hcl_tcp`**：会使 SimwareWrapper 占用 console 管道，MCP 已移除该调用。
- 设备 console 通过串口 2 的 `tcpserver` 模式访问（`127.0.0.1:<port>`），绕开命名管道的管理员权限问题。
- 同一拓扑的创建/建链/启停须在同一 MCP 进程会话内完成（进程重启会放弃 Wrapper 会话）。

## 测试

```bash
# 需要 HCL GUI 已启动
python test_smoke.py   # 冒烟测试: 建拓扑→加设备→建链→巡检→清理
python test_e2e.py     # 端到端: 含启动设备与 console 交互
```

## 协议要点（逆向自 SimwareWrapper）

- 请求行：`<seq> simware <command> [args...]\n`，含空格参数用双引号
- 响应：`<seq>-<errorCode>-<params...>\r\n/end\r\n`，多段 params 以 `\r\n` 分隔
- errorCode：`100/101` 成功，`102` 进度数据，`204` create 失败，`218` 会话失效，`227` 拓扑数达上限
- 会话初始化序列：`set_version_path` → `create_topology` → `start_recover`

## License

MIT
