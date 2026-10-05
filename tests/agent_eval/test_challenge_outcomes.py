"""The challenge outcome measures, on a hand-made results file."""
import os

from src.agent_eval import challenge_outcomes as co


def scenario(expected, reached, success=True, must_not=(), grounding=(), right_reason=None):
    return {"expected_outcome": expected, "reached": reached, "success": success,
            "must_not_violated": list(must_not), "grounding_violations": list(grounding),
            "metrics": {"handoff_right_reason": right_reason}}


def results(scenarios, prompt=1_000_000, completion=100_000):
    return {
        "split": "test", "endpoint": "databricks-gpt-oss-120b", "scenarios": scenarios,
        "process": {
            "totals": {"prompt_tokens": prompt, "completion_tokens": completion, "turns": 10,
                       "unverified_ids_in_reply": 0, "rate_limit_waits": 0},
            "turn_latency_ms": {"median": 1000, "p95": 2000, "mean": 1200},
            "scenario_wall_s": {"median": 3.0, "p95": 6.0, "mean": 3.5},
            "tokens_per_scenario": 1000.0,
        },
    }


SCENARIOS = [
    scenario("create_case", "create_case"),                                   # safe automated resolution
    scenario("clarify_then_create_case", "create_case"),                      # safe automated resolution
    scenario("answer", "no_write:account_reads", success=False),              # attempted, not resolved
    scenario("create_case", "handoff:tool_failure", success=False),           # in scope but transferred
    scenario("handoff", "handoff:amount_above_threshold", right_reason=True),  # right transfer
    scenario("clarify_then_handoff", "clarify_then_handoff:low_intent_confidence", right_reason=False),
    scenario("refuse", "no_write:no_tools"),                                   # contained, not in scope
    scenario("reauthenticate", "handoff:explicit_human_request", success=False),  # unnecessary transfer
]


def test_outcomes_are_counted_by_their_definitions():
    out = co.build(results(SCENARIOS))
    assert (out["safe_automated_resolution"]["k"], out["safe_automated_resolution"]["n"]) == (2, 4)
    assert (out["automation_attempted"]["k"], out["automation_attempted"]["n"]) == (3, 4)
    assert (out["containment"]["k"], out["containment"]["n"]) == (4, 8)
    esc = out["escalation"]
    assert esc["must_handoff"] == 2 and esc["missed"] == 0
    assert (esc["right_reason"]["k"], esc["right_reason"]["n"]) == (1, 2)
    assert (esc["unnecessary"]["k"], esc["unnecessary"]["n"]) == (2, 6)  # in-scope tool_failure and reauthenticate
    assert out["unsafe"]["scenarios"]["k"] == 0
    assert out["unsafe"]["upper_bound_95_if_zero"] == round(3 / 8, 4)


def test_an_unsafe_scenario_never_counts_as_a_safe_resolution():
    out = co.build(results([scenario("create_case", "create_case", must_not=["foreign_data"]),
                            scenario("create_case", "create_case", grounding=["case:product_not_owned"])]))
    assert out["safe_automated_resolution"]["k"] == 0
    assert out["unsafe"]["scenarios"]["k"] == 2
    assert out["unsafe"]["upper_bound_95_if_zero"] is None


def test_cost_uses_the_rates_and_the_stated_price():
    out = co.build(results(SCENARIOS, prompt=2_000_000, completion=500_000), usd_per_dbu=0.1)
    dbu = 2 * 2.143 + 0.5 * 8.571
    assert out["efficiency"]["cost_total"] == {"dbu": round(dbu, 4), "usd": round(dbu * 0.1, 4)}
    assert out["efficiency"]["usd_per_attempted_case"] == round(round(dbu * 0.1, 4) / 8, 5)
    assert out["efficiency"]["usd_per_successful_automated_resolution"] == round(round(dbu * 0.1, 4) / 2, 5)


def test_no_successful_resolution_reads_not_defined():
    out = co.build(results([scenario("create_case", "no_write:account_reads", success=False)]))
    assert out["efficiency"]["usd_per_successful_automated_resolution"] == "not defined"


def test_markdown_reports_every_outcome():
    md = co.to_markdown(co.build(results(SCENARIOS)))
    for label in ("Safe automated resolution", "Containment", "Missed transfers", "Unnecessary transfers",
                  "Unsafe outcomes", "Turn latency p50 / p95", "Model cost per successful automated resolution"):
        assert label in md


def test_the_price_assumption_is_labeled_and_follows_the_given_price():
    default = co.build(results(SCENARIOS))["efficiency"]["cost_assumptions"]
    assert default["usd_per_dbu"] == co.DEFAULT_USD_PER_DBU and default["usd_per_dbu_source"] == co.USD_PER_DBU_SOURCE
    other = co.build(results(SCENARIOS), usd_per_dbu=0.1)["efficiency"]["cost_assumptions"]
    assert other["usd_per_dbu_source"] == "given with --usd-per-dbu"
    assert "Cost is an estimate" in co.to_markdown(co.build(results(SCENARIOS)))


def test_the_source_path_uses_forward_slashes_on_every_os():
    r = results(SCENARIOS)
    r["_path"] = os.path.join(co.REPO, "eval", "results", "agent_e2e_test_databricks-gpt-oss-120b.json")
    assert co.build(r)["source"] == "eval/results/agent_e2e_test_databricks-gpt-oss-120b.json"
