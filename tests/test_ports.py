"""输入端口：连线 / 常量区分、序列化两类标记、必填与类型校验。"""
from __future__ import annotations

import pytest

from itc.graph.ports import (
    Port,
    PortValidationError,
    is_constant_input,
    is_link_input,
    serialize_inputs,
    validate_ports,
)


class TestPortClassification:
    def test_link_and_constant(self):
        assert is_link_input(["n", 0]) is True
        assert is_link_input(["n", 0.5]) is True
        assert is_link_input("text") is False
        assert is_constant_input("text") is True
        assert is_constant_input(["n", "0"]) is True  # 非连线即常量
        assert is_constant_input(3) is True
        assert is_constant_input(["n", 0]) is False

    def test_serialize_inputs_marks_both_kinds(self):
        inputs = {
            "orders": ["a", 0],
            "inpatient_no": "I",
            "count": 2,
            "specimens": ["b", 1],
        }
        s = serialize_inputs(inputs)
        assert s["inputs"] == inputs
        assert s["link_ports"] == ["orders", "specimens"]
        assert s["constant_ports"] == ["count", "inpatient_no"]
        assert set(s["link_ports"]).isdisjoint(s["constant_ports"])
        assert set(s["link_ports"]) | set(s["constant_ports"]) == set(inputs)


class TestPort:
    def test_defaults_and_fields(self):
        p = Port("x", "link")
        assert p.name == "x"
        assert p.type == "link"
        assert p.required is True
        assert p.link is True
        assert p.as_dict()["name"] == "x"


class TestValidatePorts:
    def test_valid(self):
        assert validate_ports("fetch_orders", {"inpatient_no": "I"}) is True
        assert validate_ports("rule_judge", {"orders": ["a", 0], "extra": 1}) is True

    def test_missing_port_is_named(self):
        with pytest.raises(PortValidationError) as ei:
            validate_ports("fetch_orders", {})
        assert "inpatient_no" in str(ei.value)
        assert "缺少必填端口" in str(ei.value)

    def test_type_mismatch_is_named(self):
        with pytest.raises(PortValidationError) as ei:
            validate_ports("fetch_orders", {"inpatient_no": ["a", 0]})
        assert "inpatient_no" in str(ei.value)
        assert "类型不匹配" in str(ei.value)

    def test_link_port_requires_link(self):
        with pytest.raises(PortValidationError) as ei:
            validate_ports("rule_judge", {"orders": "not-a-link"})
        assert "orders" in str(ei.value)

    def test_unknown_type_fallback(self):
        assert validate_ports("brand_new_type", {}) is True
