"""A built-in toolset that generates images from a text prompt.

Contributes one tool, ``generate_image``, that runs an AIMU ``ImageClient`` and saves the result into
``images_path`` (the same folder uploaded images live in, served by the web front end at
``/images/<name>``), under its content address. The tool then publishes the image to the running turn
(``core/artifacts.py``) rather than relying on the web channel spotting its stream chunk or a regex spotting
its result text, so the image shows whichever agent made it. The model is sent the image too, as an AIMU
``ToolResult``, so a vision model can see what it made and say how close it came to the request; a model
without vision gets the sentence alone. The image rides every later request in that conversation, as a
user's attached image does.

**Refining an image** passes an earlier one (generated, or attached by the user) as ``reference_image``,
its ``/images/...`` reference, checked by the same guard that serves uploads, so nothing outside
``images_path`` can be read. A HuggingFace model starts its denoising from that image (image-to-image),
which keeps the layout and framing and changes style or detail, by as much as ``strength`` allows; it
cannot zoom out or extend the canvas, and the tool's description says so, because "show me more of it"
is the request a user most naturally makes and image-to-image cannot meet it. Gemini edits by instruction
instead and takes no strength. Which of those arguments the tool offers depends on the configured model
(:func:`reference_support`), which costs the provider library's import once, at the first build.

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
import functools
import logging
import mimetypes
import os
import re
from pathlib import Path
from typing import Optional

from aimu.models import StreamChunk, StreamingContentType
from aimu.tools import ToolResult, tool

from kokua import images
from kokua.config import AssistantConfig
from kokua.registry import Toolset

logger = logging.getLogger(__name__)

# AIMU's image client resolves this env var for the model; without it, generation cannot work, so the
# tool is not offered at all (the model never sees an option it can't fulfill).
_IMAGE_MODEL_ENV = "AIMU_IMAGE_MODEL"


def _filename_for(prompt: str, suffix: str) -> str:
    """A download name from the prompt's first words: readable in a downloads folder, never a path."""
    words = re.findall(r"[a-z0-9]+", prompt.lower())[:6]
    return ("-".join(words) or "image") + suffix


def _final(result: str | ToolResult) -> StreamChunk:
    """The tool's answer to the model. An async generator cannot return a value, so AIMU's tool loop
    takes the last chunk's ``result`` as the response instead."""
    return StreamChunk(StreamingContentType.IMAGE_GENERATING, {"final": True, "result": result})


@functools.lru_cache(maxsize=None)
def reference_support(model: str) -> tuple[bool, bool]:
    """``(starts from an image, honours strength)`` for the image model *model* names.

    Read from AIMU's catalogue entry for the model, without building a client or loading weights. Some
    models take a reference image as a starting point (a HuggingFace model with an image-to-image
    pipeline, or Gemini, which edits by instruction), and of those only the HuggingFace ones take a
    ``strength``. A model AIMU does not catalogue is treated as taking neither, so the tool never offers
    an argument the model cannot honour. Cached because the lookup imports the provider's library
    (torch and diffusers for a HuggingFace model, about a second and a half), which should be paid once.
    """
    import aimu

    try:
        spec = aimu.resolve_image_model_enum(model).spec
    except Exception as exc:
        logger.info("Image model %r is not in AIMU's catalogue, so generate_image takes no reference: %s", model, exc)
        return False, False
    starts_from_image = bool(spec.supports_reference_image)
    return starts_from_image, starts_from_image and bool(getattr(spec, "img2img_uses_strength", False))


