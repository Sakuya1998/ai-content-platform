"""Deterministic context assembly independent of storage or model providers."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ContextKind = Literal["canon", "constraints", "current", "retrieval"]
_PRIORITY: dict[ContextKind, int] = {"canon": 0, "constraints": 1, "current": 2, "retrieval": 3}


class ContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ContextKind
    content: str
    source: str = Field(min_length=1)


class ContextBlock(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ContextKind
    content: str
    source: str
    content_hash: str
    token_count: int


class ContextPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "1.0"
    blocks: tuple[ContextBlock, ...] = ()
    token_count: int = 0
    context_hash: str = ""


def _token_count(text: str) -> int:
    """Stable multilingual estimate used before a provider tokenizer exists."""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", " ", text)
    return cjk + len(non_cjk.split())


class ContextAssembler:
    def __init__(self, token_budget: int) -> None:
        if token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        self.token_budget = token_budget

    def assemble(self, inputs: list[ContextInput]) -> ContextPack:
        ordered = sorted(enumerate(inputs), key=lambda item: (_PRIORITY[item[1].kind], item[0]))
        selected: list[ContextBlock] = []
        used = 0
        for _, item in ordered:
            count = _token_count(item.content)
            if used + count > self.token_budget:
                continue
            digest_source = json.dumps(
                {"kind": item.kind, "source": item.source, "content": item.content},
                ensure_ascii=False,
                sort_keys=True,
            )
            selected.append(
                ContextBlock(
                    kind=item.kind,
                    content=item.content,
                    source=item.source,
                    content_hash=hashlib.sha256(digest_source.encode("utf-8")).hexdigest(),
                    token_count=count,
                )
            )
            used += count
        payload = [block.model_dump(mode="json") for block in selected]
        context_hash = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return ContextPack(blocks=tuple(selected), token_count=used, context_hash=context_hash)
