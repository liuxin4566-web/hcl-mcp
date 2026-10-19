# -*- coding: utf-8 -*-
"""部署副本冒烟测试: E:\mcps\hcl-mcp\server.py"""
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
PY = sys.executable


async def call(session, name, args=None):
    res = await session.call_tool(name, args or {})
    return "\n".join(c.text for c in res.content if hasattr(c, "text"))


async def main():
    params = StdioServerParameters(command=PY, args=[SERVER])
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("[1] topo_create:", (await call(s, "topo_create"))[:120])
            print("[2] device_add:", (await call(s, "device_add", {"model": "S5820V2-54QS-GE", "name": "SW-A"}))[:160])
            print("[3] device_add:", (await call(s, "device_add", {"model": "MSR36-20", "name": "RT-B"}))[:160])
            print("[4] link_add:", await call(s, "link_add", {"device_id1": 1, "interface1": "GE_0/1",
                                                             "device_id2": 2, "interface2": "GE_0/0"}))
            print("[5] topo_inspect:")
            print(await call(s, "topo_inspect"))
            print("[6] device_remove:", (await call(s, "device_remove", {"device_id": 1}))[:80])
            print("[7] device_remove:", (await call(s, "device_remove", {"device_id": 2}))[:80])
            print("SMOKE DONE")


if __name__ == "__main__":
    asyncio.run(main())
