"""桌面装配：QApplication + 主窗口 + 系统托盘常驻，启动时先恢复未完成状态。"""
from __future__ import annotations

import sys

from itc.config import get_settings
from itc.store.db import connect, init_schema
from itc.store.recovery import recover


def main(argv=None) -> int:
    from PySide6.QtWidgets import QApplication

    from itc.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(list(argv or sys.argv))
    app.setQuitOnLastWindowClosed(False)  # 关窗后托盘常驻

    conn = connect(get_settings().state_db_path)
    init_schema(conn)
    recovery = recover(conn)

    window = MainWindow(conn=conn, qapp=app)
    window.set_status_summary(
        "恢复摘要：" + str(recovery.get("summary_line", "无未完成项"))
    )
    tray = window.ensure_tray()
    tray.quitRequested.connect(app.quit)
    tray.show()
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
