"""A built-in toolset that generates images from a text prompt.

Contributes one tool, ``generate_image``, that runs an AIMU ``ImageClient`` and saves the result into
``images_path`` (the same folder uploaded images live in, served by the web front end at
``/images/<name>``), under its content address. The tool then publishes the image to the running turn
(``core/artifacts.py``) rather than relying on the web channel spotting its stream chunk or a regex spotting
its result text, so the image shows whichever agent made it. The model is told only that it was shown.

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

import asyncio
import mimetypes
import os
import re
from pathlib import Path

from aimu.models import StreamChunk, StreamingContentType
from aimu.tools import tool

from kokua import images
from kokua.config import AssistantConfig
from kokua.registry import Toolset

# AIMU's image client resolves this env var for the model; without it, generation cannot work, so the
# tool is not offered at all (the model never sees an option it can't fulfill).
_IMAGE_MODEL_ENV = "AIMU_IMAGE_MODEL"


def _filename_for(prompt: str, suffix: str) -> str:
    """A download name from the prompt's first words: readable in a downloads folder, never a path."""
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:6]
    return ("-".join(words) or "image") + suffix


def _final(text: str) -> StreamChunk:
    """The tool's answer to the model. An async generator cannot return a value, so AIMU's tool loop
    takes the last chunk's ``result`` as the response instead."""
    return StreamChunk(StreamingContentType.IMAGE_GENERATING, {"final": True, "result": text})


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
    async def generate_image(prompt: str):
        """Generate an image from a text prompt; the image is shown to the user and saved.

        This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
        If the model it names still fails to load, this reports generation as unavailable instead of
        raising.

        Args:
            prompt: A description of the desired image.
        """
        from kokua.core.artifacts import publish

        config.images_path.mkdir(parents=True, exist_ok=True)
        # The body of an async tool runs on the loop that serves every conversation, so the blocking work
        # (building a client can import torch and load weights; adopting a file reads and hashes it) goes
        # to a thread, as a sync tool's whole body would have in AIMU's dispatch.
        try:
            client = await asyncio.to_thread(_client)
        except Exception as exc:
            yield _final(f"Image generation is unavailable: {exc}. Check the AIMU_IMAGE_MODEL environment variable.")
            return

        # The client is synchronous and slow (denoising runs in-process for a local model), so each step
        # runs in a worker thread. Async rather than a sync generator so this can await the publish.
        steps = iter(client.generate(prompt, format="path", output_dir=config.images_path, stream=True))
        written = None
        while (chunk := await asyncio.to_thread(next, steps, None)) is not None:
            yield chunk
            content = chunk.content
            if isinstance(content, dict) and content.get("final"):
                written = content.get("result")

        if not written:
            yield _final("Image generation produced no output.")
            return
        reference = await asyncio.to_thread(images.adopt_file, config.images_path, Path(written))
        stored = config.images_path / reference.rsplit("/", 1)[1]
        mime = mimetypes.guess_type(stored.name)[0] or "image/png"
        await publish(reference, filename=_filename_for(prompt, stored.suffix), mime=mime, path=stored)
        yield _final(f"Generated image, shown to the user ({reference}).")

    return [generate_image]


TOOLSET = Toolset(
    name="image",
    description="Generate images from a text prompt (needs the AIMU_IMAGE_MODEL environment variable set).",
    build=lambda ctx: build(ctx.config),
)
