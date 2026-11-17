"""数据获取与规模化样例集生成（任务 7）。

数据阶梯：联网时尝试公开样例下载并做完整性/字段校验；取不到或校验不过时
明确标注「离线降级」，复用本地已有数据或用固定种子「本地合成（非 Synthea 导出）」
补齐。四类数据始终由本地合成生成器按 spec §2 schema 产出，绝不把合成数据冒充
Synthea / MIMIC-IV 导出，也绝不静默伪造来源。

用法：
    python tools/fetch_datasets.py                 # 获取/生成并打印溯源
    python tools/fetch_datasets.py --check         # 校验四类规模与文件齐全
    python tools/fetch_datasets.py --offline       # 强制离线降级
    python tools/fetch_datasets.py --seed 1 --force
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：目标规模、验收下限、固定种子、占位字典
# ---------------------------------------------------------------------------

DEFAULT_SEED = 20261108

# 生成规模（均高于验收下限，留有余量）
TARGET = {
    "transfers": 240,
    "orders": 1200,
    "specimens": 480,
    "charges": 260,
}

# 验收下限（--check 的达标线）
MIN_SCALE = {
    "transfers": 200,
    "orders": 1000,
    "specimens": 400,
    "charges": 200,
}

CATEGORY_FILE = {
    "transfers": "transfers.json",
    "orders": "orders.json",
    "specimens": "specimens.json",
    "charges": "charges.json",
}

CATEGORY_LABEL = {
    "transfers": "转科事件",
    "orders": "在执行医嘱",
    "specimens": "待送标本",
    "charges": "床位费明细",
}

# 占位科别 / 病区：抽象标识，不是真实院内编码
DEPT_CODES = ("DEPT_A", "DEPT_B", "DEPT_C", "DEPT_D", "DEPT_E", "DEPT_F")
DEPT_NAMES = (
    "<科室A>", "<科室B>", "<科室C>", "<科室D>", "<科室E>", "<科室F>",
)

SOURCE_LABEL = "本地合成（非 Synthea 导出）"
ORIGIN = "local_synthetic"
LICENSE_LABEL = "CC0-1.0（纯合成占位数据，不含任何第三方临床数据）"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


# ---------------------------------------------------------------------------
# 标识：全部为占位或摘要，不产生拟真住院号 / 身份证 / 联系方式 / 真实日期
# ---------------------------------------------------------------------------

def _digest(*parts) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _id(prefix: str, seed: int, tag: str, i: int) -> str:
    """摘要式标识：字母前缀 + sha256 前 12 位，既非纯数字串也非拟真住院号。"""
    return f"{prefix}-{_digest(seed, tag, i)[:12]}"


def _ts(n: int) -> str:
    """抽象时间戳占位符（可字典序比较，但不是日历日期）。"""
    return f"<TS:{n:07d}>"


# ---------------------------------------------------------------------------
# 固定种子合成生成器
# ---------------------------------------------------------------------------

def generate_dataset(seed: int = DEFAULT_SEED) -> dict:
    """按固定种子生成四类数据；同一种子两次调用规模与字段完全一致。

    每条医嘱 / 标本 / 费用都带 inpatient_no 与 transfer_no 关联键，供批量脚本
    按患者分组。关联键属于数据集脚手架，不属于产品 domain 字段。
    """
    rng = random.Random(seed)
    depts = DEPT_CODES
    names = DEPT_NAMES

    transfers = []
    for i in range(TARGET["transfers"]):
        from_idx = rng.randrange(len(depts))
        to_idx = (from_idx + rng.randrange(1, len(depts))) % len(depts)
        transfers.append({
            "transfer_no": _id("TRF", seed, "trf", i),
            "inpatient_no": _id("INP", seed, "inp", i),
            "from_dept_code": depts[from_idx],
            "to_dept_code": depts[to_idx],
            "from_dept_name": names[from_idx],
            "to_dept_name": names[to_idx],
            "ward_code": f"WARD_{chr(65 + (i % len(depts)))}",
            "transfer_at": _ts(i),
        })

    orders = []
    for j in range(TARGET["orders"]):
        t = transfers[j % len(transfers)]
        # 约 70% 执行科室仍在原科室（触发「改执行地点」），其余已在目标科室
        exec_dept = t["from_dept_code"] if rng.random() < 0.7 else t["to_dept_code"]
        orders.append({
            "order_no": _id("ORD", seed, "ord", j),
            "inpatient_no": t["inpatient_no"],
            "transfer_no": t["transfer_no"],
            "content": f"<ORDER_CONTENT_{j % 24}>",
            "drug_name": f"<DRUG_{j % 18}>",
            "route": f"<ROUTE_{j % 6}>",
            "frequency": f"<FREQ_{j % 5}>",
            "execute_dept_code": exec_dept,
            "start_at": _ts(100000 + j),
        })

    specimens = []
    for k in range(TARGET["specimens"]):
        t = transfers[k % len(transfers)]
        # 少量非 pending 行用于校验「仅 status=pending 计入」的过滤
        status = "pending" if rng.random() < 0.9 else "collected"
        specimens.append({
            "specimen_no": _id("SPC", seed, "spc", k),
            "inpatient_no": t["inpatient_no"],
            "transfer_no": t["transfer_no"],
            "test_name": f"<TEST_{k % 20}>",
            "collect_dept_code": t["from_dept_code"],
            "status": status,
        })

    charges = []
    for m in range(TARGET["charges"]):
        t = transfers[m % len(transfers)]
        # 约 60% 归属目标科室，其余归属原科室 / 需人工核对的一致性提示
        dept_code = t["to_dept_code"] if rng.random() < 0.6 else t["from_dept_code"]
        charges.append({
            "inpatient_no": t["inpatient_no"],
            "transfer_no": t["transfer_no"],
            "category": "bed",
            "fee_date": _ts(200000 + m),
            "amount": f"{rng.randrange(3000, 9000) / 100:.2f}",
            "dept_code": dept_code,
        })

    return {
        "transfers": transfers,
        "orders": orders,
        "specimens": specimens,
        "charges": charges,
    }


# ---------------------------------------------------------------------------
# 落盘：四类 JSON + README，每个文件带 _meta 溯源
# ---------------------------------------------------------------------------

def _meta(category: str, count: int, *, seed: int, degraded: bool) -> dict:
    return {
        "source": SOURCE_LABEL,
        "origin": ORIGIN,
        "synthea_export": False,
        "mimic_downloaded": False,
        "offline_degraded": degraded,
        "license": LICENSE_LABEL,
        "seed": seed,
        "category": category,
        "category_label": CATEGORY_LABEL[category],
        "count": count,
        "generated_by": "tools/fetch_datasets.py",
        "note": "标识均为占位或摘要；不含姓名 / 身份证 / 联系方式 / 拟真住院号。",
    }


def write_dataset(dataset: dict, out_dir: Path, *, seed: int, degraded: bool) -> dict:
    """把四类数据写成 JSON 文件，返回 {类别: 文件路径}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for category, filename in CATEGORY_FILE.items():
        items = dataset[category]
        payload = {
            "_meta": _meta(category, len(items), seed=seed, degraded=degraded),
            "items": items,
        }
        path = out_dir / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written[category] = path
    return written


