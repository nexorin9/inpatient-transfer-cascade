"""任务 48 · 规模与性能：200 例就绪顺序、拓扑一致、缓存/幂等、耗时阈值。

用 project_root fixture；不 import H/M/U/S 组模块，不改 pytest.ini / conftest.py。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from itc.graph.scheduler import ready_order


def _load_bench(project_root: Path):
    path = project_root / "tools" / "bench_run.py"
    spec = importlib.util.spec_from_file_location("_d_bench_run", str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_d_bench_run"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bench(project_root):
    return _load_bench(project_root)


@pytest.fixture(scope="module")
def stats(bench, project_root):
    return bench.run_benchmark(
        samples=200, out_path=project_root / "outputs" / "bench_summary.json"
    )


class TestReadyOrderCorrectness:
    def test_ready_order_matches_independent_topo(self, bench):
        graph = bench._build_sample_graph(0, True, True, True)
        order = ready_order(graph)
        assert order == bench._reference_topological_order(graph)
        assert bench._order_satisfies_dependencies(graph, order) is True

    def test_every_link_predecessor_precedes_successor(self, bench):
        graph = bench._build_sample_graph(7, True, False, True)
        order = ready_order(graph)
        assert bench._order_satisfies_dependencies(graph, order) is True
        # 人为把起点放到末尾，依赖约束必须判定为 False（非恒真）
        broken = list(reversed(order))
        assert bench._order_satisfies_dependencies(graph, broken) is False


class TestScaleRunStats:
    def test_sample_count_and_order_assertions(self, bench, stats):
        assert stats["samples"] == 200
        assert stats["node_order_ok"] == 200
        assert stats["topo_consistent"] == 200
        assert stats["node_order_all_pass"] is True
        assert stats["topo_all_pass"] is True

    def test_no_new_fetch_on_repeat(self, bench, stats):
        assert stats["no_new_fetch_on_idempotent_repeat"] is True
        assert stats["no_new_fetch_on_cache_repeat"] is True
        assert stats["fetch_calls_after_idempotent_repeat"] == stats["fetch_calls_first_pass"]
        assert stats["fetch_calls_after_cache_repeat"] == stats["fetch_calls_first_pass"]
        assert stats["idempotent_skips_repeat"] == 200
        assert stats["cache_hits_repeat"] == 3 * 200
        assert stats["cache_misses_repeat"] == 0

    def test_performance_thresholds(self, bench, stats):
        assert stats["per_sample_avg_ms"] <= bench.PER_SAMPLE_AVG_LIMIT_MS
        assert stats["total_ms_200"] <= bench.TOTAL_MS_LIMIT
        assert stats["per_sample_avg_pass"] is True
        assert stats["total_ms_pass"] is True
        assert stats["all_pass"] is True


class TestSummaryArtifact:
    def test_bench_summary_written_with_batch_fields(self, bench, project_root, stats):
        path = project_root / "outputs" / "bench_summary.json"
        assert path.is_file()
        d = json.loads(path.read_text(encoding="utf-8"))
        # observability 批次摘要 6 项
        for key in ("processed", "cache_hits", "blocked", "manual", "writebacks", "failed"):
            assert key in d
        # 性能统计
        for key in ("samples", "per_sample_avg_ms", "total_ms_200",
                    "no_new_fetch_on_cache_repeat", "all_pass"):
            assert key in d
        assert d["processed"] == 200
        assert d["all_pass"] is True
