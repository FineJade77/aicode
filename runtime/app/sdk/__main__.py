"""Run a Runtime as an stdio RPC server: `python -m app.sdk`.

Stdout is the protocol channel, so nothing else may write to it — logging goes
to stderr. A stray print would corrupt the stream in a way that reads to the
host as a parse error from the Runtime.
"""

import asyncio

from app.bootstrap import build_application_runtime
from app.config import settings
from app.sdk.server import serve_stdio


def main() -> None:
    asyncio.run(serve_stdio(build_application_runtime(settings)))


if __name__ == "__main__":
    main()
