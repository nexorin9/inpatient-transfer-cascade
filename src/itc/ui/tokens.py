"""界面视觉地基：色板 / 8px 间距栅格 / 字体层级 / 状态语义 / 组件四态样式。

取值严格对齐 docs/concepts/visual-target.md §2.1 ~ §2.3。
样式集中在本文件：主窗口、联动图、处置清单、托盘、三态组件都从这里取色与样式表，
不引入任何前端框架、CDN 或 Web 视图，图标只用纯文本与几何图形。

本模块只依赖底座 domain.models 的处置状态集合，不接触 HIS / 模型服务，
也不提供任何医嘱提交与签名的调用路径。
"""
from __future__ import annotations

from string import Template

from itc.domain.models import STATES

# ---------------------------------------------------------------------------
# 1. 色板（§2.1）：每一个值都来自视觉目标卡，不允许自行扩充
# ---------------------------------------------------------------------------

COLORS: dict[str, str] = {
    "primary": "#1B6AC9",           # 主色：primary 按钮 / 选中态 / 进度中
    "primary_hover": "#15579F",     # 主色 hover
    "primary_pressed": "#124A87",   # 主色 pressed
    "success": "#16A34A",           # 已回写
    "success_bg": "#DCFCE7",
    "success_text": "#166534",
    "warning": "#D97706",           # 待确认
    "warning_bg": "#FEF3C7",
    "warning_text": "#92400E",
    "danger": "#DC2626",            # 失败 / 不可自动停嘱
    "danger_bg": "#FEE2E2",
    "danger_text": "#991B1B",
    "info": "#2563EB",              # 待人工判定
    "info_bg": "#DBEAFE",
    "info_text": "#1E40AF",
    "neutral_title": "#0F172A",     # 页面标题与区块标题
    "neutral_body": "#334155",      # 正文、表格正文
    "neutral_helper": "#64748B",    # 说明文字、状态摘要
    "neutral_disabled": "#94A3B8",  # disabled 文字与描边
    "neutral_border": "#E2E8F0",    # 分隔线
    "neutral_header": "#F1F5F9",    # 表头底 / 页面底
    "neutral_zebra": "#F8FAFC",     # 偶数行底
    "page_bg": "#F1F5F9",           # 内容区底
    "card_bg": "#FFFFFF",           # 卡片与表格底
}

# 约定集合：单测断言所有色值落在这个集合里，避免随手写一个不在色板中的颜色。
ALLOWED_COLORS = frozenset({
    "#1B6AC9", "#15579F", "#124A87",
    "#16A34A", "#DCFCE7", "#166534",
    "#D97706", "#FEF3C7", "#92400E",
    "#DC2626", "#FEE2E2", "#991B1B",
    "#2563EB", "#DBEAFE", "#1E40AF",
    "#0F172A", "#334155", "#64748B", "#94A3B8", "#E2E8F0",
    "#F1F5F9", "#F8FAFC", "#FFFFFF",
})

# 常用色值同时以模块级常量暴露，便于其它模块直接取用。
PRIMARY = COLORS["primary"]
PRIMARY_HOVER = COLORS["primary_hover"]
PRIMARY_PRESSED = COLORS["primary_pressed"]
SUCCESS = COLORS["success"]
WARNING = COLORS["warning"]
DANGER = COLORS["danger"]
INFO = COLORS["info"]
NEUTRAL_TITLE = COLORS["neutral_title"]

# ---------------------------------------------------------------------------
# 2. 间距 / 圆角 / 阴影 / 字体（§2.2 ~ §2.3）
# ---------------------------------------------------------------------------

SPACING = (4, 8, 12, 16, 24, 32)
ALLOWED_SPACING = (4, 8, 12, 16, 24, 32)

# 页面内边距 24 / 卡片内边距 16 / 单元格上下 8 左右 12 / 按钮 8x16。
# SPACING_ROLES 中的每个值都必须落在 8px 栅格集合内。
SPACING_ROLES = {
    "page_padding": 24,
    "card_padding": 16,
    "cell_vertical": 8,
    "cell_horizontal": 12,
    "button_vertical": 8,
    "button_horizontal": 16,
}

# 版面尺寸（非间距栅格，单独存放，避免与 8px 栅格集合混淆）。
LAYOUT = {"nav_width": 200, "row_height": 36, "empty_vertical": 48}

RADII = {"card": 6, "toast": 8, "modal": 12}

SHADOWS = {
    "card": "none",
    "overlay": "0 4px 16px rgba(15,23,42,0.10)",
}

FONT_FAMILY = '-apple-system, "PingFang SC", "Microsoft YaHei", "Source Han Sans SC", sans-serif'
MONO_FAMILY = '"SF Mono", Consolas, "Courier New", monospace'

