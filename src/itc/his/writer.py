"""HIS 出站写 2 类接口：执行地点变更与停嘱草稿（只到 DRAFT）。

白名单：仅 ``/api/v1/orders/{order_id}/execution-site`` 与 ``/api/v1/orders/drafts``。
停嘱草稿由医师在 HIS 内自行处置，产品不代签、不提交；源码不含任何提交 / 签名
医嘱的调用路径，写路径白名单在代码层强制校验。

幂等：执行地点变更的 ``Idempotency-Key`` 取自 G1 图幂等模块（按业务字段哈希，
与时间戳 / 重试次数无关）；写前查 registry（duck typing），命中已成功记录则
跳过请求。超次重试失败登记进待重写队列，不静默丢弃。
"""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import quote

from itc.graph.idempotency import idempotency_key
from itc.his import errors
from itc.his.redact import assert_no_phi
from itc.store.db import append_audit

log = logging.getLogger(__name__)

# 写接口白名单（模板路径）。代码层只允许这两条出站写路径。
WRITABLE_PATHS = (
    "/api/v1/orders/{order_id}/execution-site",
    "/api/v1/orders/drafts",
)

_DRAFTS_PATH = "/api/v1/orders/drafts"
_EXEC_SITE_RE = re.compile(r"^/api/v1/orders/[^/]+/execution-site$")


def _path(value) -> str:
    """URL 路径段编码防注入。"""
    return quote(str(value), safe="")


def assert_writable(path: str) -> None:
    """校验写路径在白名单内，越界直接抛不可重试错误。"""
    if path == _DRAFTS_PATH or _EXEC_SITE_RE.match(path):
        return
    raise errors.NonRetryableHisError(f"写路径不在白名单，拒绝出站：{path}")


def _safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


class HisWriter:
    def __init__(
        self,
        client,
        registry=None,
        *,
        conn=None,
        max_retries=2,
        base_delay=0.0,
    ):
        self.client = client
        self.registry = registry
        self.conn = conn
        self.max_retries = int(max_retries)
        self.base_delay = float(base_delay)

    def write_execution_site(
        self,
        *,
        order_id,
        inpatient_no,
        order_no,
        execute_dept_code,
        execute_dept_name,
        effective_from,
        transfer_no,
        reason="TRANSFER_CASCADE",
    ) -> dict:
        key = idempotency_key(
            transfer_no, order_no, execute_dept_code, effective_from
        )
        body = {
            "inpatient_no": inpatient_no,
            "order_no": order_no,
            "execute_dept_code": execute_dept_code,
            "execute_dept_name": execute_dept_name,
            "effective_from": effective_from,
            "reason": reason,
            "transfer_no": transfer_no,
        }
        path = f"/api/v1/orders/{_path(order_id)}/execution-site"
        headers = {"Idempotency-Key": key}
        return self._write(
            path, body, key, headers, transfer_no=transfer_no, order_no=order_no
        )

    def create_stop_draft(
        self, *, inpatient_no, order_no, content, source_transfer_no
    ) -> dict:
        body = {
            "inpatient_no": inpatient_no,
            "order_no": order_no,
            "draft_type": "STOP",
            "status": "DRAFT",
            "content": content,
            "source_transfer_no": source_transfer_no,
        }
        # 稳定去重键（只用于 registry 抑制重复草稿，不进请求头）。
        key = "stop_draft:" + hashlib.sha256(
            f"{source_transfer_no}|{order_no}".encode("utf-8")
        ).hexdigest()
        return self._write(
            _DRAFTS_PATH,
            body,
            key,
            None,
            transfer_no=source_transfer_no,
            order_no=order_no,
        )

    def _write(self, path, body, key, headers, *, transfer_no, order_no) -> dict:
        # 出网前字段边界断言：请求体不得含姓名 / 身份证 / 联系方式。
        assert_no_phi(body)
        # 写路径白名单：代码层只允许两条出站写路径。
        assert_writable(path)

        # 写前查 registry：命中已成功记录则复用结果，不再发请求。
        existing = self._check_registry(key)
        if existing is not None:
            return {
                "ok": True,
                "idempotent_hit": True,
                "idempotency_key": key,
                "request_sent": False,
                "result": existing,
            }

        def do_post():
            return self.client.post(path, json=body, headers=headers)

        def on_retry(attempt, exc):
            log.warning(
                "HIS 写回重试 attempt=%s key=%s reason=%s", attempt, key, exc
            )
            if self.conn is not None:
                try:
                    append_audit(
                        self.conn,
                        run_id="",
                        node_id="his_writer",
                        event_type="writeback_retry",
                        payload={
                            "attempt": attempt,
                            "idempotency_key": key,
                            "reason": str(exc)[:200],
                        },
                    )
                except Exception:  # 审计是旁路，写入失败不阻断写回重试
                    log.exception("写回重试审计事件写入失败")

        try:
            resp = errors.retry_with_backoff(
                do_post,
                max_retries=self.max_retries,
                base_delay=self.base_delay,
                on_retry=on_retry,
            )
        except errors.NonRetryableHisError:
            # 4xx / 越权 / 非法路径：直接失败并给上层可读原因，不重试、不入队。
            raise
        except (errors.RetryableHisError, errors.NetworkHisError) as exc:
            # 超次仍失败：登记待重写队列、给出提示，不静默丢弃该次写回。
            attempts = self.max_retries + 1
            reason = f"{type(exc).__name__}: {exc}"
            hint = (
                "写回失败，已登记待重写队列；请在托盘手动重试"
                "（重试仍走同一幂等键）"
            )
            log.warning("%s key=%s attempts=%s", hint, key, attempts)
            if self.conn is not None:
                errors.queue_pending_writeback(
                    self.conn,
                    idempotency_key=key,
                    attempts=attempts,
                    reason=reason,
                )
            return {
                "ok": False,
                "pending_writeback": True,
                "idempotency_key": key,
                "attempts": attempts,
                "reason": reason,
                "hint": hint,
            }

        # 成功：解析响应并登记 registry（供后续重复提交抑制）。
        status_code = resp.status_code
        result = _safe_json(resp)
        response_digest = hashlib.sha256(
            resp.text.encode("utf-8")
        ).hexdigest()[:16]
        self._record_registry(
            key,
            transfer_no=transfer_no,
            order_no=order_no,
            status_code=status_code,
            response_digest=response_digest,
            disposition_id=None,
        )
        return {
            "ok": True,
            "idempotent_hit": False,
            "idempotency_key": key,
            "status_code": status_code,
            "request_sent": True,
            "result": result,
        }

    def _check_registry(self, key):
        reg = self.registry
        if reg is None or not hasattr(reg, "check"):
            return None
        return reg.check(key)

    def _record_registry(self, key, **kwargs):
        reg = self.registry
        if reg is None or not hasattr(reg, "record"):
            return
        reg.record(key, **kwargs)
