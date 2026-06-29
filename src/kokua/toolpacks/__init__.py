"""Built-in tool-packs.

A tool-pack module exposes a module-level ``TOOL_PACK`` (a :class:`kokua.plugins.ToolPack`)
registered in pyproject under the ``kokua.tools`` entry-point group. A third party publishes a
package that registers its own ``kokua.tools`` entry point and Kokua discovers it at runtime,
merging its tools into the agent.
"""
