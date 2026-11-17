"""全局 pytest 基座：源码路径、无头 Qt、公共 fixtures。

由项目底座统一维护；各模块测试只使用此处提供的 fixture，不修改本文件。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

# 必须在任何 PySide6 import 之前设定，保证无头环境可渲染
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 沙箱环境会拦截系统临时目录的 mkdir（pytest 的 tmp_path 因此报 FileExistsError）。
# 每个测试会话用一个唯一的工作区内临时根，避免复用已存在目录触发同样的误报。
_WORKSPACE_TMP = ROOT.parent / ".pytest_tmp" / f"session-{uuid.uuid4().hex[:8]}"
_WORKSPACE_TMP.mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(_WORKSPACE_TMP)
tempfile.tempdir = str(_WORKSPACE_TMP)

import pytest  # noqa: E402

from itc.store.db import connect, init_schema  # noqa: E402


@pytest.fixture(scope="session")
def project_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return ROOT / "fixtures"


@pytest.fixture
def sample_transfer(fixtures_dir):
    from itc.domain.models import TransferOrder

    return TransferOrder.from_dict(
        json.loads((fixtures_dir / "transfer_sample.json").read_text(encoding="utf-8"))
    )


@pytest.fixture
def sample_orders(fixtures_dir):
    from itc.domain.models import ExecutingOrder

    data = json.loads((fixtures_dir / "orders_sample.json").read_text(encoding="utf-8"))
    return [ExecutingOrder.from_dict(x) for x in data["items"]]


@pytest.fixture
def sample_specimens(fixtures_dir):
    from itc.domain.models import Specimen

    data = json.loads((fixtures_dir / "specimens_sample.json").read_text(encoding="utf-8"))
    return [Specimen.from_dict(x) for x in data["items"]]


@pytest.fixture
def sample_charges(fixtures_dir):
    from itc.domain.models import BedCharge

    data = json.loads((fixtures_dir / "charges_sample.json").read_text(encoding="utf-8"))
    return [BedCharge.from_dict(x) for x in data["items"]]


@pytest.fixture
def db_conn(tmp_path):
    conn = connect(str(tmp_path / "state.sqlite3"))
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def qapp():
    """单例 QApplication，供所有 UI 测试复用。"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
