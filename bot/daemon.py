"""
守护进程入口

启动流程：
  1. 配置日志（文件 + 控制台）
  2. 初始化数据库（建表）
  3. 立即跑一次任务 A（赛程），让数据库有 fixtures 可供 B/C 选场
  4. 启动 BlockingScheduler，阻塞运行三档任务

后台保活：在服务器上用 tmux 或 systemd 运行本脚本（见 README）。
  python -m bot.daemon
"""

import sys
import logging
from logging.handlers import RotatingFileHandler

from . import db, scheduler


def setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 控制台
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(ch)

    # 文件（滚动，单文件 2MB，留 5 份，ARM 磁盘友好）
    fh = RotatingFileHandler("daemon.log", maxBytes=2_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fh)


def main() -> None:
    setup_logging()
    log = logging.getLogger("odds_bot")
    log.info("=" * 50)
    log.info("赔率轮询守护进程启动")

    db.init_db()
    log.info("数据库已就绪：%s", db.config.DB_PATH)

    # 启动即拉一次赛程，确保 B/C 有数据可用
    try:
        scheduler.task_a_update_fixtures()
    except Exception:
        log.exception("启动时拉取赛程失败，继续启动调度器")

    sched = scheduler.build_scheduler()
    log.info("调度器启动：任务A(每日%02d:%02d) / 任务B(每%dh) / 任务C(每%dmin)",
             scheduler.config.TASK_A_HOUR, scheduler.config.TASK_A_MINUTE,
             scheduler.config.TASK_B_HOURS, scheduler.config.TASK_C_MINUTES)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("守护进程停止")


if __name__ == "__main__":
    main()
