"""One module per command; the CLI discovers every public module in this package.

A command module defines:

    HELP: str                                    one-line summary for `srvr --help`
    configure(parser: ArgumentParser) -> None    declare arguments
    run(args: Namespace, ctx: Context) -> int    do the work; return the exit status

The command name is the module name with `_` replaced by `-`. Modules whose names start with
`_` are helpers, not commands. Raise `SrvrError` for expected failures.
"""
