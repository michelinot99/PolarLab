"""Buffer circolari numpy con indice assoluto del campione."""

from __future__ import annotations

import numpy as np


class Ring:
    """Ultimi `capacity` valori (scalari o vettori di `width`), indicizzati in modo assoluto.

    L'indice assoluto del primo campione mai inserito è 0; `end` è l'indice del prossimo.
    """

    def __init__(self, capacity: int, width: int | None = None, dtype=np.float64):
        shape = (capacity,) if width is None else (capacity, width)
        self._buf = np.zeros(shape, dtype=dtype)
        self.capacity = capacity
        self.end = 0

    @property
    def start(self) -> int:
        return max(0, self.end - self.capacity)

    def __len__(self) -> int:
        return self.end - self.start

    def extend(self, values: np.ndarray) -> None:
        values = np.asarray(values)
        n = len(values)
        if n == 0:
            return
        if n >= self.capacity:
            values = values[-self.capacity:]
            self.end += n - self.capacity
            n = self.capacity
        i = self.end % self.capacity
        first = min(n, self.capacity - i)
        self._buf[i:i + first] = values[:first]
        if first < n:
            self._buf[: n - first] = values[first:]
        self.end += n

    def get(self, a: int, b: int) -> np.ndarray:
        """Campioni con indice assoluto in [a, b). Lancia IndexError se non più disponibili."""
        if a < self.start or b > self.end or a > b:
            raise IndexError(f"intervallo [{a}, {b}) fuori da [{self.start}, {self.end})")
        idx = np.arange(a, b) % self.capacity
        return self._buf[idx]

    def last(self, n: int) -> np.ndarray:
        n = min(n, len(self))
        return self.get(self.end - n, self.end)

    def at(self, i: int):
        return self.get(i, i + 1)[0]

    def clear(self) -> None:
        self.end = 0
