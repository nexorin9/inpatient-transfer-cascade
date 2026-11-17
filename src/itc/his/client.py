"""HIS 出站客户端：统一鉴权、超时、状态码分类，凭据缺失启动自检明确报错。

产品默认路径指向真实 HIS 端点；测试通过 httpx.MockTransport（transport 参数）
注入替身。源码不设任何 mock 开关、不静默降级到本地替身。
"""
from __future__ import annotations

import httpx

from itc.config import ConfigurationError, require_his_credentials
from itc.his import errors


class HisClient:
    """HIS REST 出站客户端。``.client`` 为 httpx.Client。"""

    def __init__(self, base_url=None, token=None, transport=None, timeout=10.0):
        resolved_url, resolved_token = self._resolve_credentials(base_url, token)
        self.base_url = resolved_url
        self.token = resolved_token
        self.client = httpx.Client(
            base_url=resolved_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {resolved_token}"},
            transport=transport,
        )

    @staticmethod
    def _resolve_credentials(base_url, token):
        # 两者都显式提供（测试注入 MockTransport 的路径）-> 直接使用，不读环境变量。
        # 任一未提供 -> 走配置面自检：缺凭据抛 ConfigurationError，不静默降级。
        if base_url is None or token is None:
            cfg = require_his_credentials()
            if base_url is None:
                base_url = cfg.his_base_url
            if token is None:
                token = cfg.his_api_token
        # 显式传空串 / 空白也要明确报错，绝不发无鉴权请求。
        if not str(base_url).strip() or not str(token).strip():
            raise ConfigurationError(
                "HIS 出站凭据缺失：HIS_BASE_URL / HIS_API_TOKEN 不能为空；"
                "请在环境变量或 .env 配置后重试（不提供本地替身）"
            )
        return base_url, token

    def get(self, path, params=None):
        try:
            resp = self.client.get(path, params=params)
        except httpx.RequestError as exc:
            raise errors.NetworkHisError(f"HIS 出站网络异常：{exc}") from exc
        return self._check(resp)

    def post(self, path, json=None, headers=None):
        try:
            resp = self.client.post(path, json=json, headers=headers)
        except httpx.RequestError as exc:
            raise errors.NetworkHisError(f"HIS 出站网络异常：{exc}") from exc
        return self._check(resp)

    @staticmethod
    def _check(resp):
        if resp.status_code >= 400:
            kind = errors.classify_status(resp.status_code)
            message = f"HIS 返回 HTTP {resp.status_code}"
            if kind == "retryable":
                raise errors.RetryableHisError(
                    message, status_code=resp.status_code, response=resp
                )
            raise errors.NonRetryableHisError(
                message, status_code=resp.status_code, response=resp
            )
        return resp

    def close(self):
        self.client.close()
