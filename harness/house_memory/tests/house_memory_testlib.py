"""Shared helpers for house_memory tests."""

from house_memory import Memory


def make_memory(content: str, subject: str = "plink", **kwargs) -> Memory:
    kwargs.setdefault("source_author", "plink")
    kwargs.setdefault("author_of_memory", "testbot")
    return Memory(content=content, subject=subject, **kwargs)
