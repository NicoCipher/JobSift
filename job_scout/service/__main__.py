"""Loopback-only optional service launcher; core CLI remains independent."""

from urllib.parse import urlsplit

import uvicorn

from job_scout.service.app import create_app, load_config


def main():
    config = load_config()
    app = create_app(config)
    uvicorn.run(
        app, host=config.bind_host, port=urlsplit(config.origin).port or 80, proxy_headers=False
    )


if __name__ == "__main__":
    main()
