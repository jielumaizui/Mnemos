from core.kia.decision_dependency_extractor import DecisionGraph, DecisionNode


def test_get_root_decisions_returns_nodes_without_dependencies():
    foundation = DecisionNode(id="d1", decision="Use PostgreSQL")
    dependent = DecisionNode(
        id="d2",
        decision="Build audit views on PostgreSQL",
        dependencies=["d1"],
    )
    graph = DecisionGraph(
        nodes={"d1": foundation, "d2": dependent},
        edges=[("d2", "d1", "depends_on")],
    )

    assert graph.get_root_decisions() == [foundation]
