"""Report rendering: the HTML builder and the browser assets it embeds.

An empty file with a reason. This directory worked perfectly well as a
namespace package, but coverage.py only walks into a directory that is an
importable package, so builder.py was measured when a test happened to import
it and silently absent from the denominator when none did. A module that
disappears from the measurement rather than reading as nought per cent is the
fail-open shape: coverage goes up because less is being counted.
"""
