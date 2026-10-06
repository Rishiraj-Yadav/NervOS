"""The pinned LangGraph release this NervOS workflow adapter was qualified against.

Read from the *installed* distribution at import time, so the compatibility matrix in
``docs/autonomous-workflows/langgraph-compatibility.md`` cannot quietly drift away from the
version actually resolved. The adapter refuses anything else rather than guessing.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: The one LangGraph version this adapter is qualified against. Changing it requires
#: re-running the compatibility tests and updating the matrix, not just this constant.
QUALIFIED_LANGGRAPH_VERSION = "1.2.12"

#: The LangChain core range LangGraph 1.2.12 itself declares.
QUALIFIED_LANGCHAIN_CORE_SPEC = ">=1.4.7,<2"

#: Python range LangGraph 1.2.12 itself declares.
QUALIFIED_PYTHON_SPEC = ">=3.10"


class UnqualifiedLangGraph(RuntimeError):
    """The installed LangGraph is not the release this adapter was qualified against."""


def installed_langgraph_version() -> str | None:
    try:
        return version("langgraph")
    except PackageNotFoundError:
        return None


def assert_qualified_langgraph() -> str:
    """Refuse an unqualified LangGraph rather than running against an untested version.

    An adapter that silently adapts an untested release is worse than no adapter: the
    framework's state semantics can change, and the failure would surface as a lost or
    duplicated workflow step rather than as a version mismatch.
    """
    found = installed_langgraph_version()
    if found is None:
        raise UnqualifiedLangGraph("langgraph is not installed")
    if found != QUALIFIED_LANGGRAPH_VERSION:
        raise UnqualifiedLangGraph(
            f"langgraph {found} is installed; this adapter is qualified against "
            f"{QUALIFIED_LANGGRAPH_VERSION}"
        )
    return found
