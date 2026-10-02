"""Token estimates without a tokenizer dependency.

A token budget exists so a search result does not flood a model's context.
For that, an estimate that is consistently within about twenty percent is
enough: the number is a bound, not a bill. Pulling in a tokenizer for it would
add a dependency and a model download to every install, and the package's
central promise is that it works offline with five dependencies.

Anyone who needs exact counts passes ``token_counter=`` to ``Vectrix`` and
every estimate in the package goes through it instead.
"""

from __future__ import annotations

import math
from typing import Callable, Optional


# ============================================================================
# SETTINGS: the counter type, and the characters a token costs
# ============================================================================
#
# A caller may hand in a real tokenizer; without one, a fixed characters-per-
# token ratio stands in.

#: Anything that maps text to a token count, such as ``len(enc.encode(s))``.
TokenCounter = Callable[[str], int]

#: The usual English-prose ratio for BPE tokenizers. Code and CJK text run
#: denser, so the estimate leans generous rather than tight.
CHARS_PER_TOKEN = 4.0


# ============================================================================
# THE ESTIMATE
# ============================================================================
#
# INPUT   a text, and a counter when the caller has one
# OUTPUT  how many tokens it costs a model, within about twenty percent
#
# A bound, not a bill: enough for a budget, without a tokenizer dependency.


def estimate_tokens(text: Optional[str], counter: Optional[TokenCounter] = None) -> int:
    """Estimate how many tokens ``text`` costs a model.

    Empty text is free. With a ``counter`` the answer is exact by definition;
    without one it is ``ceil(len(text) / 4)``, never less than one for any
    non-empty string, so a budget cannot be satisfied by an unbounded number
    of tiny results.
    """
    if not text:
        return 0
    if counter is not None:
        return max(0, int(counter(text)))
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))
