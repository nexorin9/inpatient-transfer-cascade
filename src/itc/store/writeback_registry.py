"""写回幂等登记表：幂等键 -> 写回结果，抑制重复提交与跨进程重复请求。

保护分两层：
1) 进程内：模块级 RLock 包住「查表 -> 发请求 -> 登记」整个过程；
2) 跨进程：writeback_registry.idempotency_key 唯一约束（底座表主键），
   登记表落盘，新连接 / 新进程重新查表即可复用结果。
"""
from __future__ import annotations

import hashlib
import json
import threading

from itc.store.db import now_iso

# 进程内锁：同一进程里不同 conn / 不同实例也必须串行化同一键的写回。
_PROCESS_LOCK = threading.RLock()


def make_idempotency_key(transfer_no: str, order_no: str,
                         execute_dept_code: str, effective_from: str) -> str:
    """按契约调用 G1 的 idempotency_key（sha256，按 | 固定拼接）。

    G1 文件由 graph 组并行实现；尚未就绪时用同一口径兜底，保证本组可独立运行。
    键只取业务字段，与时间戳 / 重试次数无关。
    """
    try:
        from itc.graph.idempotency import idempotency_key
    except ImportError:
        raw = "|".join(str(x) for x in
                       (transfer_no, order_no, execute_dept_code, effective_from))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return idempotency_key(transfer_no, order_no, execute_dept_code, effective_from)


def is_success(row: dict | None) -> bool:
    """2xx 视为成功写回；失败记录允许重试，不抑制请求。"""
    if not row:
        return False
    try:
        code = int(row.get("status_code") or 0)
    except (TypeError, ValueError):
        return False
    return 200 <= code < 300


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_digest(value) -> str:
    """响应摘要：字符串直接落库，其它结构规范化后取 sha256。"""
    if isinstance(value, str):
        return value
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class WritebackRegistry:
    """写回结果登记表 + 待重写队列。"""

    make_key = staticmethod(make_idempotency_key)

    def __init__(self, conn):
        self.conn = conn

    def _get(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM writeback_registry WHERE idempotency_key=?", (key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def check(self, key: str) -> dict | None:
        """查表：返回该幂等键的写回结果（状态码 / 响应摘要 / 时间 / 处置项 id）。"""
        return self._get(key)

    def check_success(self, key: str) -> dict | None:
        """只返回已成功（2xx）的记录，用于写前跳过请求。"""
        row = self._get(key)
        return row if is_success(row) else None

    def record(self, key: str, *, transfer_no: str = "", order_no: str = "",
               status_code: int = 200, response_digest="", disposition_id=None) -> dict:
        """登记一次写回结果。已有成功结果时保持原记录不变（幂等）。"""
        with _PROCESS_LOCK:
            existing = self._get(key)
            if is_success(existing):
                return existing
            code = _as_int(status_code)
            digest = _as_digest(response_digest)
            # 唯一约束兜底：并发下后到者不产生第二条记录，只更新待重试结果。
            self.conn.execute(
                "INSERT INTO writeback_registry"
                "(idempotency_key,transfer_no,order_no,status_code,response_digest,disposition_id,created_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(idempotency_key) DO UPDATE SET"
                " transfer_no=excluded.transfer_no, order_no=excluded.order_no,"
                " status_code=excluded.status_code, response_digest=excluded.response_digest,"
                " disposition_id=excluded.disposition_id, created_at=excluded.created_at",
                (key, transfer_no, order_no, code, digest, disposition_id, now_iso()),
            )
            self.conn.commit()
            return self._get(key)

    def concurrent_write(self, key: str, fn, **meta) -> dict:
        """在进程内锁保护下执行一次写回。

        命中已成功记录则跳过 fn（不发请求）、复用结果；否则调用 fn 发请求并登记。
        fn 可自行调用 record，也可返回结果由本方法兜底登记（重试仍走同一键）。
        """
        with _PROCESS_LOCK:
            hit = self.check_success(key)
            if hit is not None:
                return {**hit, "reused": True}
            result = fn()
            row = self._get(key)
            if not is_success(row):
                payload = self._payload_from(result, meta)
                if payload is not None:
                    row = self.record(key, **payload)
            if row is None:
                row = {
                    "idempotency_key": key, "transfer_no": "", "order_no": "",
                    "status_code": 0, "response_digest": _as_digest(result),
                    "disposition_id": None, "created_at": now_iso(),
                }
            return {**row, "reused": False}

    @staticmethod
    def _payload_from(result, meta: dict) -> dict | None:
        data = dict(meta)
        if isinstance(result, dict):
            for k in ("transfer_no", "order_no", "disposition_id",
                      "response_digest", "status_code"):
                if k in result and k not in data:
                    data[k] = result[k]
            if "response_digest" not in data and "response" in result:
                data["response_digest"] = result["response"]
        data.setdefault("status_code", 200)
        if not is_success({"status_code": data["status_code"]}):
            return None  # 失败不登记成功结果，留待重试
        data.setdefault("transfer_no", "")
        data.setdefault("order_no", "")
        data.setdefault("disposition_id", None)
        data.setdefault("response_digest", "")
        return {k: data[k] for k in
                ("transfer_no", "order_no", "status_code", "response_digest", "disposition_id")}

    def queue_pending(self, idempotency_key=None, disposition_id=None, *,
                      attempts: int = 0, reason: str = "", key=None) -> int:
        """写回 5xx 超次后置入待重写队列；队列保留同一幂等键供重试复用。"""
        idem = idempotency_key or key
        with _PROCESS_LOCK:
            cur = self.conn.execute(
                "INSERT INTO pending_writebacks"
                "(disposition_id,idempotency_key,attempts,reason,created_at)"
                " VALUES (?,?,?,?,?)",
                (disposition_id, idem, int(attempts), reason, now_iso()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def pending(self) -> list[dict]:
        """未解决的待重写项（resolved_at IS NULL）。"""
        rows = self.conn.execute(
            "SELECT * FROM pending_writebacks WHERE resolved_at IS NULL ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_pending(self, pending_id: int) -> bool:
        """标记一项待重写已解决；返回是否确实更新了一行。"""
        with _PROCESS_LOCK:
            cur = self.conn.execute(
                "UPDATE pending_writebacks SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
                (now_iso(), pending_id),
            )
            self.conn.commit()
            return cur.rowcount > 0
