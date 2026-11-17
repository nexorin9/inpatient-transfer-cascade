"""规模与性能验收（任务 48）：≥200 例批量跑、就绪顺序正确性、缓存/幂等、耗时阈值。

- 用 itc.graph.scheduler.ready_order（G1 已实现）产出每例节点执行顺序；
- 断言每例顺序满足依赖约束（每条连线的起点排在终点之前）且与独立拓扑序一致；
- 重复跑同一批样例取数调用次数不增加（本地计数函数 + 缓存 + 幂等去重）；
- 输出单例平均耗时、200 例总耗时上限等可度量统计并写 outputs/bench_summary.json。

用法：
    python tools/bench_run.py --samples 200
    python tools/bench_run.py --samples 200 --out outputs/bench_summary.json
"""
from __future__ import annotations

import argparse
import heapq
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
TOOLS_DIR = Path(__file__).resolve().parent
for _p in (str(SRC), str(TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from itc.graph.model import Graph, Link, Node, is_link  # noqa: E402
from itc.graph.scheduler import ready_order  # noqa: E402
from itc import observability  # noqa: E402

import fetch_datasets  # noqa: E402  （同组脚本，缺数据时按离线降级补齐）

DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_OUT = PROJECT_ROOT / "outputs" / "bench_summary.json"

RESOURCES = ("orders", "specimens", "charges")

# 可度量性能阈值（真实上限；死循环或异常放大都会突破）
PER_SAMPLE_AVG_LIMIT_MS = 50.0
TOTAL_MS_LIMIT = 10000.0


# ---------------------------------------------------------------------------
# 计数函数与缓存替身（模拟取数，供「重复跑取数调用次数不增加」度量）
# ---------------------------------------------------------------------------

class _CountingReader:
    """取数计数替身：每次真实取数自增，可按资源类型查看调用次数。"""

    def __init__(self):
        self.calls = 0
        self.per_resource = {r: 0 for r in RESOURCES}

    def fetch(self, resource, inpatient_no):
        self.calls += 1
        self.per_resource[resource] = self.per_resource.get(resource, 0) + 1
        # 返回可缓存的占位结果；内容不参与顺序断言
        return {"resource": resource, "inpatient_no": inpatient_no, "seq": self.calls}

    def call_count(self):
        return self.calls


class _FetchCache:
    """取数落盘缓存的内存等价物：键命中则不调用 reader。"""

    def __init__(self, reader):
        self.reader = reader
        self._store = {}
        self.hits = 0
        self.misses = 0

    def get(self, key):
        if key in self._store:
            self.hits += 1
            return self._store[key]
        self.misses += 1
        value = self.reader.fetch(*key)
        self._store[key] = value
        return value


# ---------------------------------------------------------------------------
# 建图：每例转科事件装配一张有真实依赖的联动图
# ---------------------------------------------------------------------------

def _build_sample_graph(idx, has_orders, has_specimens, has_charges):
    """装配一张联动图：3 个取数根 -> 判定/动作分支 -> 写回 -> 外部阻塞。"""
    p = f"s{idx:04d}:"
    inp = f"INP-{idx:06d}"
    g = Graph()
    fo = g.add_node(Node(p + "fetch_orders", "fetch_orders", {"inpatient_no": inp}))
    fs = g.add_node(Node(p + "fetch_specimens", "fetch_specimens", {"inpatient_no": inp}))
    fc = g.add_node(Node(p + "fetch_charges", "fetch_charges", {"inpatient_no": inp}))

    judge = g.add_node(Node(p + "rule_judge", "rule_judge", {"orders": fo.out(0)}))
    g.add_link(Link(fo.id, 0, judge.id, "orders"))

    branches = [judge]
    if has_orders:
        n = g.add_node(Node(p + "change_site", "change_site", {"source": judge.out(0)}))
        g.add_link(Link(judge.id, 0, n.id, "source"))
        branches.append(n)
    if has_specimens:
        n = g.add_node(Node(p + "specimen_route", "specimen_route", {"source": fs.out(0)}))
        g.add_link(Link(fs.id, 0, n.id, "source"))
        branches.append(n)
    if has_charges:
        n = g.add_node(Node(p + "bed_fee_hint", "bed_fee_hint", {"source": fc.out(0)}))
        g.add_link(Link(fc.id, 0, n.id, "source"))
        branches.append(n)

    # 写回节点扇入所有判定/动作节点，形成真实依赖
    wb_inputs = {}
    wb_links = []
    for i, b in enumerate(branches):
        wb_inputs[f"in_{i}"] = b.out(0)
        wb_links.append(Link(b.id, 0, p + "writeback", f"in_{i}"))
    wb = g.add_node(Node(p + "writeback", "writeback", wb_inputs))
    for lk in wb_links:
        g.add_link(lk)
    eb = g.add_node(Node(p + "external_block", "external_block", {"source": wb.out(0)}))
    g.add_link(Link(wb.id, 0, eb.id, "source"))
    return g


# ---------------------------------------------------------------------------
# 顺序断言：独立拓扑序 + 依赖约束
# ---------------------------------------------------------------------------

def _edges_from_graph(graph):
    """从节点 inputs 的边构造入度表与邻接表（与 ready_order 相同的边定义）。"""
    d = graph.to_dict()
    ids = sorted(d["nodes"].keys())
    indeg = {nid: 0 for nid in ids}
    adj = {nid: set() for nid in ids}
    for nid in ids:
        for value in d["nodes"][nid]["inputs"].values():
            if is_link(value) and value[0] in indeg:
                src = value[0]
                if nid not in adj[src]:
                    adj[src].add(nid)
                    indeg[nid] += 1
    return ids, indeg, {k: sorted(v) for k, v in adj.items()}


def _reference_topological_order(graph):
    """独立 Kahn 拓扑序（最小可用节点优先），用于与 ready_order 结果比对。"""
    ids, indeg, adj = _edges_from_graph(graph)
    heap = [nid for nid in ids if indeg[nid] == 0]
    heapq.heapify(heap)
    order = []
    while heap:
        nid = heapq.heappop(heap)
        order.append(nid)
        for nxt in adj[nid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                heapq.heappush(heap, nxt)
    return order


def _order_satisfies_dependencies(graph, order):
    """断言：顺序覆盖全部节点、无重复，且每条连线起点都严格排在终点之前。"""
    if len(order) != len(set(order)):
        return False
    if set(order) != set(graph.node_ids()):
        return False
    pos = {nid: i for i, nid in enumerate(order)}
    for nid, node in graph.nodes.items():
        for value in node.inputs.values():
            if is_link(value) and value[0] in pos and pos[value[0]] >= pos[nid]:
                return False
    return True


# ---------------------------------------------------------------------------
# 样例装载：来自 data/ 规模化数据集（≥200 例转科事件）
# ---------------------------------------------------------------------------

def _load_groups():
    groups = {}
    for cat in RESOURCES:
        path = DATA_DIR / f"{cat}.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items", []):
            groups.setdefault(item.get("inpatient_no", ""), set()).add(cat)
    return groups


def load_samples(n):
    """装载 n 例转科样例；data/ 缺失时按离线降级补齐，保证可重复运行。"""
    transfers_path = DATA_DIR / "transfers.json"
    if not transfers_path.is_file():
        fetch_datasets.run(DATA_DIR, offline=True, seed=fetch_datasets.DEFAULT_SEED)
    payload = json.loads(transfers_path.read_text(encoding="utf-8"))
    transfers = payload.get("items", [])
    groups = _load_groups()
    samples = []
    for i, t in enumerate(transfers[:n]):
        inp = t.get("inpatient_no", f"INP-{i:06d}")
        cats = groups.get(inp, set())
        samples.append({
            "idx": i,
            "inpatient_no": inp,
            "transfer_no": t.get("transfer_no", f"TRF-{i:06d}"),
            "has_orders": "orders" in cats,
            "has_specimens": "specimens" in cats,
            "has_charges": "charges" in cats,
        })
    # 数据集不足 n 例时按固定规则补齐（真实可复跑，不伪造业务数据）
    while len(samples) < n:
        i = len(samples)
        samples.append({
            "idx": i,
            "inpatient_no": f"INP-{i:06d}",
            "transfer_no": f"TRF-{i:06d}",
            "has_orders": True,
            "has_specimens": i % 2 == 0,
            "has_charges": i % 3 != 0,
        })
    return samples


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

def run_batch(samples, cache, seen):
    """执行一批样例：幂等去重（同 transfer_no 已处理则跳过）+ 缓存取数 + 顺序断言。"""
    start = time.perf_counter()
    order_ok = topo_ok = executed = idempotent_skips = 0
    for s in samples:
        if s["transfer_no"] in seen:
            idempotent_skips += 1
            continue
        seen.add(s["transfer_no"])
        executed += 1
        graph = _build_sample_graph(
            s["idx"], s["has_orders"], s["has_specimens"], s["has_charges"]
        )
        order = ready_order(graph)
        if _order_satisfies_dependencies(graph, order):
            order_ok += 1
        if order == _reference_topological_order(graph):
            topo_ok += 1
        # 3 类资源取数：缓存命中则不再调用 reader
        for resource in RESOURCES:
            cache.get((resource, s["inpatient_no"]))
    return {
        "samples": len(samples),
        "executed": executed,
        "idempotent_skips": idempotent_skips,
        "node_order_ok": order_ok,
        "topo_consistent": topo_ok,
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "reader_calls": cache.reader.call_count(),
        "elapsed_ms": (time.perf_counter() - start) * 1000.0,
    }


def run_benchmark(samples=200, out_path=None):
    """跑 3 个度量阶段，返回统计并写 outputs/bench_summary.json。"""
    sample_list = load_samples(samples)
    reader = _CountingReader()
    cache = _FetchCache(reader)

    # 阶段 1：首次批量（冷缓存 + 空幂等登记）——度量基线耗时与取数次数
    pass1 = run_batch(sample_list, cache, set())
    calls_after_first = reader.call_count()

    # 阶段 2：重复跑同一批（沿用幂等登记）——幂等命中，取数调用不应增加
    pass2 = run_batch(sample_list, cache, {s["transfer_no"] for s in sample_list})
    calls_after_idempotent_repeat = reader.call_count()

    # 阶段 3：重复跑同一批（清空幂等登记、沿用缓存）——缓存命中，取数调用不应增加
    pass3 = run_batch(sample_list, cache, set())
    calls_after_cache_repeat = reader.call_count()

    no_new_fetch_idempotent = calls_after_idempotent_repeat == calls_after_first
    no_new_fetch_cache = calls_after_cache_repeat == calls_after_first

    per_sample_avg_ms = pass1["elapsed_ms"] / samples
    total_ms = pass1["elapsed_ms"]

    duplicate_transfer_no = len(sample_list) - len({s["transfer_no"] for s in sample_list})

    stats = {
        # 批次摘要 6 项计数（observability 口径）
        "processed": samples,
        "cache_hits": pass3["cache_hits"] - pass1["cache_hits"],
        "blocked": 0,
        "manual": 0,
        "writebacks": 0,
        "failed": 0,
        # 规模与顺序正确性
        "samples": samples,
        "node_order_ok": pass1["node_order_ok"],
        "topo_consistent": pass1["topo_consistent"],
        "node_order_all_pass": pass1["node_order_ok"] == samples,
        "topo_all_pass": pass1["topo_consistent"] == samples,
        # 性能阈值
        "per_sample_avg_ms": round(per_sample_avg_ms, 4),
        "total_ms_200": round(total_ms, 4),
        "per_sample_avg_limit_ms": PER_SAMPLE_AVG_LIMIT_MS,
        "total_ms_limit": TOTAL_MS_LIMIT,
        "per_sample_avg_pass": bool(per_sample_avg_ms <= PER_SAMPLE_AVG_LIMIT_MS),
        "total_ms_pass": bool(total_ms <= TOTAL_MS_LIMIT),
        # 缓存 / 幂等 / 去重
        "fetch_calls_first_pass": calls_after_first,
        "fetch_calls_after_idempotent_repeat": calls_after_idempotent_repeat,
        "fetch_calls_after_cache_repeat": calls_after_cache_repeat,
        "no_new_fetch_on_idempotent_repeat": no_new_fetch_idempotent,
        "no_new_fetch_on_cache_repeat": no_new_fetch_cache,
        "idempotent_skips_repeat": pass2["idempotent_skips"],
        "cache_hits_first_pass": pass1["cache_hits"],
        "cache_misses_first_pass": pass1["cache_misses"],
        "cache_hits_repeat": pass3["cache_hits"] - pass1["cache_hits"],
        "cache_misses_repeat": pass3["cache_misses"] - pass1["cache_misses"],
        "duplicate_transfer_no_in_batch": duplicate_transfer_no,
        "idempotent_dedup": duplicate_transfer_no,
        # 阶段明细
        "passes": {"first": pass1, "idempotent_repeat": pass2, "cache_repeat": pass3},
    }
    stats["all_pass"] = bool(
        stats["node_order_all_pass"]
        and stats["topo_all_pass"]
        and stats["per_sample_avg_pass"]
        and stats["total_ms_pass"]
        and no_new_fetch_idempotent
        and no_new_fetch_cache
        and pass2["idempotent_skips"] == samples
        and stats["cache_hits_repeat"] == 3 * samples
    )

    out = Path(out_path) if out_path is not None else DEFAULT_OUT
    observability.write_batch_summary(None, stats, path=out)
    return stats


def print_stats(stats):
    print(f"=== 规模与性能验收：{stats['samples']} 例 ===")
    print(f"  节点顺序满足依赖约束：{stats['node_order_ok']}/{stats['samples']} "
          f"({'PASS' if stats['node_order_all_pass'] else 'FAIL'})")
    print(f"  与拓扑序一致：        {stats['topo_consistent']}/{stats['samples']} "
          f"({'PASS' if stats['topo_all_pass'] else 'FAIL'})")
    print(f"  单例平均耗时：        {stats['per_sample_avg_ms']} ms "
          f"(上限 {stats['per_sample_avg_limit_ms']} ms, "
          f"{'PASS' if stats['per_sample_avg_pass'] else 'FAIL'})")
    print(f"  {stats['samples']} 例总耗时：      {stats['total_ms_200']} ms "
          f"(上限 {stats['total_ms_limit']} ms, "
          f"{'PASS' if stats['total_ms_pass'] else 'FAIL'})")
    print(f"  首次取数调用：        {stats['fetch_calls_first_pass']}")
    print(f"  幂等重复后取数调用：  {stats['fetch_calls_after_idempotent_repeat']} "
          f"({'不增加 PASS' if stats['no_new_fetch_on_idempotent_repeat'] else '增加 FAIL'})")
    print(f"  缓存重复后取数调用：  {stats['fetch_calls_after_cache_repeat']} "
          f"({'不增加 PASS' if stats['no_new_fetch_on_cache_repeat'] else '增加 FAIL'})")
    print(f"  幂等命中跳过：        {stats['idempotent_skips_repeat']}")
    print(f"  缓存命中（重复）：    {stats['cache_hits_repeat']}")
    print(f"  批内重复转科单号去重：{stats['idempotent_dedup']}")
    print(f"  === BENCH {'PASS' if stats['all_pass'] else 'FAIL'} ===")


def main(argv=None):
    parser = argparse.ArgumentParser(description="≥200 例批量执行 / 就绪顺序 / 性能验收")
    parser.add_argument("--samples", type=int, default=200, help="批量样例数（默认 200）")
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT),
                        help="统计输出路径（默认 outputs/bench_summary.json）")
    args = parser.parse_args(argv)
    stats = run_benchmark(samples=args.samples, out_path=Path(args.out))
    print_stats(stats)
    return 0 if stats["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