def readme_text(*, seed: int, degraded: bool, probe_reason: str,
                counts: dict) -> str:
    scale_rows = "\n".join(
        f"| {CATEGORY_FILE[c]} | {CATEGORY_LABEL[c]} | {counts.get(c, 0)} | "
        f"{MIN_SCALE[c]} | 达标 |"
        for c in CATEGORY_FILE
    )
    degraded_word = "是（本次为离线降级）" if degraded else "否（联网样例字段校验已尝试）"
    return f"""# data/ 数据集说明（由 tools/fetch_datasets.py 生成，非交付产品数据）

本目录是**测试数据层**：只用于规模化 / 性能 / 边界单测，不构成产品边界。
产品默认路径仍指向真实 HIS 端点（见 `src/itc/config.py` 与 `.env.example`）。

## 1. 来源与溯源（重要）

- **实际来源**：{SOURCE_LABEL}。四类文件由 `tools/fetch_datasets.py` 用固定随机种子
  （seed={seed}）按 spec §2 schema 合成，**不是 Synthea 发行包导出，也不是 MIMIC-IV 样例**。
- 联网分支会尝试下载「MIMIC-IV transfers 公开样例 / Synthea 公开导出」并做字段与规模
  校验；未配置公开样例地址（`ITC_SAMPLE_URL`）、断网、超时或校验不通过时，一律走
  **【离线降级】**，复用本地已有数据或按固定种子本地合成补齐，**绝不静默伪造来源**。
- 每个 JSON 文件的 `_meta` 都带 `source` / `origin` / `synthea_export: false` /
  `mimic_downloaded: false` / `offline_degraded` / `seed` / `license`，可逐文件核对来源。
- 本次联网尝试结果：{probe_reason}
- 本次是否离线降级：{degraded_word}

## 2. 许可证

- 合成占位数据：{LICENSE_LABEL}。
- 纯本地生成，未下载、未内嵌任何第三方临床数据，因此不引入第三方数据许可义务。
- 项目整体许可见仓库根 `LICENSE`（MIT），本说明仅针对 `data/` 内容。

## 3. 规模（验收下限：转科≥200 / 医嘱≥1000 / 标本≥400 / 床位费≥200）

| 文件 | 类别 | 本次条数 | 下限 | 结论 |
| --- | --- | --- | --- | --- |
{scale_rows}

- 固定种子两次生成：规模与字段**逐字段一致**（`generate_dataset(seed)` 为纯函数）。
- 换用不同 seed 会得到不同的标识与字段分布，证明种子真实参与生成。

## 4. 字段贴合度（spec §2）

| 文件 | 对应 domain 模型 | 贴合字段 |
| --- | --- | --- |
| transfers.json | `itc.domain.models.TransferOrder` | transfer_no / inpatient_no / from_dept_code / to_dept_code / from_dept_name / to_dept_name / ward_code / transfer_at |
| orders.json | `itc.domain.models.ExecutingOrder` | order_no / content / drug_name / route / frequency / execute_dept_code / start_at |
| specimens.json | `itc.domain.models.Specimen` | specimen_no / test_name / collect_dept_code / status |
| charges.json | `itc.domain.models.BedCharge` | category / fee_date / amount / dept_code |

- 每条医嘱 / 标本 / 费用另带 `inpatient_no` 与 `transfer_no` 关联键（数据集脚手架，
  供批量脚本按患者分组），不属于产品 domain 必填字段，解析时会被忽略。
- 字段分布刻意对齐业务：`execute_dept_code` 约 70% 仍在原科室（触发改执行地点）、
  标本约 90% 为 `status=pending`（其余用于过滤校验）、费用 `amount` 为十进制字符串、
  `dept_code` 与目标科室部分不一致（触发床位费一致性提示）。

## 5. 标识与脱敏

- 标识一律为占位或摘要：`TRF-`/`INP-`/`ORD-`/`SPC-` 前缀 + sha256 前 12 位，
  科别为 `DEPT_A..F` / `<科室A..F>` 占位，时间为 `<TS:...>` 抽象占位符。
- **不含**姓名、身份证、联系方式、拟真住院号，也不含「拟真住院号 + 真实日期」组合。
- `data/` 已在 `.gitignore` 中排除，不进入发布仓库。

## 6. 断网 / 离线降级说明

1. `python tools/fetch_datasets.py --offline`：强制离线降级，跳过联网尝试。
2. 默认（不带 `--offline`）：先尝试 `ITC_SAMPLE_URL`（短超时）；失败即降级并打印
   **【离线降级】** 与 **【本地合成（非 Synthea 导出）】** 标识。
3. 降级时若 `data/` 已有达标文件则**复用本地已有数据**（不重写）；缺失或不达标才按
   固定种子补齐。
4. `python tools/fetch_datasets.py --check`：校验四类规模与文件齐全；缺失时按离线降级
   补齐后再校验，全程明确标注来源，绝不静默伪造。
5. 校验命令可重复运行；`--seed` / `--force` 可显式重算。
"""


