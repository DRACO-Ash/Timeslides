"""Entry point.

Binds 0.0.0.0 so the platform can reach the container, and reads PORT with 8080
as the default rather than setting it, per the App Store runtime contract.
One worker: render jobs are held in process and the group store is a file on a
single volume, so a second worker would split both. Concurrency comes from the
render thread pool, not from extra processes.
"""

from __future__ import annotations

import os

from timeslides.api import create_app

app = create_app()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                workers=1, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
