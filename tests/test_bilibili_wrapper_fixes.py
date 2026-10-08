"""Real command-wrapper AST, with only network/library dependencies replaced."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class ClientError(Exception):
    pass


class ClientPayloadError(ClientError):
    pass


class ClientConnectionError(ClientError):
    pass


class ClientResponseError(ClientError):
    pass


class OtherError(Exception):
    pass


class BilibiliWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logs = []
        self.failure = None
        self.media = object()

        async def download(*args, **kwargs):
            if self.failure is not None:
                raise self.failure
            return self.media

        async def log(message, *args, **kwargs):
            self.logs.append(message)

        env = {
            "asyncio": asyncio,
            "audio_lib_main": NS(download_bilibili=download),
            "errors": NS(StorageFull=OtherError),
            "bilibili_api": NS(ResponseCodeException=OtherError, ArgsException=OtherError),
            "aiohttp": NS(ClientError=ClientError, ClientPayloadError=ClientPayloadError,
                          ClientConnectionError=ClientConnectionError, ClientResponseError=ClientResponseError),
            "httpx": NS(ConnectTimeout=OtherError, RemoteProtocolError=OtherError),
            "console": NS(rp=log), "utils": NS(PrintType=NS(ERROR=1)),
        }
        tree = ast.parse((SOURCE / "zeta_bot/core.py").read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "download_bilibili_audio")
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), function], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), "actual_download_bilibili_audio", "exec"), env)
        self.wrapper = env["download_bilibili_audio"]

    async def call(self):
        return await self.wrapper(NS(guild="offline"), {"bvid": "BVTEST", "title": "Offline test"}, "bilibili_single")

    async def test_payload_failure_returns_retryable_safe_result(self):
        self.failure = ClientPayloadError("RAW_PRIVATE_ERROR_SENTINEL")
        result = await self.call()
        self.assertIsNone(result["audio"])
        self.assertIs(result["exception"], self.failure)
        self.assertTrue(result["retryable"])
        self.assertNotIn("RAW_PRIVATE_ERROR_SENTINEL", result["message"] + "".join(self.logs))

    async def test_connection_timeout_and_generic_client_failures_return_results(self):
        for error in (ClientConnectionError, asyncio.TimeoutError, ClientError):
            with self.subTest(error=error.__name__):
                self.failure = error("RAW_PRIVATE_ERROR_SENTINEL")
                result = await self.call()
                self.assertIsNone(result["audio"])
                self.assertTrue(result["retryable"])
                self.assertNotIn("RAW_PRIVATE_ERROR_SENTINEL", result["message"] + "".join(self.logs))

    async def test_specific_http_response_handling_is_preserved(self):
        self.failure = ClientResponseError("offline 403")
        result = await self.call()
        self.assertEqual(result["message"], "请求繁忙")
        self.assertTrue(result["retryable"])

    async def test_cancellation_is_not_swallowed_as_network_failure(self):
        self.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.call()

    async def test_success_returns_same_audio_without_releasing_callers_lease(self):
        result = await self.call()
        self.assertIs(result["audio"], self.media)
        self.assertIsNone(result["exception"])
        self.assertFalse(result["retryable"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
