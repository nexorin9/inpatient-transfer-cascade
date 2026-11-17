"""单一 OpenAI-compatible 模型网关：httpx 客户端 / 重试 / 审计钩子 / 降级映射。

配置 base_url / api_key / model / timeout 全部来自 itc.config.Settings（环境变量
或 .env），网关不硬编码任何地址与密钥；业务层与网关均不 import 厂商 SDK，只用
httpx 调 POST {base_url}/chat/completions。测试通过 transport 注入
httpx.MockTransport，产品默认路径指向真实端点，不为替身改写。
"""
from __future__ import annotations

import json
import time

import httpx

from itc.config import get_settings
from itc.his.redact import assert_no_phi
from itc.model_gateway import audit, errors
from itc.model_gateway.prompt import (
    INPUT_FIELDS,
    build_judgement_input,
    build_messages,
)
from itc.model_gateway.schema import JudgementSchemaError, validate_judgement

MAX_RETRIES = 2


def assert_request_boundary(payload: dict) -> None:
    """出网前断言式字段边界检查。

    只含住院号摘要与 6 个医嘱字段；不含姓名 / 身份证 / 联系方式（H 组
    assert_no_phi），不含住院号原文，不多不少恰好 INPUT_FIELDS 七个键。
    """
    assert_no_phi(payload)
    keys = set(payload)
    expected = set(INPUT_FIELDS)
    if keys != expected:
        raise ValueError(
            f"判定输入字段越界：多={sorted(keys - expected)} 缺={sorted(expected - keys)}"
        )
    if "inpatient_no" in payload:
        raise ValueError("判定输入不得含住院号原文")


class ModelGateway:
    """唯一模型网关。settings=None 时由 get_settings() 从环境变量读取配置。"""

    def __init__(self, settings=None, transport=None):
        self.settings = settings or get_settings()
        self._ready = self.settings.llm_ready
        # httpx 的 base_url 合并要求目录以 / 结尾，否则 /v1 + /chat/completions
        # 会被拼成 /v1chat/completions；此处只规范化传给 httpx 的值，不改配置。
        base = self.settings.llm_base_url
        if base and not base.endswith("/"):
            base += "/"
        self.client = httpx.Client(
            base_url=base,
            timeout=self.settings.llm_timeout_seconds,
            headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
            transport=transport,
        )

    def close(self):
        self.client.close()

    def judge_order(self, order, transfer, *, session=None) -> dict:
        """逐条判定长嘱归属；成功返回 schema 结果，任何失败返回待人工判定。

        session 可选：带 record(...) 方法的审计存储（ModelAuditStore），每次调用
        落一行 model_audits；不传则不落审计，网关不依赖具体存储实现。
        """
        payload = build_judgement_input(order, transfer)
        assert_request_boundary(payload)
        input_digest = audit.digest_input(payload)
        body = {
            "model": self.settings.llm_model,
            "messages": build_messages(order, transfer),
            "response_format": {"type": "json_object"},
            "temperature": 0,
        }
        assert_no_phi(body)  # 整个请求体再做一次 PHI 断言

        # 配置不齐 = 模型不可达：不出网，直接降级为待人工判定。
        if not self._ready:
            exc = errors.ModelConnectionError("模型未配置或不可达，跳过出网")
            return self._failed(order, exc, input_digest, session, 0.0, 0)

        start = time.monotonic()
        retries = 0
        while True:
            try:
                parsed = self._request_once(body)
            except errors.ModelError as exc:
                if errors.is_retryable(exc) and retries < MAX_RETRIES:
                    retries += 1
                    continue
                latency_ms = (time.monotonic() - start) * 1000.0
                return self._failed(order, exc, input_digest, session, latency_ms, retries)
            else:
                latency_ms = (time.monotonic() - start) * 1000.0
                result = dict(parsed)
                result["manual"] = False
                result["degraded"] = False
                self._audit(session, latency_ms, input_digest, result["judgement"], False, retries)
                return result

    def _request_once(self, body: dict) -> dict:
        """单次协议调用：发请求、分类错误、解析 content、校验 schema。"""
        try:
            resp = self.client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise errors.ModelTimeoutError("模型调用超时") from exc
        except httpx.RequestError as exc:
            raise errors.ModelConnectionError("模型服务连接失败") from exc
        if resp.status_code >= 400:
            raise errors.ModelHTTPError(
                self._http_summary(resp), status_code=resp.status_code
            )
        parsed = self._parse_content(resp)
        try:
            validate_judgement(parsed)
        except JudgementSchemaError as exc:
            raise errors.ModelSchemaError(f"结构化校验失败：{exc}") from exc
        return parsed

    @staticmethod
    def _http_summary(resp) -> str:
        """从 4xx/5xx 错误体提取简短摘要（只取 error.message，截断长度）。"""
        summary = f"HTTP {resp.status_code}"
        try:
            data = resp.json()
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict) and err.get("message"):
                summary += f": {str(err['message'])[:200]}"
            elif isinstance(err, str):
                summary += f": {err[:200]}"
        except Exception:
            pass
        return summary

    @staticmethod
    def _parse_content(resp) -> dict:
        """解析 OpenAI-compatible 响应的 choices[0].message.content 为 dict。"""
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, dict):
                parsed = content
            elif isinstance(content, str):
                text = content.strip()
                if text.startswith("```"):  # 兼容模型给 JSON 套代码围栏
                    text = text.strip("`")
                    if text.lower().startswith("json"):
                        text = text[4:].strip()
                parsed = json.loads(text)
            else:
                raise ValueError("content 非字符串或对象")
        except Exception as exc:
            raise errors.ModelUnparsableError("模型响应不可解析") from exc
        if not isinstance(parsed, dict):
            raise errors.ModelUnparsableError("模型响应不是 JSON 对象")
        return parsed

    def _failed(self, order, exc, input_digest, session, latency_ms, retries) -> dict:
        result = errors.failed_judgement(
            self._order_no(order), exc, latency_ms=latency_ms, retries=retries
        )
        self._audit(session, latency_ms, input_digest, "UNDETERMINED", True, retries)
        return result

    @staticmethod
    def _order_no(order):
        if isinstance(order, dict):
            return order.get("order_no", "")
        return getattr(order, "order_no", "")

    def _audit(self, session, latency_ms, input_digest, judgement, degraded, retries) -> None:
        if session is None or not hasattr(session, "record"):
            return
        session.record(
            model=self.settings.llm_model,
            latency_ms=latency_ms,
            input_digest=input_digest,
            judgement=judgement,
            degraded=degraded,
            retries=retries,
        )
