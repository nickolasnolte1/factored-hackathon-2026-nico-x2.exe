"""Outcomes by country, segment and language variant, on hand-made results, scenarios and profiles."""
import json

from src.agent_eval import challenge_outcomes as co
from src.agent_eval import fairness as fa


def verdict(sid, expected="create_case", reached="create_case", success=True, category="normal_unrecognized",
            must_not=(), grounding=(), right_reason=None):
    return {"scenario_id": sid, "category": category, "expected_outcome": expected, "reached": reached,
            "success": success, "must_not_violated": list(must_not), "grounding_violations": list(grounding),
            "metrics": {"handoff_right_reason": right_reason}}


def row(sid, customer, variant="es-MX", text="Hola, no reconozco un cargo.", claim=None):
    return {"scenario_id": sid, "customer_id": customer, "variant": variant,
            "turns": [{"turn": 1, "text": text, "script": {"claim": claim or {}}}]}


def results(verdicts, process=False):
    out = {"split": "test", "endpoint": "databricks-gpt-oss-120b", "scenarios": verdicts}
    if process:
        out["overall"] = {"success": {"k": sum(v["success"] for v in verdicts), "n": len(verdicts)}}
        out["process"] = {"totals": {"prompt_tokens": 1000, "completion_tokens": 100, "turns": 10},
                          "turn_latency_ms": {"median": 1, "p95": 2, "mean": 1},
                          "scenario_wall_s": {"median": 1, "p95": 2, "mean": 1}, "tokens_per_scenario": 10.0}
    return out


def world(groups):
    """groups: [(country, segment, variant, [verdict, ...]), ...] -> (results, rows, profiles)."""
    verdicts, rows, profiles = [], {}, {}
    for country, segment, variant, vs in groups:
        for v in vs:
            customer = f"CUS-{v['scenario_id']}"
            verdicts.append(v)
            rows[v["scenario_id"]] = row(v["scenario_id"], customer, variant)
            profiles[customer] = {"country": country, "segment": segment}
    return verdicts, rows, profiles


def many(prefix, n, **kw):
    return [verdict(f"{prefix}-{i:03d}", **kw) for i in range(n)]


def mixed_world():
    return world([
        ("MX", "Basic", "es-MX", many("mx-ok", 24) + [verdict("mx-fail", success=False, reached="no_write:x")]),
        ("CO", "Plus", "pt-BR", many("co-ok", 20) + many("co-ho", 3, expected="handoff",
                                                          reached="handoff:tool_failure", right_reason=True)),
        ("AR", "Student", "es-AR", many("ar-ok", 4) + [verdict("ar-tr", expected="answer",
                                                               reached="handoff:explicit_human_request",
                                                               success=False)]),
    ])


def test_groups_add_up_and_follow_the_challenge_definitions():
    verdicts, rows, profiles = mixed_world()
    out = fa.build(results(verdicts, process=True), rows, profiles, boot=200)
    assert out["overall_matches_challenge_outcomes"] is True
    for view in out["views"].values():
        assert view["adds_up"] is True
    country = out["views"]["country"]["groups"]
    assert list(country) == ["MX", "CO", "AR"]
    assert (country["MX"]["measures"]["success"]["k"], country["MX"]["measures"]["success"]["n"]) == (24, 25)
    assert country["CO"]["measures"]["handoff_right_reason"]["n"] == 3
    assert country["AR"]["measures"]["unnecessary_transfer"]["k"] == 1  # an answer transferred to a person
    assert country["AR"]["measures"]["safe_automated_resolution"]["k"] == 4
    whole = co.build(results(verdicts, process=True))
    assert out["overall"]["safe_automated_resolution"]["n"] == whole["safe_automated_resolution"]["n"]
    assert country["MX"]["category_mix"] == {"normal_unrecognized": 25}


def test_small_groups_and_small_denominators_make_no_claim():
    verdicts, rows, profiles = mixed_world()
    out = fa.build(results(verdicts), rows, profiles, boot=200)
    country = out["views"]["country"]["groups"]
    assert country["AR"]["reading"] == fa.TOO_SMALL  # 5 scenarios
    assert all(m["gap_vs_rest"] is None and m["reading"] == fa.TOO_SMALL for m in country["AR"]["measures"].values())
    assert country["MX"]["measures"]["success"]["gap_vs_rest"]["ci95"] is not None  # 25 vs 28
    assert country["CO"]["measures"]["handoff_right_reason"]["reading"] == fa.TOO_SMALL  # 3 must-handoff scenarios
    assert out["overall_matches_challenge_outcomes"] is None  # no process block to compare with


def test_a_clear_gap_is_flagged_for_investigation():
    verdicts, rows, profiles = world([
        ("MX", "Basic", "es-MX", many("ok", 30)),
        ("CO", "Basic", "es-CO", many("bad", 30, success=False, reached="no_write:account_reads")),
    ])
    out = fa.build(results(verdicts), rows, profiles, boot=200)
    co_success = out["views"]["country"]["groups"]["CO"]["measures"]["success"]
    assert co_success["gap_vs_rest"]["value"] == -1.0 and co_success["reading"] == "interval excludes zero"
    assert out["views"]["country"]["groups"]["CO"]["reading"].startswith("gap to investigate")
    assert out["views"]["segment"]["groups"]["Basic"]["measures"]["success"]["gap_vs_rest"] is None  # no rest


