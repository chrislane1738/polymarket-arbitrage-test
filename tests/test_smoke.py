def test_smoke():
    assert 1 + 1 == 2


def test_can_import_project_packages():
    from analytics import arb, book, paper  # noqa: F401
    from feeds import kalshi_ws, polymarket_ws  # noqa: F401
    from store import sqlite  # noqa: F401
