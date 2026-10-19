# -*- coding: utf-8 -*-
"""MCP server e2e 测试：stdio 启动 server 子进程并调用工具"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
PY = sys.executable


async def call(session, name, args=None):
    res = await session.call_tool(name, args or {})
    texts = [c.text for c in res.content if hasattr(c, "text")]
    return "\n".join(texts)


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("== list_models")
            print((await call(s, "list_models"))[:300])
            print("== topo_create")
            print(await call(s, "topo_create"))
            print("== device_add x2")
            print(await call(s, "device_add", {"model": "S5820V2-54QS-GE", "name": "SW-A"}))
            print(await call(s, "device_add", {"model": "MSR36-20", "name": "RT-B"}))
            print("== link_add")
            print(await call(s, "link_add", {"device_id1": 1, "interface1": "GE_0/1",
                                             "device_id2": 2, "interface2": "GE_0/0"}))
            print("== device_start 1")
            print(await call(s, "device_start", {"device_id": 1}))
            print("== wait console ...")
            for i in range(18):
                await asyncio.sleep(5)
                out = await call(s, "device_console", {"device_id": 1, "seconds": 2})
                if out and "(无输出)" not in out and "无法连接" not in out:
                    print(f"t+{(i+1)*5}s console tail:", out[-200:])
                    break
                print(f"t+{(i+1)*5}s no output yet")
            print("== topo_inspect")
            print(await call(s, "topo_inspect"))
            print("== device_stop 1")
            print(await call(s, "device_stop", {"device_id": 1}))
            print("== link_remove")
            print(await call(s, "link_remove", {"device_id1": 1, "interface1": "GE_0/1"}))
            print("== device_remove 2")
            print(await call(s, "device_remove", {"device_id": 2}))
            print("== device_remove 1")
            print(await call(s, "device_remove", {"device_id": 1}))
            print("== E2E DONE")


if __name__ == "__main__":
    asyncio.run(main())
