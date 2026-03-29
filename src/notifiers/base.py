# src/notifiers/base.py
from abc import ABC, abstractmethod


class BaseNotifier(ABC):
    def __init__(self, config: dict):
        self.config = config

    @abstractmethod
    async def send(self, digest: str, compact_digest: str = None) -> bool:
        """Send digest. compact_digest is for channels with size limits."""
        pass
