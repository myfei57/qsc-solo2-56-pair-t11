"""无 pytest 环境下的最小兼容垫片（仅供本仓库离线验证使用）。

生产与正常 CI 仍用真正的 pytest（见 requirements.txt / pyproject.toml）。这个垫片
只实现本仓库测试实际用到的 pytest 特性：``fixture``（支持夹具互相依赖）、
``parametrize``、``raises``。用法：

    PYTHONPATH=. python3 tools/run_tests.py tests/test_maintain.py
"""

from __future__ import annotations

import contextlib
import inspect
import tempfile
from pathlib import Path


class Failure(AssertionError):
    """断言失败。"""


class _Raises:
    def __init__(self, exc_type):
        self.exc_type = exc_type
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise Failure(f"期望抛出 {self.exc_type.__name__}，但没有抛出")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        return True


def raises(exc_type):
    return _Raises(exc_type)


class _Mark:
    @staticmethod
    def parametrize(argnames, argvalues):
        names = [n.strip() for n in argnames.split(",")]

        def decorator(func):
            cases = []
            for values in argvalues:
                if not isinstance(values, tuple):
                    values = (values,)
                assert len(values) == len(names)
                cases.append(dict(zip(names, values)))
            func.__param_cases__ = cases
            return func

        return decorator


mark = _Mark()


class Fixture:
    def __init__(self, func):
        self.func = func
        self.__wrapped__ = func


def fixture(func=None):
    if func is None:
        return fixture
    return Fixture(func)


@fixture
def tmp_path():
    path = Path(tempfile.mkdtemp(prefix="pytest-shim-"))
    yield path


def _resolve_names(names, fixtures, stack=None):
    """按夹具名递归解析，返回 (值映射, teardown 生成器列表)。"""

    stack = stack or set()
    resolved = {}
    teardowns = []
    for name in names:
        if name not in fixtures:
            raise Failure(f"未知 fixture: {name}")
        if name in stack:
            raise Failure(f"fixture 存在循环依赖: {name}")
        fx = fixtures[name]
        sub_names = list(inspect.signature(fx.func).parameters)
        sub, more = _resolve_names(sub_names, fixtures, stack | {name})
        teardowns.extend(more)
        result = fx.func(**sub)
        if inspect.isgenerator(result):
            resolved[name] = next(result)
            teardowns.append(result)
        else:
            resolved[name] = result
    return resolved, teardowns


def run_module(module) -> tuple[int, int, list[str]]:
    fixtures = {
        name: value for name, value in vars(module).items() if isinstance(value, Fixture)
    }
    passed = 0
    failures = []
    total = 0
    for name, func in sorted(vars(module).items()):
        if not name.startswith("test_") or not callable(func):
            continue
        cases = getattr(func, "__param_cases__", [{}])
        for index, case in enumerate(cases):
            total += 1
            label = name if len(cases) == 1 else f"{name}[{index}]"
            try:
                fixture_names = [
                    n for n in inspect.signature(func).parameters if n not in case
                ]
                kwargs, teardowns = _resolve_names(fixture_names, fixtures)
                try:
                    func(**case, **kwargs)
                finally:
                    for gen in teardowns:
                        with contextlib.suppress(StopIteration):
                            next(gen)
                passed += 1
            except Exception as error:  # noqa: BLE001 - 测试运行器兜住所有失败
                failures.append(f"{label}: {type(error).__name__}: {error}")
    return passed, total, failures
