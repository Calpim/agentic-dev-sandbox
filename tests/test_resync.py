from orchestrator.guards.analyzer import DirectiveType


def test_directive_type_has_resync():
    """DirectiveType に RESYNC が定義されていること"""
    assert DirectiveType.RESYNC.value == "RESYNC"
