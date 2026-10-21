HOST = "127.0.0.1"
PORT = 8123


def create_app():
    """Return the WSGI entry point used by the demo service."""
    return {"host": HOST, "port": PORT}


if __name__ == "__main__":
    settings = create_app()
    print(f"listening on {settings['host']}:{settings['port']}")
