"""Agent tools that search a TiDB Cloud Filesystem with its full-text and semantic index."""

from __future__ import annotations

import posixpath

from strands import tool
from strands.tools.decorator import DecoratedFunctionTool

from .client import RemoteEntry, TiFSClient
from .errors import TiFSError


def _scope(root: str, path: str | None) -> str:
    """Resolve ``path`` inside ``root``, refusing to escape it."""
    root = "/" + root.strip("/")
    if not path:
        return root
    target = posixpath.normpath(posixpath.join(root, path.lstrip("/")))
    if target != root and not target.startswith(root.rstrip("/") + "/"):
        raise ValueError(f"{path} is outside {root}")
    return target


def _format(entries: list[RemoteEntry], root: str, limit: int) -> str:
    # Only report paths inside root, even if the service returned more (defence in depth).
    lines = [entry.path for entry in entries if _inside(entry.path, root)][:limit]
    return "\n".join(lines) if lines else "No matching files."


def _inside(path: str, root: str) -> bool:
    root = "/" + root.strip("/")
    target = posixpath.normpath("/" + path.lstrip(":/"))
    return root == "/" or target == root or target.startswith(root + "/")


def make_search_files(
    client: TiFSClient | None = None, *, root: str = "/", name: str = "search_files"
) -> DecoratedFunctionTool:
    """Create a tool that searches file contents by keywords or meaning.

    Args:
        client: Client for the file system. Defaults to one configured from the environment.
        root: Directory the tool may search; paths the agent passes are relative to it.
        name: Tool name.
    """
    ti = client or TiFSClient()

    @tool(name=name)
    async def search_files(query: str, path: str | None = None, limit: int = 20) -> str:
        """Search the contents of stored files by keywords or meaning and list the best matches.

        Finds files that are about the query even when they do not contain the exact
        words, including text extracted from PDFs and images. The query is plain text,
        not a regular expression.

        Args:
            query: What to look for, in natural language or keywords.
            path: Directory to search. Defaults to everything the tool can see.
            limit: Maximum number of files to return (at most 20).
        """
        try:
            entries = await ti.search(query, path=_scope(root, path), limit=limit)
        except (TiFSError, ValueError) as e:
            return f"Search failed: {e}"
        return _format(entries, root, limit)

    return search_files


def make_find_files(
    client: TiFSClient | None = None, *, root: str = "/", name: str = "find_files"
) -> DecoratedFunctionTool:
    """Create a tool that finds files by name, date, size or tag.

    Args:
        client: Client for the file system. Defaults to one configured from the environment.
        root: Directory the tool may search; paths the agent passes are relative to it.
        name: Tool name.
    """
    ti = client or TiFSClient()

    @tool(name=name)
    async def find_files(
        name_pattern: str | None = None,
        path: str | None = None,
        newer_than: str | None = None,
        older_than: str | None = None,
        min_size_bytes: int | None = None,
        max_size_bytes: int | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> str:
        """Find stored files by name glob, modification date, size or tag.

        Args:
            name_pattern: File name glob such as "*.md".
            path: Directory to search. Defaults to everything the tool can see.
            newer_than: Only files modified after this date (YYYY-MM-DD).
            older_than: Only files modified before this date (YYYY-MM-DD).
            min_size_bytes: Minimum file size.
            max_size_bytes: Maximum file size.
            tag: Only files with this tag, as key=value.
            limit: Maximum number of files to return (at most 100).
        """
        try:
            entries = await ti.find(
                path=_scope(root, path),
                name=name_pattern,
                tag=tag,
                newer=newer_than,
                older=older_than,
                min_size=min_size_bytes,
                max_size=max_size_bytes,
                limit=limit,
            )
        except (TiFSError, ValueError) as e:
            return f"Find failed: {e}"
        return _format(entries, root, limit)

    return find_files
