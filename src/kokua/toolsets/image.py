"""A built-in toolset that generates images from a text prompt.

Contributes one tool, ``generate_image``, that runs an AIMU ``ImageClient`` and saves the result into
``images_path`` (the same folder uploaded images live in, served by the web front end at
``/images/<name>``). The tool returns a message carrying that reference, so the web UI renders the image
inline (live via the IMAGE_GENERATING stream, and on reload via history replay).

Unlike AIMU's built-in ``image`` tool group, this saves into Kokua's servable ``images_path`` rather than
a folder inside the aimu package, and it defers building the client until the tool is first called: the
client reads the ``AIMU_IMAGE_MODEL`` env var and raises if it is unset, so building it at toolset-load time
would break startup for every user who has not configured an image model.

**AIMU's own ``image`` group is deliberately not registered anywhere in Kokua, and this toolset is why.**
Both contribute a tool named ``generate_image``, but AIMU's saves into a folder inside the aimu package,
which the web front end cannot serve. Registering both would not be a startup error, which is the trap:
two toolsets sharing a *tool* name are deduplicated first-wins by declared order, unlike two toolsets
sharing a *toolset* name, which `register` refuses. So an agent would silently get whichever came first,
which is the exact failure a single namespace exists to rule out. `tests/toolsets/test_image.py` pins the
omission.
"""

from __future__ import annotations

import os
from pathlib import Path

from aimu.tools import tool

from kokua.config import AssistantConfig
from kokua.registry import Toolset

# AIMU's image client resolves this env var for the model; without it, generation cannot work, so the
# tool is not offered at all (the model never sees an option it can't fulfill).
_IMAGE_MODEL_ENV = "AIMU_IMAGE_MODEL"


def build(config: AssistantConfig) -> list:
    """Return this toolset's tools when an image model is configured, else nothing.

    Gated on ``AIMU_IMAGE_MODEL`` so a default install (no image model) doesn't expose a
    generate_image tool the model could call but never satisfy."""
    if not os.environ.get(_IMAGE_MODEL_ENV):
        return []

    cache: dict = {}

    def _client():
        # Built once, on first use: reads AIMU_IMAGE_MODEL (e.g. "gemini:nano-banana" or "hf:<repo>") and
        # raises ValueError if unset. Cached so weights / API clients aren't rebuilt per call.
        if "client" not in cache:
            import aimu

            cache["client"] = aimu.image_client()
        return cache["client"]

    @tool
    def generate_image(prompt: str):
        """Generate an image from a text prompt; the image is shown to the user and saved.

        This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
        If the model it names still fails to load, this reports generation as unavailable instead of
        raising.

        Args:
            prompt: A description of the desired image.
        """
        config.images_path.mkdir(parents=True, exist_ok=True)
        try:
            client = _client()
        except Exception as exc:
            return f"Image generation is unavailable: {exc}. Check the AIMU_IMAGE_MODEL environment variable."

        # Streaming generator: yield IMAGE_GENERATING chunks (denoising progress flows to the UI live), then
        # return the saved reference. output_dir directs the final file into the servable images folder.
        final_result = None
        for chunk in client.generate(prompt, format="path", output_dir=config.images_path, stream=True):
            yield chunk
            content = chunk.content
            if isinstance(content, dict) and content.get("final"):
                final_result = content.get("result")

        if not final_result:
            return "Image generation produced no output."
        name = Path(final_result).name
        return f"Generated image, shown to the user inline (/images/{name})."

    return [generate_image]


TOOLSET = Toolset(
    name="image",
    description="Generate images from a text prompt (needs the AIMU_IMAGE_MODEL environment variable set).",
    build=lambda ctx: build(ctx.config),
)
