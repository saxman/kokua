"""Kokua's toolsets: one file per toolset, each file named for the toolset it declares.

Nothing else lives here. The registry machinery a toolset is built against (``Toolset``, ``Setting``,
``LiveState``, ``ToolsetContext``, and name resolution) is :mod:`kokua.registry`, and the code that
assembles every provider into one namespace is :mod:`kokua.core.agents`, so a reader looking for a
capability finds a file with its name and nothing else to sort through.

This module deliberately exports nothing. It exists because Python needs it for the directory to be a
package the wheel collects and ``import_module("kokua.toolsets.web")`` resolves.
"""
