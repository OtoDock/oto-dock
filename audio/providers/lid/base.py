"""The language identifier contract: a label list and one log score per
label for a window of 16 kHz speech."""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class LanguageIdentifier(ABC):
    """Scores a short window of speech against the languages it knows.

    ``labels`` are the model's language codes in its output order (ISO
    639-1 for the supported languages);
    ``scores`` runs the model synchronously (callers run it off the loop)
    and returns one log score per label, comparable across labels so a
    softmax over any subset of them is a posterior over that subset.
    """

    labels: list[str] = []

    @abstractmethod
    def scores(self, pcm_16k: np.ndarray) -> np.ndarray:
        """``pcm_16k``: mono float32 samples in -1..1 at 16 kHz."""

    def available(self) -> bool:
        return True
