"""Demo enterprise factory.

* ``plant`` — «Демо-Завод»: заказ → производство → доставка → выручка;
* ``farm`` — «Агроферма: строительство»: смета стройки как проект EVM,
  бригады, риски, деньги и онлайн-мониторинг.
"""

from dtb.demo.factory import build_demo_model, build_demo_scenarios, create_demo
from dtb.demo.farm import build_farm_model, build_farm_scenarios, create_farm_demo

#: Демо по ключу: функция, возвращающая (модель, сценарии).
DEMOS = {"plant": create_demo, "farm": create_farm_demo}


def create_demos(which: str = "all"):
    """Список (модель, сценарии) для ``plant``, ``farm`` или ``all``."""
    if which == "all":
        return [factory() for factory in DEMOS.values()]
    if which not in DEMOS:
        raise KeyError(f"неизвестное демо {which!r}: допустимо {', '.join(DEMOS)} или all")
    return [DEMOS[which]()]


__all__ = [
    "DEMOS",
    "build_demo_model",
    "build_demo_scenarios",
    "build_farm_model",
    "build_farm_scenarios",
    "create_demo",
    "create_demos",
    "create_farm_demo",
]
