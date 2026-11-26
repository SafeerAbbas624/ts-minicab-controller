"""Local WebSocket server the 'TS Minicab Bot Bridge' Chrome extension connects to.

Runs in a background thread; the bot calls Bridge.call(cmd, **args) synchronously.
Listens on 127.0.0.1 only. The extension and the bot prove to each other that they
know the token in chrome-extension/config.js before any command is accepted."""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path

from websockets.asyncio.server import serve

log = logging.getLogger(__name__)

EXT_DIR = Path(__file__).resolve().parents[1] / "chrome-extension"


class BridgeError(RuntimeError):
    pass


class BridgeDisconnected(BridgeError):
    pass


def read_ext_config() -> tuple[int, str]:
    if not (EXT_DIR / "config.js").exists():
        raise SystemExit("chrome-extension/config.js is missing (it is not stored in git). "
                         "Run: python run_bot.py --install-extension")
    text = (EXT_DIR / "config.js").read_text(encoding="utf-8")
    port = int(re.search(r"port:\s*(\d+)", text).group(1))
    token = re.search(r'token:\s*"([^"]*)"', text).group(1)
    return port, token


def install_token(port: int = 8765) -> str:
    """Write a fresh random token into the extension's config.js."""
    token = secrets.token_hex(24)
    (EXT_DIR / "config.js").write_text(
        "// Written by `python run_bot.py --install-extension`. Do not share this token.\n"
        f'const BRIDGE = {{ port: {port}, token: "{token}" }};\n', encoding="utf-8")
    return token


class Bridge:
    def __init__(self):
        self.port, self.token = read_ext_config()
        if self.token == "SET-BY-INSTALL":
            raise SystemExit("The Chrome extension has no token yet. Run: python run_bot.py --install-extension")
        self._loop = asyncio.new_event_loop()
        self._ws = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._connected = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bridge", daemon=True)
        self._thread.start()
        self._started.wait(10)

    # ------------------------------------------------------------ server thread
    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        async with serve(self._handler, "127.0.0.1", self.port, max_size=50 * 1024 * 1024,
                         ping_interval=20, ping_timeout=60):
            self._started.set()
            log.info("bridge listening on 127.0.0.1:%d", self.port)
            asyncio.get_running_loop().create_task(self._keepalive())
            await asyncio.Future()

    async def _keepalive(self):
        # An app-level message every 20 s keeps the extension's service worker awake.
        while True:
            await asyncio.sleep(20)
            if self._ws is not None:
                try:
                    await self._ws.send(json.dumps({"type": "keepalive"}))
                except Exception:
                    pass

    async def _handler(self, ws):
        origin = ws.request.headers.get("Origin", "") if ws.request else ""
        if not origin.startswith("chrome-extension://"):
            log.warning("bridge: refused connection from origin %r", origin)
            await ws.close(code=1008)
            return
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        except Exception:
            await ws.close(code=1008)
            return
        if hello.get("type") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), self.token):
            log.warning("bridge: wrong token from %s – refused", origin)
            await ws.close(code=1008)
            return
        proof = hashlib.sha256((self.token + str(hello.get("nonce", ""))).encode()).hexdigest()
        await ws.send(json.dumps({"type": "welcome", "proof": proof}))
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = ws
        self._connected.set()
        log.info("bridge: Chrome extension connected (%s)", origin)
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "result":
                    fut = self._pending.pop(msg.get("id"), None)
                    if fut and not fut.done():
                        fut.set_result(msg)
        except Exception:
            pass
        finally:
            if self._ws is ws:
                self._ws = None
                self._connected.clear()
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(BridgeDisconnected("extension disconnected"))
                self._pending.clear()
                log.warning("bridge: Chrome extension disconnected")

    async def _send(self, cmd: str, args: dict) -> asyncio.Future:
        if self._ws is None:
            raise BridgeDisconnected("extension not connected")
        i = next(self._ids)
        fut = self._loop.create_future()
        self._pending[i] = fut
        await self._ws.send(json.dumps({"type": "cmd", "id": i, "cmd": cmd, "args": args}))
        return fut

    # ------------------------------------------------------------ public
    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self._connected.wait(timeout)

    def call(self, cmd: str, timeout: float = 90, **args):
        if not self._connected.wait(30):
            raise BridgeDisconnected("Chrome extension is not connected (is Chrome open with the bridge extension on?)")

        async def go():
            fut = await self._send(cmd, args)
            return await asyncio.wait_for(fut, timeout)

        try:
            msg = asyncio.run_coroutine_threadsafe(go(), self._loop).result(timeout + 5)
        except (asyncio.TimeoutError, TimeoutError):
            raise BridgeError(f"{cmd}: no answer from Chrome in {timeout:.0f} s")
        if not msg.get("ok"):
            raise BridgeError(f"{cmd}: {msg.get('error')}")
        return msg.get("result")


def wait_for_extension(bridge: Bridge) -> None:
    if bridge.connected:
        return
    print("\nWaiting for Chrome: open Chrome with the profile that has the bridge extension.\n"
          "The 'TS Minicab Bot Bridge' extension connects by itself (badge 'on').\n"
          "Not installed yet? See README.md -> 'Use your own Chrome'.\n")
    t0 = time.time()
    while not bridge.wait_connected(15):
        log.info("still waiting for the Chrome extension (%.0f s)", time.time() - t0)
