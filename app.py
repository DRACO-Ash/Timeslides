"""Entry point.

The listen address and port both come from configuration, defaulting to every
interface on 8080, which is what the App Store runtime contract requires: it
sets containerPort 8080 and probes the pod's own address, so a container bound
only to loopback builds cleanly and then fails every probe. See
timeslides.config.DEFAULT_HOST for why the default is expressed as it is.

PORT is read with a default rather than set as an image ENV, per the same
contract.

One worker: render jobs are held in process and the group store is a file on a
single volume, so a second worker would split both. Concurrency comes from the
render thread pool, not from extra processes.
"""

from __future__ import annotations

from timeslides.api import create_app
from timeslides.config import load_settings

settings = load_settings()
app = create_app(settings=settings)


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port,
                workers=1, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
