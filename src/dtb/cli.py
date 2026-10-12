"""DTB command-line interface.

Commands:
    dtb serve [--host 0.0.0.0] [--port 8000]   — run API + UI
    dtb demo                                    — create the demo enterprise in the DB
    dtb simulate <scenario_id> [--runs N] [--seed S]
    dtb validate <enterprise_id>
    dtb export-template <path.xlsx>
    dtb verify [--benchmarks mm1,mmc,...] [--replications N] [--seed S]
    dtb verify-run <scenario_id> [--seed S]
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dtb", description="DTB — Digital Twin of Business")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="запустить API и веб-интерфейс")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true")

    sub.add_parser("demo", help="создать демо-предприятие в базе")

    p_sim = sub.add_parser("simulate", help="запустить симуляцию сценария")
    p_sim.add_argument("scenario_id")
    p_sim.add_argument("--runs", type=int, default=1)
    p_sim.add_argument("--seed", type=int, default=None)

    p_val = sub.add_parser("validate", help="проверить модель предприятия")
    p_val.add_argument("enterprise_id")

    p_tpl = sub.add_parser("export-template", help="выгрузить пустой Excel-шаблон модели")
    p_tpl.add_argument("path")

    p_ver = sub.add_parser("verify", help="сверить движок с аналитическими эталонами")
    p_ver.add_argument(
        "--benchmarks", default="", help="список через запятую: mm1,mmc,dd1,discounting,little"
    )
    p_ver.add_argument("--replications", type=int, default=3)
    p_ver.add_argument("--seed", type=int, default=20260101)
    p_ver.add_argument("--horizon", type=float, default=None)

    p_vrun = sub.add_parser(
        "verify-run", help="проверить инварианты на прогоне сценария"
    )
    p_vrun.add_argument("scenario_id")
    p_vrun.add_argument("--seed", type=int, default=None)

    args = parser.parse_args(argv)

    if args.command == "verify":
        from dtb.verification import run_benchmarks

        names = [n.strip() for n in args.benchmarks.split(",") if n.strip()] or None
        report = run_benchmarks(
            names, seed=args.seed, horizon=args.horizon, replications=args.replications
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.passed else 3

    if args.command == "serve":
        import uvicorn

        uvicorn.run("dtb.api.app:create_app", host=args.host, port=args.port,
                    reload=args.reload, factory=True)
        return 0

    from dtb.persistence import Database

    db = Database()

    if args.command == "demo":
        from dtb.demo import create_demo

        model, scenarios = create_demo()
        db.save_model(model)
        db.save_version(model, author="cli", comment="demo model v1")
        for scenario in scenarios:
            db.save_scenario(scenario)
        print(f"Создано предприятие {model.enterprise.id!r} и {len(scenarios)} сценариев:")
        for scenario in scenarios:
            print(f"  - {scenario.id}: {scenario.name}")
        return 0

    if args.command == "simulate":
        from dtb.analysis import run_monte_carlo
        from dtb.simulation import simulate
        from dtb.validation import validate_model

        scenario = db.load_scenario(args.scenario_id)
        if scenario is None:
            print(f"Сценарий {args.scenario_id!r} не найден", file=sys.stderr)
            return 1
        model = db.load_model(scenario.enterprise_id)
        if model is None:
            print("Предприятие не найдено", file=sys.stderr)
            return 1
        report = validate_model(model)
        if not report.ok_to_simulate:
            print("Модель содержит критические ошибки:", file=sys.stderr)
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        if args.runs > 1:
            mc = run_monte_carlo(model, scenario, runs=args.runs, master_seed=args.seed)
            print(json.dumps({
                "runs": mc.runs,
                "final_kpis": mc.final_kpis,
                "goal_achievement_probability": mc.goal_achievement_probability,
            }, ensure_ascii=False, indent=2))
        else:
            result = simulate(model, scenario, seed=args.seed)
            print(json.dumps({
                "status": result.status,
                "digest": result.content_digest(),
                "final_kpis": result.final_kpis,
                "goal_achievement": result.goal_achievement,
                "metrics": result.metrics,
            }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "validate":
        from dtb.validation import data_quality_report, validate_model

        model = db.load_model(args.enterprise_id)
        if model is None:
            print(f"Предприятие {args.enterprise_id!r} не найдено", file=sys.stderr)
            return 1
        print(json.dumps({
            "validation": validate_model(model).to_dict(),
            "data_quality": data_quality_report(model),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.command == "verify-run":
        from dtb.simulation import simulate
        from dtb.verification import verify_run

        scenario = db.load_scenario(args.scenario_id)
        if scenario is None:
            print(f"Сценарий {args.scenario_id!r} не найден", file=sys.stderr)
            return 1
        model = db.load_model(scenario.enterprise_id)
        if model is None:
            print("Предприятие не найдено", file=sys.stderr)
            return 1
        result = simulate(model, scenario, seed=args.seed)
        report = verify_run(result, model)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0 if report.passed else 3

    if args.command == "export-template":
        from dtb.domain import Enterprise, EnterpriseModel
        from dtb.integration import export_workbook

        empty = EnterpriseModel(enterprise=Enterprise(id="template", name="Шаблон модели"))
        with open(args.path, "wb") as f:
            f.write(export_workbook(empty))
        print(f"Шаблон сохранён: {args.path}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
