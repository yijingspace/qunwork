

def test_task_template_crud(tmp_path):
    from coworker.conversations import ConversationStore

    store = ConversationStore(tmp_path / "data")
    assert store.list_task_templates() == []

    t = store.add_task_template("每周周报", "汇总本周工作并生成周报")
    assert t["title"] == "每周周报"
    assert t["prompt"].startswith("汇总")
    assert t["id"] > 0

    rows = store.list_task_templates()
    assert len(rows) == 1 and rows[0]["title"] == "每周周报"

    # whitespace-only / missing fields rejected
    try:
        store.add_task_template("   ", "x")
        assert False, "expected ValueError"
    except ValueError:
        pass

    assert store.delete_task_template(t["id"]) is True
    assert store.delete_task_template(t["id"]) is False
    assert store.list_task_templates() == []


def test_swarm_template_crud(tmp_path):
    from coworker.conversations import ConversationStore

    store = ConversationStore(tmp_path / "data")
    assert store.list_swarm_templates() == []

    t = store.add_swarm_template(
        "每周市场报告",
        "用蜂群生成每周市场分析报告",
        [{"id": "t1", "description": "调研", "deps": []}, {"id": "t2", "description": "成稿", "deps": ["t1"]}],
    )
    assert t["id"] > 0
    assert len(t["plan"]) == 2 and t["plan"][1]["deps"] == ["t1"]

    rows = store.list_swarm_templates()
    assert len(rows) == 1 and rows[0]["title"] == "每周市场报告"
    assert rows[0]["plan"][0]["id"] == "t1"

    try:
        store.add_swarm_template("", "x", [])
        assert False, "expected ValueError"
    except ValueError:
        pass

    assert store.delete_swarm_template(t["id"]) is True
    assert store.list_swarm_templates() == []