def build(config: AssistantConfig) -> list:
    """Return this toolset's tools when an image model is configured, else nothing.

    Gated on ``AIMU_IMAGE_MODEL`` so a default install (no image model) doesn't expose a
    generate_image tool the model could call but never satisfy. The tool's arguments follow the model
    in the same spirit (see :func:`reference_support`): ``reference_image`` only for a model that can
    start from an image, and ``strength`` only for one that honours it."""
    model = os.environ.get(_IMAGE_MODEL_ENV)
    if not model:
        return []

    cache: dict = {}

    def _client():
        # Built once, on first use: reads AIMU_IMAGE_MODEL (e.g. "gemini:nano-banana" or "hf:<repo>") and
        # raises ValueError if unset. Cached so weights / API clients aren't rebuilt per call.
        if "client" not in cache:
            import aimu

            cache["client"] = aimu.image_client()
        return cache["client"]

    async def generate(prompt: str, reference_image: str = "", strength: Optional[float] = None):
        """The one implementation behind every shape of the tool below."""
        from kokua.core.artifacts import publish

        options: dict = {}
        if reference_image:
            source = images.reference_to_path(config.images_path, reference_image)
            if source is None:
                yield _final(
                    f"There is no stored image {reference_image!r}. Pass the /images/... reference an image was "
                    "given when it was generated or attached, or leave reference_image empty for a new image."
                )
                return
            options["reference_image"] = source
            if strength is not None:
                if not 0 < strength <= 1:
                    yield _final(f"strength must be greater than 0 and at most 1, got {strength}.")
                    return
                options["strength"] = strength

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
        steps = iter(client.generate(prompt, format="path", output_dir=config.images_path, stream=True, **options))
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
        # Reads the file to inline it, so off the loop like the adopt above.
        result = await asyncio.to_thread(
            ToolResult, f"Generated image, shown to the user ({reference}).", images=[stored]
        )
        yield _final(result)

    # Three shapes of one tool, so the model is only ever offered arguments its image model honours.
    starts_from_image, honours_strength = reference_support(model)
    if honours_strength:

        @tool
        async def generate_image(prompt: str, reference_image: str = "", strength: float = 0.75):
            """Generate an image from a text prompt; the image is shown to the user and saved.

            To refine an earlier image (one you generated, or one the user attached), pass its /images/...
            reference as reference_image: the new image starts from it, keeping its layout and framing
            while the prompt changes its style or details. Starting from an image cannot zoom out or
            extend the picture beyond its edges; a higher strength lets the result move further from the
            original, at the cost of resembling it less.

            This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
            If the model it names still fails to load, this reports generation as unavailable instead of
            raising.

            Args:
                prompt: A description of the desired image.
                reference_image: Optional. The /images/... reference of an image to start from. Leave empty
                    for a new image.
                strength: With reference_image, how far the result may move from it: near 0 keeps it almost
                    unchanged, 1 barely follows it. Default 0.75.
            """
            async for chunk in generate(prompt, reference_image, strength if reference_image else None):
                yield chunk

    elif starts_from_image:

        @tool
        async def generate_image(prompt: str, reference_image: str = ""):
            """Generate an image from a text prompt; the image is shown to the user and saved.

            To change an earlier image (one you generated, or one the user attached), pass its /images/...
            reference as reference_image and describe the change in the prompt.

            This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
            If the model it names still fails to load, this reports generation as unavailable instead of
            raising.

            Args:
                prompt: A description of the desired image, or of the change to make to reference_image.
                reference_image: Optional. The /images/... reference of an image to start from. Leave empty
                    for a new image.
            """
            async for chunk in generate(prompt, reference_image):
                yield chunk

    else:

        @tool
        async def generate_image(prompt: str):
            """Generate an image from a text prompt; the image is shown to the user and saved.

            This tool is offered only when AIMU_IMAGE_MODEL is configured, so seeing it means one is set.
            If the model it names still fails to load, this reports generation as unavailable instead of
            raising.

            Args:
                prompt: A description of the desired image.
            """
            async for chunk in generate(prompt):
                yield chunk

    return [generate_image]


TOOLSET = Toolset(
    name="image",
    description="Generate images from a text prompt (needs the AIMU_IMAGE_MODEL environment variable set).",
    build=lambda ctx: build(ctx.config),
)
