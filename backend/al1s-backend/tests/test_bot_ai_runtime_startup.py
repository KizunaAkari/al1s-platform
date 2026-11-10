import asyncio
from types import SimpleNamespace

from al1s.meme_workshop.runtime import create_runtime_app


def test_local_ai_restores_before_qq_events_and_stops_after_worker():
    async def run():
        events = []
        class Repo:
            def recover(self, *, native_stopped): events.append(('recover', native_stopped))
        class Controller:
            engine = SimpleNamespace(local_runtime=True)
            def start(self): events.append('start')
            async def wait_initial(self): events.append('ready')
            async def close(self): events.append('close-controller')
        class Worker:
            async def run(self):
                events.append('worker-run')
                await asyncio.Event().wait()
            async def stop(self): events.append('worker-stop')
        class Runtime:
            local_runtime = True
            async def close(self): events.append('close-runtime')
        app = create_runtime_app(object(), object(), 'x' * 32, Worker(), role='qq',
                                 ai_repository=Repo(), ai_controller=Controller(), ai_hub=Runtime())
        def paths(routes):
            for route in routes:
                if getattr(route, 'path', None):
                    yield route.path
                nested = getattr(route, 'original_router', None)
                if nested:
                    yield from paths(nested.routes)
        assert not any('/internal/ai/bridge' in path for path in paths(app.routes))
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            assert events[:4] == [('recover', True), 'start', 'ready', 'worker-run']
        assert events.index('worker-stop') < events.index('close-controller') < events.index('close-runtime')
    asyncio.run(run())


def test_runtime_factory_uses_local_engine_and_legacy_library_data(tmp_path):
    async def run():
        from al1s.meme_workshop.runtime_main import make_local_ai
        from al1s.bot_ai.local_runtime import LocalAiRuntime
        worker = SimpleNamespace(send_ai_meme=lambda *_args: None, _ai_adapter=None)
        repo, controller, runtime = make_local_ai(worker, None, None, None, None,
            media_root=tmp_path / 'media', sticker_root=tmp_path / 'library', config_path=tmp_path / 'config')
        assert isinstance(runtime, LocalAiRuntime)
        assert worker._ai_adapter is controller.sessions and controller.engine.stickers.root == (tmp_path / 'library').resolve()
        assert (tmp_path / 'media').is_dir()
        await controller.close()
        await runtime.close()
    asyncio.run(run())
