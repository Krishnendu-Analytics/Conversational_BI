"""Deterministic text chunking for RAG (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    token_count: int


class TextChunker:
    """Split text into overlapping character windows."""

    def __init__(self, *, chunk_size: int = 800, overlap: int = 120) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        if overlap < 0:
            raise ValueError("overlap must be >= 0")
        if overlap >= chunk_size:
            raise ValueError("overlap must be < chunk_size")
        self.chunk_size = chunk_size
        self.overlap = overlap

    def chunk(self, text: str, *, max_chunks: int | None = None) -> list[TextChunk]:
        cleaned = (text or "").strip()
        if not cleaned:
            return []

        if len(cleaned) <= self.chunk_size:
            return [TextChunk(index=0, text=cleaned, token_count=_approx_tokens(cleaned))]

        step = self.chunk_size - self.overlap
        chunks: list[TextChunk] = []
        start = 0
        index = 0
        while start < len(cleaned):
            end = min(start + self.chunk_size, len(cleaned))
            # Prefer breaking on whitespace near the window end
            piece = cleaned[start:end]
            if end < len(cleaned):
                break_at = piece.rfind(" ")
                if break_at > self.chunk_size // 2:
                    end = start + break_at
                    piece = cleaned[start:end]
            piece = piece.strip()
            if piece:
                chunks.append(
                    TextChunk(index=index, text=piece, token_count=_approx_tokens(piece))
                )
                index += 1
                if max_chunks is not None and len(chunks) >= max_chunks:
                    break
            if end >= len(cleaned):
                break
            start = max(end - self.overlap, start + 1)
        return chunks


def _approx_tokens(text: str) -> int:
    return max(1, len(text.split()))
