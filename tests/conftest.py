"""测试夹具：所有用例都从干净存储和可推进时钟起步。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from psdline.app import Platform, build_platform  # noqa: E402
from psdline.clock import ManualClock  # noqa: E402
from psdline.config import default_config  # noqa: E402
from psdline.store import JsonStore, JsonlJournal  # noqa: E402
from psdline.workshop import ReminderPolicy, WorkshopService  # noqa: E402


@pytest.fixture()
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture()
def workshop(tmp_path, clock) -> WorkshopService:
    """已建好人员与备件目录的车间服务（5 人、电机 2、密封圈 10）。"""

    service = WorkshopService(
        JsonStore(str(tmp_path / "ws")),
        JsonlJournal(str(tmp_path / "ws"), "workshop", segment_limit=32),
        clock=clock,
        policy=ReminderPolicy(l2_after=4.0, l3_after=24.0),
    )
    service.register_person("sup-1", "sup-1", "王主管", "SUPERVISOR")
    service.register_person("sup-1", "tech-day", "李白班", "TECH", shift="DAY")
    service.register_person("sup-1", "tech-night", "赵夜班", "TECH", shift="NIGHT")
    service.register_person("sup-1", "store-1", "孙仓管", "STORE")
    service.register_person("sup-1", "disp-1", "周调度", "DISPATCH")
    service.register_part("store-1", "MOTOR-01", "门驱电机", 2, unit="台")
    service.register_part("store-1", "SEAL-08", "防水密封圈", 10)
    return service


@pytest.fixture()
def online(platform) -> Platform:
    """已到站、已开门、已确认间隙的平台。"""

    platform.start()
    platform.arrive("T01", "ST1", 0)
    platform.check_gap("T01")
    return platform
