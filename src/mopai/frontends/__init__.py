"""Built-in front ends.

Each front-end module exposes a module-level ``FRONTEND`` (a :class:`mopai.plugins.FrontEnd`)
registered in pyproject under the ``mopai.frontends`` entry-point group. A third party adds a
front end (e.g. Telegram) the same way, from its own package.
"""
