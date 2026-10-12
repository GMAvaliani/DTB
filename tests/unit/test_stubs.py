"""ТЗ 0.3: заготовки ниши — места заведены, расчёт намеренно не реализован.

Эти тесты закрепляют именно границу: сущности хранятся, версионируются,
переносятся через Excel и доступны в API, но на ход расчёта не влияют. Если
когда-нибудь влияние появится, тесты об этом скажут — и это будет осознанным
изменением предмета работы, а не случайностью.
"""

import pytest

from dtb.domain import Scenario
from dtb.domain.links import (
    NOT_IMPLEMENTED_COLLECTIONS,
    Feedback,
    Gap,
    GapType,
    Link,
    LinkFlowKind,
    TransferRegistry,
)
from dtb.domain.model import SCHEMA_VERSION, EnterpriseModel
from dtb.simulation import simulate
from dtb.validation import validate_model
from tests.conftest import build_small_model


def model_with_stubs():
    model = build_small_model(horizon=120.0)
    model.links["l1"] = Link(
        id="l1", name="Производство → продажа",
        source_sed_id="production", target_sed_id="sale",
        utility_type_id="product", health_criterion="stock('product') > 0",
        delay=4.0, reaction_horizon=12.0, critical_moment=100.0,
    )
    model.gap_types["g_flow"] = GapType(
        id="g_flow", name="Разрыв прямого потока", broken_object="forward_flow",
        localization_check="нет передачи результата в срок",
        decision_class="регулирование", closure_mode="regulatory",
    )
    model.gaps.append(
        Gap(id="gap1", gap_type_id="g_flow", link_id="l1", detected_at=50.0, severity=0.7)
    )
    model.feedbacks.append(
        Feedback(id="f1", link_id="l1", from_sed_id="sale", to_sed_id="production",
                 kind="acceptance", timestamp=60.0)
    )
    model.transfers.append(
        TransferRegistry(id="t1", link_id="l1", source_sed_id="production",
                         target_sed_id="sale", utility_type_id="product",
                         quantity=1.0, sent_at=55.0)
    )
    return model


def test_stubs_do_not_change_the_run():
    """Заготовки не влияют на расчёт: траектория та же, что и без них."""
    scenario = Scenario(id="s", name="s", enterprise_id="acme", seed=42)
    plain = simulate(build_small_model(horizon=120.0), scenario, seed=42)
    with_stubs = simulate(model_with_stubs(), scenario, seed=42)
    assert with_stubs.final_kpis == plain.final_kpis
    assert with_stubs.sed_stats == plain.sed_stats
    assert with_stubs.metrics["switching_moments"] == plain.metrics["switching_moments"]
    assert [e["type"] for e in with_stubs.event_log] == [
        e["type"] for e in plain.event_log
    ]


def test_stubs_survive_snapshot_roundtrip():
    """Заготовки переживают выгрузку и загрузку модели вместе с версиями."""
    model = model_with_stubs()
    restored = EnterpriseModel.from_snapshot(model.to_snapshot())
    assert restored.links["l1"].delay == 4.0
    assert restored.links["l1"].critical_moment == 100.0
    assert restored.links["l1"].implemented is False
    assert restored.gap_types["g_flow"].closure_mode == "regulatory"
    assert restored.gaps[0].severity == 0.7
    assert restored.feedbacks[0].kind == "acceptance"
    assert restored.transfers[0].quantity == 1.0
    assert restored.content_hash() == model.content_hash()
    assert restored.schema_version == SCHEMA_VERSION


def test_stub_fields_are_marked_not_implemented():
    assert NOT_IMPLEMENTED_COLLECTIONS == ("link", "gap_type")
    assert LinkFlowKind.FORWARD.value == "forward"
    assert LinkFlowKind.BACKWARD.value == "backward"
    # признак явный: связь не задействована в расчёте
    assert Link(id="x", source_sed_id="a", target_sed_id="b").implemented is False


def test_validation_says_stubs_are_not_implemented():
    report = validate_model(model_with_stubs())
    assert report.ok_to_simulate
    notices = [i for i in report.issues if i.code == "not_implemented"]
    assert {i.entity_type for i in notices} >= {"link", "gap_type", "model"}
    assert all(i.severity.value == "info" for i in notices)


def test_validation_checks_stub_references():
    """Ссылки заготовок проверяются: битая ссылка — предупреждение, не ошибка."""
    model = model_with_stubs()
    model.links["l1"].source_sed_id = "ghost"
    model.links["l1"].responsible_subject_ref = "nobody"
    report = validate_model(model)
    codes = [(i.code, i.entity_type) for i in report.issues]
    assert ("broken_ref", "link") in codes
    assert report.ok_to_simulate  # расчёт не зависит от заготовок


def test_v4_snapshot_migrates_to_stub_collections():
    model = build_small_model()
    snapshot = model.to_snapshot()
    snapshot["schema_version"] = 4
    snapshot.pop("feedbacks", None)
    snapshot.pop("transfers", None)
    restored = EnterpriseModel.from_snapshot(snapshot)
    assert restored.schema_version == SCHEMA_VERSION
    assert restored.feedbacks == [] and restored.transfers == []


def test_stub_entities_are_addressable_like_any_entity():
    model = model_with_stubs()
    assert model.get_entity("link", "l1") is not None
    assert model.get_entity("gap_type", "g_flow") is not None
    assert {etype for etype, _ in model.iter_entities()} >= {"link", "gap_type"}
    assert model.delete_entity("link", "l1") is True
    with pytest.raises(KeyError):
        model.collection("feedback")  # отдельного типа сущности нет: это список
