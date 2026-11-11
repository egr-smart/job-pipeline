"""Pluggable LLM backends and the scoring step built on them."""

from ..config import CliConfig, LLMBackendConfig
from .base import Backend, LLMError


def make_backend(cfg: LLMBackendConfig) -> Backend:
    if isinstance(cfg, CliConfig):
        from .cli import CliBackend

        return CliBackend(cfg)
    from .openai_compat import OpenAICompatibleBackend

    return OpenAICompatibleBackend(cfg)


__all__ = ["Backend", "LLMError", "make_backend"]
