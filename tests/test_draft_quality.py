from types import SimpleNamespace

from backend.agents.drafting.graph import label_proposals, pending_questions, saved_notes


def test_exact_saved_rules_and_exclusions_are_retained_without_model_rewrite():
    notes = {
        "users": ["店员不能退款"],
        "scope": ["手工录单后扣库存"],
        "constraints": ["不做财务对账"],
        "goals": ["录单压缩到30分钟"],
    }
    body = saved_notes(notes)
    for values in notes.values():
        assert all(value in body for value in values)


def test_only_pending_section_is_parsed_without_another_review_call():
    body = (
        "## 主要功能\n- 支付确认后扣库存\n## 待确认事项\n"
        "- 支付渠道接入条件待确认\n## 客户已说明的信息\n- 店员不能退款"
    )
    questions = pending_questions(body, {}, SimpleNamespace(conflicts=[]))
    assert questions == ["支付渠道接入条件待确认"]


def test_generated_details_cannot_label_themselves_as_confirmed_customer_facts():
    body = label_proposals(
        "## 支付流程\n### 输入字段\n- 【已说明】上传截图\n## 待确认事项\n- 接入条件未知"
    )
    assert "## 支付流程" in body
    assert "### 输入字段" in body
    assert "【已说明】" not in body
    assert pending_questions(body, {}, SimpleNamespace(conflicts=[])) == ["接入条件未知"]


def test_legacy_suggestions_are_marked_automatic_without_turning_them_into_questions():
    body = label_proposals("## 实施建议\n【建议，待确认】使用关系数据库。\n## 待确认事项\n暂无")
    assert "【自动生成】使用关系数据库。" in body
    assert pending_questions(body, {}, SimpleNamespace(conflicts=[])) == []
