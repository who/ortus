"""Abstract base class for video generation providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class GenerationResult:
    """Result of polling a video generation job."""

    job_id: str
    status: str  # "pending" | "completed" | "failed"
    output_path: Optional[str] = None
    error: Optional[str] = None


class VideoProvider(ABC):
    """Abstract base class for video generation providers."""

    @abstractmethod
    def submit(self, prompt: str, duration: int, resolution: str) -> str:
        """Submit a video generation job.

        Returns the job ID.
        """

    @abstractmethod
    def poll(self, job_id: str) -> GenerationResult:
        """Poll the status of a video generation job."""

    @abstractmethod
    def download(self, job_id: str, output_path: str) -> None:
        """Download the completed video to the specified path."""
