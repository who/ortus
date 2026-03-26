"""Provider factory for video generation backends."""

from video.providers.base import VideoProvider


def get_provider(config: dict) -> VideoProvider:
    """Map a provider name to its adapter class and return an instance.

    Args:
        config: Dict with at least a 'provider' key naming the backend.

    Returns:
        An instantiated VideoProvider subclass.

    Raises:
        ValueError: If the provider name is not recognized.
    """
    name = config.get("provider", "")

    if name == "sora":
        from video.providers.sora import SoraProvider
        return SoraProvider(config)
    elif name == "runway":
        from video.providers.runway import RunwayProvider
        return RunwayProvider(config)
    elif name == "kling":
        from video.providers.kling import KlingProvider
        return KlingProvider(config)
    elif name == "veo":
        from video.providers.veo import VeoProvider
        return VeoProvider(config)
    elif name == "fal":
        from video.providers.fal import FalProvider
        return FalProvider(config)
    else:
        raise ValueError(f"Unknown provider: {name!r}. Supported: sora, runway, kling, veo, fal")
