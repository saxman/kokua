"""Built-in tool-packs.

A tool-pack module exposes a module-level ``TOOL_PACK`` (a :class:`mopai.plugins.ToolPack`)
registered in pyproject under the ``mopai.tools`` entry-point group. A third party publishes a
package that registers its own ``mopai.tools`` entry point and Mopai discovers it at runtime,
merging its tools into the agent.
"""