# 四级字体层级：页面标题 20/600、区块标题 16/600、正文表格 14/400、辅助 12/400
FONTS: dict[str, dict[str, int]] = {
    "page_title": {"size": 20, "weight": 600},
    "section_title": {"size": 16, "weight": 600},
    "body": {"size": 14, "weight": 400},
    "helper": {"size": 12, "weight": 400},
}
FONT_LEVELS = ("page_title", "section_title", "body", "helper")

# ---------------------------------------------------------------------------
# 3. 五类状态色与文案（§1.2 / §1.3），以及处置状态的映射
# ---------------------------------------------------------------------------

STATUS_KEYS = ("awaiting", "written", "manual", "blocked", "failed")

STATUS_COLORS: dict[str, dict[str, str]] = {
    "awaiting": {
        "label": "待确认", "base": COLORS["warning"],
        "bg": COLORS["warning_bg"], "text": COLORS["warning_text"],
        "dot": COLORS["warning"],
    },
    "written": {
        "label": "已回写", "base": COLORS["success"],
        "bg": COLORS["success_bg"], "text": COLORS["success_text"],
        "dot": COLORS["success"],
    },
    "manual": {
        "label": "待人工判定", "base": COLORS["info"],
        "bg": COLORS["info_bg"], "text": COLORS["info_text"],
        "dot": COLORS["info"],
    },
    "blocked": {
        "label": "阻断", "base": COLORS["neutral_title"],
        "bg": COLORS["neutral_border"], "text": COLORS["neutral_title"],
        "dot": COLORS["neutral_title"],
    },
    "failed": {
        "label": "失败", "base": COLORS["danger"],
        "bg": COLORS["danger_bg"], "text": COLORS["danger_text"],
        "dot": COLORS["danger"],
    },
}

STATUS_LABELS = {key: STATUS_COLORS[key]["label"] for key in STATUS_KEYS}

# 处置 7 态（底座 STATES 共 8 个取值，含起始的 pending_fetch）与状态标签一一映射。
# 每个状态恰好归入一个语义类；"待人工判定" 是项级叠加态（规则歧义 / 模型降级 /
# UNDETERMINED），由 resolve_status 在状态之上再判定，不改变状态机本身。
STATE_LABELS: dict[str, str] = {
    "pending_fetch": "待取数",
    "scoped": "已圈定范围",
    "rule_judged": "规则已判定",
    "model_judged": "模型已判定",
    "awaiting_confirm": "待确认",
    "written_back": "已回写",
    "blocked": "阻断",
    "failed": "失败",
}

STATE_STATUS: dict[str, str] = {
    "pending_fetch": "awaiting",
    "scoped": "awaiting",
    "rule_judged": "awaiting",
    "model_judged": "awaiting",
    "awaiting_confirm": "awaiting",
    "written_back": "written",
    "blocked": "blocked",
    "failed": "failed",
}

# 每个语义类覆盖的状态集合，合起来恰好是全部处置状态（manual 为项级叠加）。
STATUS_STATES: dict[str, tuple[str, ...]] = {
    "awaiting": ("pending_fetch", "scoped", "rule_judged", "model_judged", "awaiting_confirm"),
    "written": ("written_back",),
    "manual": (),
    "blocked": ("blocked",),
    "failed": ("failed",),
}


