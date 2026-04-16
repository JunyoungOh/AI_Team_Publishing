import pytest


def test_recommend_endpoint_exists():
    from src.ui.server import app
    routes = [r.path for r in app.routes]
    assert '/api/discussion/recommend-participants' in routes


def test_recommend_fallback_on_no_bridge():
    """When API call fails, should return fallback participants."""
    import asyncio
    from src.ui.routes.discussion import _recommend_participants

    result = asyncio.run(
        _recommend_participants("테스트 주제", "free", "basic", 3)
    )
    assert isinstance(result, dict)
    assert "participants" in result
    assert result["human_suggestion"] is None
    assert len(result["participants"]) >= 2
    for p in result["participants"]:
        assert "name" in p
        assert "persona" in p


def test_recommend_fallback_participate_mode():
    """Participate mode should include human_suggestion in fallback."""
    import asyncio
    from src.ui.routes.discussion import _recommend_participants

    result = asyncio.run(
        _recommend_participants("테스트 주제", "free", "participate", 3)
    )
    assert isinstance(result, dict)
    assert result["human_suggestion"] is not None
    assert "name" in result["human_suggestion"]
    assert "persona" in result["human_suggestion"]
    assert len(result["participants"]) >= 2
