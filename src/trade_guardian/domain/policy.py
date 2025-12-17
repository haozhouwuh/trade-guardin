from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class ShortLegPolicy:
    base_rank: int = 1
    min_dte: int = 3
    max_probe_rank: int = 3  # count, e.g. 3 => ranks base..base+2

    def probe_ranks(self) -> List[int]:
        if self.max_probe_rank <= 1:
            return [self.base_rank]
        return list(range(self.base_rank, self.base_rank + self.max_probe_rank))
