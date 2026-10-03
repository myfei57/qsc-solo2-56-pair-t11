#!/usr/bin/env python3
"""离线测试运行器（零第三方依赖）。

把 tools/pytest_shim 注册成 ``pytest`` 模块，再加载 tests/conftest.py 里的夹具，
随后逐个运行给定测试文件。用法：

    PYTHONPATH=. python3 tools/run_tests.py tests/test_maintain.py
    PYTHONPATH=. python3 tools/run_tests.py            # 跑 tests/ 下全部
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import pytest_shim  # noqa: E402

sys.modules["pytest"] = pytest_shim


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(paths: list[str]) -> int:
    conftest = ROOT / "tests" / "conftest.py"
    base_fixtures: dict[str, pytest_shim.Fixture] = {}
    if conftest.is_file():
        conf = _load_module(conftest, "conftest")
        base_fixtures.update(
            (name, value) for name, value in vars(conf).items()
            if isinstance(value, pytest_shim.Fixture)
        )
    # shim 提供的 tmp_path 作为最弱兜底（conftest 没定义时用）
    base_fixtures.setdefault("tmp_path", pytest_shim.tmp_path)

    if not paths:
        paths = [str(p) for p in sorted((ROOT / "tests").glob("test_*.py"))]

    total_passed = 0
    total_cases = 0
    all_failures: list[str] = []
    for index, path_str in enumerate(paths):
        path = Path(path_str)
        if not path.is_absolute():
            path = ROOT / path
        module = _load_module(path, f"test_under_test_{index}")
        # 合并 conftest 夹具到测试模块（测试里引用同模块 fixture 优先）
        for name, value in base_fixtures.items():
            if not hasattr(module, name):
                setattr(module, name, value)
        passed, total, failures = pytest_shim.run_module(module)
        total_passed += passed
        total_cases += total
        all_failures.extend(failures)
        print(f"{path.name}: {passed}/{total} 通过")

    print("-" * 60)
    if all_failures:
        print(f"失败 {len(all_failures)} 个：")
        for failure in all_failures:
            print(f"  ✗ {failure}")
        return 1
    print(f"全部通过：{total_passed}/{total_cases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
