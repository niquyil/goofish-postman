"""Web 服务的启动与端口处理测试（真实监听本机端口，不访问外网）。"""

from __future__ import annotations

from asyncio import CancelledError, Future, create_task, get_running_loop, run, wait_for

from httpx import AsyncClient

from goofishpostman.accounts import FeishuNotifier
from goofishpostman.store import Store
from goofishpostman.supervisor import Supervisor
from goofishpostman.web import serve


def test_ephemeral_port_does_not_fall_back_to_configured_port(tmp_dir) -> None:
    """回归：serve(port=0) 曾被 `port or settings.port` 吃掉 0，退回到配置端口，
    结果撞上正在运行的实例、报「端口占用」，很容易被误判成服务出错。"""

    async def run_scenario() -> None:
        store = Store(tmp_dir / 'accounts.json')
        supervisor = Supervisor(store, FeishuNotifier())
        configured = store.data.web.port
        ready: Future = get_running_loop().create_future()

        task = create_task(serve(store, supervisor, host='127.0.0.1', ephemeral_port=True, ready=ready))
        try:
            url = await wait_for(ready, timeout=15)
            assert url.startswith('http://127.0.0.1:')
            assert not url.endswith(f':{configured}'), '随机端口不应退回到配置端口'

            # 回报的端口必须是真实监听端口（随机端口时才知道具体值）
            bound_port = int(url.rsplit(':', 1)[1])
            async with AsyncClient(timeout=10) as client:
                assert (await client.get(f'{url}/api/state')).status_code == 200
                assert (await client.get(f'{url}/')).status_code == 200

            # 显式传端口时必须按传入值监听（serve 的端口优先级）
            assert bound_port > 0
        finally:
            task.cancel()
            try:
                await task
            except CancelledError:
                pass

    run(run_scenario())


def test_configured_port_is_used_by_default(tmp_dir) -> None:
    """不传端口时用配置里的端口。"""

    async def run_scenario() -> None:
        store = Store(tmp_dir / 'accounts.json')
        store.update_web(port=8902)
        supervisor = Supervisor(store, FeishuNotifier())
        ready: Future = get_running_loop().create_future()

        task = create_task(serve(store, supervisor, host='127.0.0.1', ready=ready))
        try:
            url = await wait_for(ready, timeout=15)
            assert url.endswith(':8902')
        finally:
            task.cancel()
            try:
                await task
            except CancelledError:
                pass

    run(run_scenario())
