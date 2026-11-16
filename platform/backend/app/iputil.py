# -*- coding: utf-8 -*-
"""客户端真实 IP 解析：审计中间件与登录安全模块共用。

部署拓扑下 backend 仅监听 127.0.0.1（经 nginx 反代，host 网络），
nginx 以 proxy_set_header X-Real-IP $remote_addr 传递真实客户端 IP；
多级代理时 X-Forwarded-For 最左端为原始客户端。
"""
from fastapi import Request


def client_ip(request: Request) -> str:
    xri = (request.headers.get("x-real-ip") or "").strip()
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xri or xff or (request.client.host if request.client else "unknown")
