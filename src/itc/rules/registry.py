"""规则目录加载与版本机制（供所有规则共用同一套加载 / 开关 / 命中计数）。

规则目录 JSON 默认放 src/itc/rules/catalogs/rules.json，每条声明
id / version / enabled；另支持信息科按 rule_*.json 一规则一文件追加。
单个规则文件损坏时保留其余已加载规则，并把损坏项记入 failed。
每条规则判定产出的 RuleHit.rule_id 与这里的 id 对应，便于展示命中规则 id。
"""
from __future__ import annotations

import json
from pathlib import Path

from itc.config import get_settings

_DEFAULT_FILENAME = "rules.json"


class _Version(str):
    """规则目录版本：既可当字符串比较，也兼容 reg.version() 的调用写法。"""

    def __new__(cls, value, registry):
        obj = str.__new__(cls, value)
        obj._registry = registry
        return obj

    def __call__(self, rule_id=None):
        if rule_id is None:
            return str(self)
        return self._registry.version_of(rule_id)


class RuleRegistry:
    """规则目录：加载、版本、启用开关与命中计数。"""

    def __init__(self, rules_dir=None):
        self.rules_dir = Path(rules_dir) if rules_dir is not None else Path(get_settings().rules_dir)
        self._rules = []
        self._hits = {}
        self.failed = []
        self._version = "1.0"

    @property
    def version(self):
        """目录整体版本（字符串；也可 reg.version() 或 reg.version(rule_id)）。"""
        return _Version(self._version, self)

    def version_of(self, rule_id):
        """单条规则声明的 version；未找到返回空串。"""
        for rule in self._rules:
            if rule.get("id") == rule_id:
                return str(rule.get("version", ""))
        return ""

    def enabled(self, rule_id):
        """规则是否启用；未声明的规则保守判定为不启用。"""
        for rule in self._rules:
            if rule.get("id") == rule_id:
                return bool(rule.get("enabled", True))
        return False

    def hit(self, rule_id):
        """登记一次规则命中，返回累计次数；同步写回规则条目的 hit_count。"""
        self._hits[rule_id] = self._hits.get(rule_id, 0) + 1
        for rule in self._rules:
            if rule.get("id") == rule_id:
                rule["hit_count"] = self._hits[rule_id]
        return self._hits[rule_id]

    def hit_counts(self):
        """返回 rule_id -> 命中次数，覆盖全部已加载规则与曾命中的 id。"""
        ids = {rule.get("id") for rule in self._rules if rule.get("id")}
        ids.update(self._hits)
        return {rid: int(self._hits.get(rid, 0)) for rid in sorted(ids, key=str)}

    def load(self):
        """加载规则目录，返回已加载规则列表 list[dict]。

        候选文件 = rules.json（主目录，存在才读）+ 目录下 rule_*.json。
        单个文件损坏 / 非法只记入 self.failed，不影响其余文件继续加载。
        """
        self._rules = []
        self.failed = []
        self._version = "1.0"
        candidates = []
        main = self.rules_dir / _DEFAULT_FILENAME
        if main.is_file():
            candidates.append(main)
        if self.rules_dir.is_dir():
            candidates += sorted(self.rules_dir.glob("rule_*.json"))
        for p in candidates:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError) as exc:
                self.failed.append({"file": p.name, "error": str(exc)})
                continue
            entries = self._extract(data)
            if entries is None:
                self.failed.append({
                    "file": p.name,
                    "error": "规则文件内容非法：需为规则对象或含 rules 数组的对象",
                })
                continue
            if p.name == _DEFAULT_FILENAME and isinstance(data, dict) and data.get("version"):
                self._version = str(data["version"])
            for raw in entries:
                self._ingest(raw, p.name)
        return [dict(rule) for rule in self._rules]

    @staticmethod
    def _extract(data):
        """把文件内容规整为规则条目列表；无法识别返回 None。"""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if isinstance(data.get("rules"), list):
                return data["rules"]
            if "id" in data:
                return [data]
        return None

    def _ingest(self, raw, filename):
        """登记一条规则（同 id 保留首次），补齐 version / enabled / hit_count。"""
        if not isinstance(raw, dict) or not raw.get("id"):
            self.failed.append({"file": filename, "error": "单条规则缺少 id"})
            return
        rid = str(raw["id"])
        for existing in self._rules:
            if existing["id"] == rid:
                return
        rule = dict(raw)
        rule["id"] = rid
        rule.setdefault("version", self._version)
        rule.setdefault("enabled", True)
        rule["hit_count"] = int(self._hits.get(rid, 0))
        rule.setdefault("source_file", filename)
        self._rules.append(rule)
