"""`qed` command line.

qed dev                    partners (:9100) + Plexis MCP server (:8000) in one process
qed serve | qed partners   run either side alone
qed ledger verify|backup|restore <file>
qed token [--user]         print a development access token
qed bench [--n 60]         benchmark: Plexis vs baselines, pass rate + latency
qed load [--n 40 --c 8]    concurrent latency test against a running server
qed voice [--text]         voice-only device (microphone in, speech out) for a running server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _serve(app: object, host: str, port: int) -> object:
    import uvicorn

    return uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on"))


async def _dev(args: argparse.Namespace) -> None:
    from qed.config import load
    from qed.partners.servers import build_app
    from qed.server.app import create_app

    s = load()
    _quiet_windows_socket_resets()
    partners = _serve(build_app(s.host), s.host, s.partners_port)
    qed = _serve(create_app(s), s.host, s.port)
    print(f"Plexis  MCP endpoint   http://{s.host}:{s.port}/mcp   (protocol 2025-11-25, Streamable HTTP)")
    print(f"     health/metrics http://{s.host}:{s.port}/readyz  /metrics")
    print(
        f"Partners (7 MCP)    http://{s.host}:{s.partners_port}/<transport|stays|dining|experiences|weather|wallet|frontdesk>/mcp"
    )
    print(
        f"LLM: {'Amazon Bedrock (' + s.bedrock_parser_model + ')' if s.llm_enabled else 'off (deterministic compiler)'}"
    )
    await asyncio.gather(partners.serve(), _delayed(qed.serve(), 0.6))


def _quiet_windows_socket_resets() -> None:
    """Windows' proactor loop logs a traceback whenever a browser drops a connection; it is harmless."""
    loop = asyncio.get_running_loop()
    default = loop.get_exception_handler()

    def handler(lp: asyncio.AbstractEventLoop, ctx: dict[str, object]) -> None:
        if isinstance(ctx.get("exception"), ConnectionResetError):
            return
        if default is not None:
            default(lp, ctx)
        else:
            lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


async def _delayed(coro: object, delay: float) -> None:
    await asyncio.sleep(delay)
    await coro  # type: ignore[misc]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qed")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dev")
    sub.add_parser("serve")
    sub.add_parser("partners")
    led = sub.add_parser("ledger")
    led.add_argument("action", choices=["verify", "backup", "restore"])
    led.add_argument("file", nargs="?")
    tok = sub.add_parser("token")
    tok.add_argument("--user", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--n", type=int, default=60)
    b.add_argument("--out", default="eval/results")
    vc = sub.add_parser("voice", help="voice-only device: talk to Plexis through the microphone")
    vc.add_argument("--server", default=None, help="Plexis base URL (default from QED_PORT)")
    vc.add_argument("--text", action="store_true", help="type instead of talking")
    vc.add_argument("--mute", action="store_true", help="print replies instead of speaking them")
    vc.add_argument("--model", default="base.en", help="local Whisper model (tiny.en, base.en, small.en)")
    vc.add_argument(
        "--always-on", action="store_true", help="listen continuously instead of press-to-talk (quiet rooms)"
    )
    ld = sub.add_parser("load")
    ld.add_argument("--n", type=int, default=40)
    ld.add_argument("--c", type=int, default=8)
    args = p.parse_args(argv)

    if args.cmd == "dev":
        asyncio.run(_dev(args))
    elif args.cmd == "serve":
        from qed.config import load
        from qed.server.app import create_app

        s = load()
        asyncio.run(_serve(create_app(s), s.host, s.port).serve())  # type: ignore[attr-defined]
    elif args.cmd == "partners":
        from qed.config import load
        from qed.partners.servers import build_app

        s = load()
        asyncio.run(_serve(build_app(s.host), s.host, s.partners_port).serve())  # type: ignore[attr-defined]
    elif args.cmd == "ledger":
        from qed.config import load
        from qed.receipts.ledger import Ledger, Signer

        s = load()
        ledger = Ledger(s.state_dir, Signer(s.state_dir / "keys"), backup_every=0)
        if args.action == "verify":
            print(json.dumps(ledger.verify_chain(), indent=2))
        elif args.action == "backup":
            print(ledger.backup())
        else:
            if not args.file:
                print("usage: qed ledger restore <backup.sqlite>")
                return 2
            print(json.dumps(ledger.restore(Path(args.file)), indent=2))
    elif args.cmd == "token":
        import httpx

        from qed.config import load

        s = load()
        base = f"http://{s.host}:{s.port}"
        r = httpx.post(
            f"{base}/oauth/token",
            data={"grant_type": "client_credentials", "scope": "mcp:service"},
            auth=("alexa-plus-simulator", "dev-secret-change-me"),
        )
        print(r.json()["access_token"])
    elif args.cmd == "bench":
        from qed.eval.bench import run

        asyncio.run(run(args.n, Path(args.out)))
    elif args.cmd == "voice":
        from qed.config import load
        from qed.device.device import main as voice_main

        s = load()
        voice_main(
            args.server or f"http://{s.host}:{s.port}",
            text=args.text,
            mute=args.mute,
            model=args.model,
            always_on=args.always_on,
        )
    elif args.cmd == "load":
        from qed.eval.load import run as load_run

        asyncio.run(load_run(args.n, args.c))
    return 0


if __name__ == "__main__":
    sys.exit(main())
