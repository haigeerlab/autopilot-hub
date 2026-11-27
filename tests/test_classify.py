"""Classify CI failures from workflow run and job payloads."""
import unittest

from orchestrator.classify import classify_failure, classify_rerun


def step(name, number, conclusion):
    return {"name": name, "number": number, "conclusion": conclusion}


def job(*steps, name="CI", conclusion="failure"):
    return {"name": name, "conclusion": conclusion, "steps": list(steps)}


class CiFailureTest(unittest.TestCase):
    def setUp(self):
        self.run_payload = {"conclusion": "failure", "run_attempt": 1}

    def category(self, *jobs):
        category, reason = classify_failure(self.run_payload, list(jobs))
        self.assertTrue(reason)
        return category

    def test_setup_failure_before_first_test_is_env(self):
        self.assertEqual(self.category(job(
            step("Set up Python", 1, "failure"),
            step("Unit tests", 2, "skipped"),
        )), "env")

    def test_startup_failure_is_config(self):
        self.run_payload["conclusion"] = "startup_failure"
        self.assertEqual(self.category(), "config")

    def test_failure_in_test_step_is_test(self):
        self.assertEqual(self.category(job(
            step("Set up Python", 1, "success"),
            step("Run pytest", 2, "failure"),
        )), "test")

    def test_unclear_failure_is_test(self):
        for failed_job in (
            job(step("Build artifacts", 1, "failure")),
            job(step("Run tests", 1, "success")),
            job({"name": "Setup", "conclusion": "failure"},
                step("Run tests", 2, "skipped")),
            {"name": "CI", "conclusion": "failure"},
        ):
            with self.subTest(job=failed_job):
                self.assertEqual(self.category(failed_job), "test")
        self.assertEqual(self.category(), "test")

    def test_failed_job_with_empty_steps_is_config(self):
        self.assertEqual(self.category(job()), "config")

    def test_explicit_workflow_and_action_errors_are_config(self):
        for error in ("Invalid workflow file: ci.yml", "Workflow parsing failed",
                      "Unable to resolve action actions/setup-python@missing",
                      "Action actions/setup-python@missing not found"):
            with self.subTest(error=error):
                self.assertEqual(self.category(job(
                    step(error, 1, "failure"),
                    step("Run tests", 2, "skipped"),
                )), "config")

    def test_explicit_config_error_can_appear_in_run_or_job_name(self):
        self.assertEqual(self.category(job(step("Setup", 1, "failure"),
                                           name="Unable to find action helper@v1")), "config")
        self.run_payload["name"] = "Invalid workflow file: ci.yml"
        self.assertEqual(self.category(), "config")

    def test_missing_test_fixture_is_not_a_missing_action(self):
        self.assertEqual(self.category(job(
            step("Action integration test: fixture not found", 1, "failure"),
        )), "test")

    def test_any_test_job_wins_regardless_of_order(self):
        env_job = job(step("Setup", 1, "failure"), step("Tests", 2, "skipped"),
                      name="Linux")
        config_job = job(name="macOS")
        test_job = job(step("Tests", 1, "failure"), name="Windows")
        for jobs in ((env_job, config_job, test_job), (test_job, config_job, env_job)):
            with self.subTest(jobs=jobs):
                self.assertEqual(self.category(*jobs), "test")

    def test_config_job_wins_over_env_job(self):
        env_job = job(step("Setup", 1, "failure"), step("Tests", 2, "skipped"))
        self.assertEqual(self.category(env_job, job()), "config")

    def test_successful_and_cancelled_jobs_do_not_affect_env(self):
        env_job = job(step("Setup", 1, "failure"), step("Tests", 2, "cancelled"))
        self.assertEqual(self.category(job(conclusion="success"), env_job,
                                       job(conclusion="cancelled")), "env")

    def test_test_step_matching_ignores_case_and_accepts_script_names(self):
        for name in ("TEST", "PyTeSt", "UNITTEST", "VaLiDaTe", "CHECK",
                     "Run ./SCRIPTS/validate.sh", "Run ./scripts/run_tests.sh", "回归测试"):
            with self.subTest(name=name):
                self.assertEqual(self.category(job(
                    step("Setup", 1, "failure"), step(name, 2, "skipped"),
                )), "env")

    def test_checkout_is_a_setup_step(self):
        for name in ("Run actions/checkout@v4", "Check out repository"):
            with self.subTest(name=name):
                self.assertEqual(self.category(job(
                    step(name, 1, "failure"), step("Tests", 2, "skipped"),
                )), "env")

    def test_step_number_controls_order(self):
        self.assertEqual(self.category(job(
            step("Tests", 2, "skipped"), step("Setup", 1, "failure"),
        )), "env")

    def test_failure_after_first_test_is_test(self):
        self.assertEqual(self.category(job(
            step("Unit tests", 1, "success"), step("Build", 2, "failure"),
            step("Integration tests", 3, "skipped"),
        )), "test")

    def test_successful_rerun_is_env_for_recording(self):
        rerun = {"conclusion": "success", "run_attempt": 2}
        category, reason = classify_rerun(rerun)
        self.assertEqual(category, "env")
        self.assertIn("重跑", reason)
        self.assertEqual(classify_failure(rerun, [])[0], "test")

    def test_first_attempt_or_unsuccessful_rerun_has_no_record(self):
        for run in ({"conclusion": "success", "run_attempt": 1},
                    {"conclusion": "failure", "run_attempt": 2},
                    {"conclusion": "success"}):
            with self.subTest(run=run):
                self.assertIsNone(classify_rerun(run))
