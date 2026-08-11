#!/usr/bin/env python3
"""枚举局域网 _miio / _miot 服务，尝试通过 model/服务名区分设备类型。"""
import time
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

class Listener(ServiceListener):
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info:
            props = {}
            for k, v in (info.properties or {}).items():
                try:
                    props[k.decode()] = v.decode()
                except Exception:
                    props[str(k)] = str(v)
            print("SVC:", name, "| addrs:", info.parsed_addresses(), "| props:", props, flush=True)
    def remove_service(self, zc, type_, name):
        pass
    def update_service(self, zc, type_, name):
        pass

zc = Zeroconf()
for t in ("_miio._udp.local.", "_miot._tcp.local."):
    ServiceBrowser(zc, t, Listener())
time.sleep(9)
zc.close()
print("done")