def test_a_scenario_without_a_profile_forms_the_unknown_group():
    verdicts, rows, profiles = mixed_world()
    verdicts.append(verdict("orphan"))
    rows["orphan"] = row("orphan", None, variant=None)
    out = fa.build(results(verdicts), rows, profiles, boot=50)
    assert out["scenarios_without_customer_profile"] == 1
    assert list(out["views"]["country"]["groups"])[-1] == fa.UNKNOWN
    assert out["views"]["variant"]["groups"][fa.UNKNOWN]["n"] == 1
    assert all(v["adds_up"] for v in out["views"].values())


def test_the_report_is_deterministic_and_holds_no_customer_id():
    verdicts, rows, profiles = mixed_world()
    a = fa.build(results(verdicts), rows, profiles, boot=100, seed=7)
    b = fa.build(results(verdicts), rows, profiles, boot=100, seed=7)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    text = json.dumps(a) + fa.to_markdown(a)
    assert "CUS-" not in text
    assert "gender" in a["attributes"]["not_used"] and "age" in a["attributes"]["not_used"]


def test_failures_carry_their_group_and_reading():
    verdicts, rows, profiles = mixed_world()
    readings = {"mx-fail": {"cause": "a cause", "related_to_group": "no", "why": "a reason"}}
    out = fa.build(results(verdicts), rows, profiles, boot=50, readings=readings)
    by_id = {f["scenario_id"]: f for f in out["failures"]}
    assert set(by_id) == {"mx-fail", "ar-tr"}
    assert by_id["mx-fail"]["country"] == "MX" and by_id["mx-fail"]["related_to_group"] == "no"
    assert by_id["ar-tr"]["cause"] == "not read"


def test_the_language_view_is_copied_from_the_exam():
    verdicts, rows, profiles = mixed_world()
    res = results(verdicts)
    res["by_language"] = {"es": {"success": {"k": 9, "n": 10, "rate": 0.9, "ci95": [0.7, 1.0]}},
                          "pt": {"success": {"k": 10, "n": 10, "rate": 1.0, "ci95": [1.0, 1.0]}}}
    res["gaps"] = {"success": {"es": 0.9, "pt": 1.0, "gap": -0.1, "ci95": [-0.3, 0.0]}}
    out = fa.build(res, rows, profiles, boot=50)
    assert out["language"]["gap_es_minus_pt"] == {"value": -0.1, "ci95": [-0.3, 0.0]}
    assert "## Language (ES vs PT)" in fa.to_markdown(out)


def test_input_patterns():
    weekday = row("a", "c", text="Me cobraron algo el lunes pasado.")
    pt_weekday = row("b", "c", text="Recusaram na segunda-feira passada.")
    zero = row("c", "c", text="Uma tarifa de 263.510 pesos argentinos.", claim={"amount": 263510.0})
    grouped = row("d", "c", text="Un cargo de 1.215.340 pesos.", claim={"amount": 1215340})
    cents = row("e", "c", text="Un cargo de 8.207,59 dólares.", claim={"amount": 8207.59})
    other_format = row("f", "c", text="A charge of 263,510 pesos.", claim={"amount": 263510})
    assert fa.patterns_of(weekday) == {"relative_weekday"}
    assert fa.patterns_of(pt_weekday) == {"relative_weekday"}
    assert fa.patterns_of(zero) == {"dot_grouped_amount", "dot_grouped_one_group_zero"}
    assert fa.patterns_of(grouped) == {"dot_grouped_amount"}
    assert fa.patterns_of(cents) == set()
    assert fa.patterns_of(other_format) == set()


def test_markdown_states_the_rule_and_every_view():
    verdicts, rows, profiles = mixed_world()
    md = fa.to_markdown(fa.build(results(verdicts), rows, profiles, boot=50))
    for label in ("Country of the customer", "Customer segment", "Language variant of the scenario text",
                  "Category mix", "The failed scenarios", "Input patterns behind the failures",
                  "sample too small to conclude", "fewer than 20 scenarios", "not production results"):
        assert label in md


def test_language_and_country_are_crossed_with_totals():
    verdicts, rows, profiles = mixed_world()
    rows["mx-ok-000"]["language"] = "pt"
    for sid in ("co-ok-000", "co-ok-001"):
        rows[sid]["language"] = "pt"
        rows[sid]["variant"] = "mixed"
    out = fa.build(results(verdicts), rows, profiles, boot=50)
    lbc = out["language_by_country"]
    assert lbc["pt"]["variants"]["mixed"] == {"MX": 0, "CO": 2, "AR": 0}
    assert lbc["pt"]["variants"]["es-MX"] == {"MX": 1, "CO": 0, "AR": 0}
    assert lbc["pt"]["all"] == {"MX": 1, "CO": 2, "AR": 0}
    assert sum(sum(b["all"].values()) for b in lbc.values()) == len(verdicts)
    assert "| pt | all | 1 | 2 | 0 | 3 |" in fa.to_markdown(out)