def resolve_status(state: str, item: dict | None = None) -> str:
    """返回处置项应显示的语义类 key。

    state 决定主类；若条目带人工判定标记（degraded / manual_reason /
    UNDETERMINED 判定）且尚未落终态，则叠加为「待人工判定」。
    """
    base = STATE_STATUS.get(state)
    if base is None:
        return "awaiting"
    if base not in ("awaiting", "written", "blocked", "failed"):
        return base
    if base == "awaiting" and item:
        if bool(item.get("degraded")) or str(item.get("manual_reason") or "").strip():
            return "manual"
        try:
            conf = float(item.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if str(item.get("judgement") or "") == "UNDETERMINED" and conf == 0.0:
            return "manual"
    return base


def status_label(state: str, item: dict | None = None) -> str:
    """状态标签文案：终态用状态标签，待人工叠加态用语义类文案。"""
    key = resolve_status(state, item)
    if key == "manual":
        return STATUS_LABELS["manual"]
    return STATE_LABELS.get(state, STATUS_LABELS.get(key, "待确认"))


def status_qss(key: str) -> str:
    """状态标签样式：琥珀 / 绿 / 蓝 / 深灰 / 红五类底色与字色。"""
    spec = STATUS_COLORS.get(key, STATUS_COLORS["awaiting"])
    return (
        "QLabel {"
        f" background-color: {spec['bg']};"
        f" color: {spec['text']};"
        f" border: 1px solid {spec['base']};"
        " border-radius: 6px; padding: 4px 8px; font-size: 12px;"
        " }"
    )


# 联动图节点状态灯（§1.2）：灰=待执行 / 蓝=执行中 / 绿=已完成 / 橙=待人工 / 红=失败。
# 阻断与失败共用红色但绘制不同的标记（斜纹屏障 / 叉号），保证五类视觉可区分。
NODE_STATUS_COLORS: dict[str, str] = {
    "pending": COLORS["neutral_disabled"],   # 待执行 灰
    "running": COLORS["primary"],            # 执行中 蓝
    "success": COLORS["success"],            # 已完成 绿
    "cached": COLORS["success"],
    "manual": COLORS["warning"],             # 待人工 橙
    "blocked": COLORS["danger"],             # 阻断 红 + 屏障标记
    "failed": COLORS["danger"],              # 失败 红 + 叉号标记
    "rejected": COLORS["danger"],
    "cancelled": COLORS["neutral_disabled"],
}

NODE_STATUS_LABELS = {
    "pending": "待执行", "running": "执行中", "success": "已完成",
    "cached": "已完成", "manual": "待人工", "blocked": "阻断",
    "failed": "失败", "rejected": "失败", "cancelled": "待执行",
}


def node_status_color(status: str) -> str:
    """取节点状态灯颜色；未知状态按待执行处理。"""
    return NODE_STATUS_COLORS.get(str(status), NODE_STATUS_COLORS["pending"])


# ---------------------------------------------------------------------------
# 4. 基础组件四态样式表（hover / active / disabled / loading）
# ---------------------------------------------------------------------------
# Qt 样式表没有原生 loading 伪态，统一用动态属性 [loading="true"] 表达；
# active 用 :pressed 或 [active="true"] 表达。四种状态在三类组件中齐全。

COMPONENT_STATES = ("hover", "active", "disabled", "loading")

BUTTON_QSS = Template("""
QPushButton {
    background-color: $primary;
    color: $card_bg;
    border: 1px solid $primary;
    border-radius: 6px;
    padding: 8px 16px;
    font-size: 14px;
}
QPushButton:hover { background-color: $primary_hover; border-color: $primary_hover; }
QPushButton:pressed, QPushButton[active="true"] { background-color: $primary_pressed; border-color: $primary_pressed; }
QPushButton:disabled { background-color: $neutral_header; color: $neutral_disabled; border-color: $neutral_border; }
QPushButton[loading="true"] { background-color: $primary_hover; color: $neutral_helper; }
QPushButton[variant="secondary"] { background-color: $card_bg; color: $primary; border-color: $neutral_border; }
QPushButton[variant="secondary"]:hover { background-color: $neutral_header; }
QPushButton[variant="secondary"]:pressed, QPushButton[variant="secondary"][active="true"] { background-color: $info_bg; }
QPushButton[variant="secondary"]:disabled { color: $neutral_disabled; }
QPushButton[variant="secondary"][loading="true"] { color: $neutral_disabled; }
QPushButton[variant="danger"] { background-color: $danger; border-color: $danger; color: $card_bg; }
""").substitute(**COLORS)

INPUT_QSS = Template("""
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {
    background-color: $card_bg;
    color: $neutral_body;
    border: 1px solid $neutral_border;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 14px;
    selection-background-color: $info_bg;
}
QLineEdit:hover, QPlainTextEdit:hover, QComboBox:hover { border-color: $primary; }
QLineEdit:focus, QPlainTextEdit:focus, QLineEdit[active="true"] { border-color: $primary; }
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled { background-color: $neutral_header; color: $neutral_disabled; border-color: $neutral_border; }
QLineEdit[loading="true"], QPlainTextEdit[loading="true"] { color: $neutral_helper; border-color: $warning; }
QLineEdit[invalid="true"], QPlainTextEdit[invalid="true"] { border-color: $danger; }
""").substitute(**COLORS)

TABLE_QSS = Template("""
QTableView {
    background-color: $card_bg;
    color: $neutral_body;
    border: 1px solid $neutral_border;
    gridline-color: $neutral_border;
    font-size: 14px;
    alternate-background-color: $neutral_zebra;
}
QTableView:hover { border-color: $primary; }
QTableView[active="true"] { border-color: $primary; }
QTableView:disabled { color: $neutral_disabled; }
QTableView[loading="true"] { color: $neutral_helper; }
QHeaderView::section {
    background-color: $neutral_header;
    color: $neutral_title;
    border: none;
    border-bottom: 1px solid $neutral_border;
    padding: 8px 12px;
    font-size: 14px;
}
QTableView::item:hover { background-color: $info_bg; }
QTableView::item:selected, QTableView::item[active="true"] { background-color: $primary; color: $card_bg; }
QTableView::item:disabled { color: $neutral_disabled; }
""").substitute(**COLORS)

BASE_QSS = Template("""
QMainWindow, QWidget#contentArea, QWidget#workspace { background-color: $page_bg; }
QWidget#navPanel, QWidget#headerBar { background-color: $neutral_header; }
QWidget#cardPanel { background-color: $card_bg; border: 1px solid $neutral_border; border-radius: 6px; }
QLabel#pageTitle { color: $neutral_title; font-size: 20px; font-weight: 600; }
QLabel#sectionTitle { color: $neutral_title; font-size: 16px; font-weight: 600; }
QLabel#bodyText { color: $neutral_body; font-size: 14px; }
QLabel#helperText, QLabel#runSummary { color: $neutral_helper; font-size: 12px; }
QLabel#dangerText { color: $danger_text; font-size: 14px; }
QFrame#divider { color: $neutral_border; }
""").substitute(**COLORS)

COMPONENT_QSS = {"button": BUTTON_QSS, "input": INPUT_QSS, "table": TABLE_QSS}

# 停嘱草稿「灰底深字」与不可停品类红字标注（视觉目标卡 §1.3）。
DRAFT_LABEL_QSS = Template("""
QLabel {
    background-color: $neutral_header;
    color: $neutral_title;
    border: 1px solid $neutral_disabled;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
""").substitute(**COLORS)

NO_STOP_LABEL_QSS = Template("""
QLabel {
    background-color: $danger_bg;
    color: $danger_text;
    border: 1px solid $danger;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
}
""").substitute(**COLORS)

STOP_DRAFT_LABEL = "草稿·需医师复核"
NO_STOP_LABEL = "不可自动停嘱·需医师判断"

# 组件局部样式同样集中在此，界面文件只引用常量，不内联拼样式表。
STATE_TITLE_QSS = Template(
    "QLabel { color: $neutral_title; font-size: 16px; font-weight: 600; }"
).substitute(**COLORS)

STATE_DETAIL_QSS = Template(
    "QLabel { color: $neutral_helper; font-size: 12px; }"
).substitute(**COLORS)

TOAST_QSS = Template("""
QFrame#toast {
    background-color: $neutral_title;
    color: $card_bg;
    border-radius: 8px;
    padding: 8px 16px;
    font-size: 14px;
}
""").substitute(**COLORS)

GRAPH_VIEW_QSS = Template("""
QGraphicsView {
    background-color: $card_bg;
    border: 1px solid $neutral_border;
    border-radius: 6px;
}
""").substitute(**COLORS)

DANGER_TEXT_QSS = Template(
    "QLabel { color: $danger_text; font-size: 12px; }"
).substitute(**COLORS)

FULL_QSS = "\n".join((BASE_QSS, BUTTON_QSS, INPUT_QSS, TABLE_QSS))


def apply_theme(widget) -> str:
    """把集中定义的样式表套到窗口或其根部件上，返回实际设置的样式表。"""
    widget.setStyleSheet(FULL_QSS)
    return FULL_QSS


# ---------------------------------------------------------------------------
# 5. 汇总 TOKENS
# ---------------------------------------------------------------------------

TOKENS: dict = {
    "colors": COLORS,
    "allowed_colors": ALLOWED_COLORS,
    "spacing": SPACING,
    "allowed_spacing": ALLOWED_SPACING,
    "spacing_roles": SPACING_ROLES,
    "layout": LAYOUT,
    "radii": RADII,
    "shadows": SHADOWS,
    "fonts": FONTS,
    "font_levels": FONT_LEVELS,
    "font_family": FONT_FAMILY,
    "mono_family": MONO_FAMILY,
    "status_colors": STATUS_COLORS,
    "status_labels": STATUS_LABELS,
    "status_keys": STATUS_KEYS,
    "state_labels": STATE_LABELS,
    "state_status": STATE_STATUS,
    "status_states": STATUS_STATES,
    "states": tuple(STATES),
    "node_status_colors": NODE_STATUS_COLORS,
    "node_status_labels": NODE_STATUS_LABELS,
    "component_states": COMPONENT_STATES,
    "component_qss": COMPONENT_QSS,
    "button_qss": BUTTON_QSS,
    "input_qss": INPUT_QSS,
    "table_qss": TABLE_QSS,
    "draft_label_qss": DRAFT_LABEL_QSS,
    "no_stop_label_qss": NO_STOP_LABEL_QSS,
    "state_title_qss": STATE_TITLE_QSS,
    "state_detail_qss": STATE_DETAIL_QSS,
    "toast_qss": TOAST_QSS,
    "graph_view_qss": GRAPH_VIEW_QSS,
    "danger_text_qss": DANGER_TEXT_QSS,
    "stop_draft_label": STOP_DRAFT_LABEL,
    "no_stop_label": NO_STOP_LABEL,
    "full_qss": FULL_QSS,
    # 扁平别名：TOKENS["primary"] 与 TOKENS["colors"]["primary"] 等价
    **COLORS,
}
