import unittest
from pathlib import Path


class ReleaseWorkflowTests(unittest.TestCase):
    def test_manual_release_has_optional_version_input(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "release-builds.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertIn("      version:", workflow)
        self.assertIn("REQUESTED_VERSION: ${{ inputs.version }}", workflow)
        self.assertIn("if requested:", workflow)
        self.assertIn("version = requested", workflow)

    def test_release_version_requires_semver_and_must_increase(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "release-builds.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(
            're.fullmatch(r"(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)", requested)',
            workflow,
        )
        self.assertIn("if selected <= latest:", workflow)
        self.assertIn("version = f\"{latest[0]}.{latest[1]}.{latest[2] + 1}\"", workflow)


if __name__ == "__main__":
    unittest.main()
