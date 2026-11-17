"""模型调用审计元数据落库（model_audits 表）。

输入摘要哈希只对脱敏后的判定输入（住院号摘要 + 医嘱白名单字段）计算，审计行
只存哈希，不落住院号原文与医嘱全文。表由底座 db.py 建好，只增不改。
"""
from __future__ import annotations

import hashlib
import json

from itc.his.redact import assert_no_phi
from itc.store.db import init_schema, now_iso


def digest_input(payload: dict) -> str:
    """对脱敏后的判定输入做规范化 sha256；payload 必须先过边界断言。

    规范化保证同一份判定输入恒定得到同一摘要；哈希不可逆，审计行因此不含
    住院号原文与医嘱全文。
    """
    assert_no_phi(payload)
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ModelAuditStore:
    """模型调用审计：record 追加一行，rows 只读查询。"""

    def __init__(self, conn):
        init_schema(conn)  # 幂等，确保 model_audits 表存在
        self.conn = conn

    def record(
        self,
        *,
        model,
        latency_ms,
        input_digest,
        judgement,
        degraded=False,
        retries=0,
    ) -> int:
        """落一行审计：模型 / 耗时 / 输入摘要哈希 / 判定结果 / 降级 / 重试次数。"""
        # judgement 允许传完整判定 dict 或结果字符串，统一存判定值。
        judgement_value = (
            judgement["judgement"] if isinstance(judgement, dict) else str(judgement)
        )
        cur = self.conn.execute(
            "INSERT INTO model_audits"
            "(model,latency_ms,input_digest,judgement,degraded,retries,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                str(model),
                float(latency_ms),
                str(input_digest),
                judgement_value,
                int(bool(degraded)),
                int(retries),
                now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def rows(self) -> list[dict]:
        """按写入顺序返回全部审计行（只读）。"""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM model_audits ORDER BY id ASC"
            ).fetchall()
        ]
