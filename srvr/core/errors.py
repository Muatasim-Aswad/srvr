class SrvrError(Exception):
    """Expected failure with a user-facing message; the CLI prints it without a traceback."""
