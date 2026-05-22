from __future__ import annotations

import asyncio
import inspect


def pytest_pyfunc_call(pyfuncitem):
    testfunction = pyfuncitem.obj
    if not inspect.iscoroutinefunction(testfunction):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(testfunction(**kwargs))
    return True