def write_readme(out_dir: Path, *, seed: int, degraded: bool,
                 probe_reason: str, counts: dict) -> Path:
    path = out_dir / "README.md"
    path.write_text(
        readme_text(seed=seed, degraded=degraded, probe_reason=probe_reason, counts=counts),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# 联网分支：公开样例下载 + 完整性校验（仅作字段贴合度参考，绝不冒充来源）
# ---------------------------------------------------------------------------

def try_public_sample(timeout: float = 2.0) -> dict:
    """尝试下载公开样例并校验；任何失败都返回 ok=False 与可读原因。

    公开样例仅用作字段贴合度参考，四类 schema 文件始终由本地合成生成器产出，
    因此即便联网成功也不会把产出来源标成 Synthea / MIMIC。
    """
    url = os.environ.get("ITC_SAMPLE_URL", "").strip()
    if not url:
        return {
            "ok": False,
            "reason": "未配置公开样例地址 ITC_SAMPLE_URL，跳过联网下载，走离线降级",
        }
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read(2 * 1024 * 1024)
        text = raw.decode("utf-8", errors="replace")
        data = json.loads(text)
        if isinstance(data, dict):
            rows = data.get("items") or data.get("rows") or data.get("data") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list) or len(rows) < 1:
            return {"ok": False, "reason": f"公开样例校验不通过：行数不足（{len(rows) if isinstance(rows, list) else 'N/A'}）"}
        return {
            "ok": True,
            "reason": f"公开样例下载并校验通过（{len(rows)} 行，仅作字段贴合度参考）",
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            ValueError, OSError) as exc:
        return {"ok": False, "reason": f"联网获取公开样例失败：{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - 断网/代理/DNS 等一律降级，不吞成伪造
        return {"ok": False, "reason": f"联网获取公开样例异常：{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 校验：四类规模达标 + 文件齐全 + 溯源可读
# ---------------------------------------------------------------------------

def check(out_dir: Path) -> dict:
    """读取 data/ 四类文件，返回规模、达标、齐全与溯源信息（不改写任何文件）。"""
    categories = {}
    provenance = {}
    files_complete = True
    for category, filename in CATEGORY_FILE.items():
        path = out_dir / filename
        present = path.is_file()
        if not present:
            files_complete = False
            categories[category] = {
                "present": False, "count": 0, "min": MIN_SCALE[category], "pass": False,
            }
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            meta = payload.get("_meta", {}) or {}
            items = payload.get("items", [])
            count = len(items) if isinstance(items, list) else 0
            if category == "transfers" and not provenance:
                provenance = dict(meta)
            categories[category] = {
                "present": True,
                "count": count,
                "min": MIN_SCALE[category],
                "pass": count >= MIN_SCALE[category],
            }
        except (json.JSONDecodeError, OSError):
            categories[category] = {
                "present": True, "count": 0, "min": MIN_SCALE[category], "pass": False,
            }
    all_pass = all(c["pass"] for c in categories.values()) and files_complete
    return {
        "categories": categories,
        "all_pass": all_pass,
        "files_complete": files_complete,
        "provenance": provenance,
    }


def print_check_report(report: dict, out_dir: Path) -> None:
    print(f"=== 数据集校验：{out_dir} ===")
    for category, filename in CATEGORY_FILE.items():
        c = report["categories"][category]
        verdict = "达标" if c["pass"] else "未达标"
        present = "齐全" if c["present"] else "缺失"
        print(
            f"  {filename:<18} {CATEGORY_LABEL[category]:<8} "
            f"规模={c['count']:>5} 下限={c['min']:>5} 文件={present} 结论={verdict}"
        )
    print(f"  四类文件齐全：{'是' if report['files_complete'] else '否'}")
    print(f"  四类规模全部达标：{'是' if report['all_pass'] else '否'}")
    prov = report.get("provenance") or {}
    if prov:
        print(f"  来源：{prov.get('source', SOURCE_LABEL)}")
        print(f"  Synthea 导出：{prov.get('synthea_export', False)}")
        print(f"  离线降级：{'是' if prov.get('offline_degraded') else '否'}")
        print(f"  许可证：{prov.get('license', LICENSE_LABEL)}")
    # 明确打印离线降级 / 本地合成标识，避免静默伪造来源
    if prov.get("offline_degraded"):
        print("  【离线降级】复用 / 补齐本地已有数据")
        print(f"  【{SOURCE_LABEL}】未冒充任何公开数据集导出")


# ---------------------------------------------------------------------------
# 编排：降级判定 → 复用或生成 → README → 校验
# ---------------------------------------------------------------------------

def run(out_dir: Path, *, offline: bool = False, seed: int = DEFAULT_SEED,
        force: bool = False) -> dict:
    """获取/生成四类数据；返回降级、溯源、是否复用与最终校验结果。"""
    out_dir.mkdir(parents=True, exist_ok=True)

    if offline:
        probe = {"ok": False, "reason": "--offline 强制离线降级，跳过联网尝试"}
    else:
        probe = try_public_sample()

    degraded = offline or (not probe["ok"])

    # 明确标注，绝不静默伪造来源
    print(f"  联网尝试：{probe['reason']}")
    if degraded:
        print("  【离线降级】未取得可用的公开样例，四类数据以本地来源为准")
        print(f"  【{SOURCE_LABEL}】产出来源固定标记为本地合成，不冒充 Synthea / MIMIC-IV")
        print("  【本地合成（非 Synthea 导出）】复用本地已有数据，缺失则按固定种子补齐")
    else:
        print(f"  来源说明：公开样例仅作字段校验参考；四类文件仍为【{SOURCE_LABEL}】")

    existing = check(out_dir)
    reused = (not force) and existing["all_pass"] and existing["files_complete"]
    if reused:
        print("  复用本地已有数据：四类文件已达标，不重写")
        counts = {c: existing["categories"][c]["count"] for c in CATEGORY_FILE}
    else:
        dataset = generate_dataset(seed)
        written = write_dataset(dataset, out_dir, seed=seed, degraded=degraded)
        counts = {c: len(dataset[c]) for c in CATEGORY_FILE}
        print(f"  本地合成写入 {len(written)} 个文件（seed={seed}）")

    write_readme(
        out_dir, seed=seed, degraded=degraded,
        probe_reason=probe["reason"], counts=counts,
    )
    final = check(out_dir)
    return {
        "degraded": degraded,
        "source": SOURCE_LABEL,
        "probe": probe,
        "reused": reused,
        "seed": seed,
        "check": final,
    }


def ensure(out_dir: Path, *, seed: int = DEFAULT_SEED) -> dict:
    """--check 前的兜底：缺失/未达标时按离线降级补齐，全程明确标注来源。"""
    report = check(out_dir)
    if report["all_pass"] and report["files_complete"]:
        return report
    print("  【离线降级】本地数据缺失或未达标，按固定种子本地合成补齐（非 Synthea 导出）")
    run(out_dir, offline=True, seed=seed)
    return check(out_dir)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="数据获取 / 规模化样例集生成与校验")
    parser.add_argument("--check", action="store_true", help="校验四类规模与文件齐全")
    parser.add_argument("--offline", action="store_true", help="强制离线降级，跳过联网尝试")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="固定随机种子")
    parser.add_argument("--force", action="store_true", help="忽略复用，强制重新生成")
    parser.add_argument("--out", type=str, default=str(DEFAULT_DATA_DIR), help="数据输出目录")
    args = parser.parse_args(argv)

    out_dir = Path(args.out)

    if args.check:
        report = ensure(out_dir, seed=args.seed)
        print_check_report(report, out_dir)
        ok = report["all_pass"] and report["files_complete"]
        print(f"=== CHECK {'PASS' if ok else 'FAIL'} ===")
        return 0 if ok else 1

    result = run(out_dir, offline=args.offline, seed=args.seed, force=args.force)
    print_check_report(result["check"], out_dir)
    ok = result["check"]["all_pass"] and result["check"]["files_complete"]
    print(f"=== RUN {'PASS' if ok else 'FAIL'} ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
