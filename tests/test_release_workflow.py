import unittest
from pathlib import Path


class ReleaseWorkflowTests(unittest.TestCase):
    def read_workflow(self):
        return (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "release-builds.yml"
        ).read_text(encoding="utf-8")

    def test_manual_release_has_optional_version_and_title_inputs(self):
        workflow = self.read_workflow()
        self.assertIn("  workflow_dispatch:", workflow)
        self.assertIn("      version:", workflow)
        self.assertIn("      release_title:", workflow)
        self.assertIn("REQUESTED_VERSION: ${{ inputs.version || '' }}", workflow)
        self.assertIn("REQUESTED_TITLE: ${{ inputs.release_title || '' }}", workflow)
        self.assertIn("if requested_version:", workflow)
        self.assertIn("version = requested_version", workflow)

    def test_push_commit_can_request_a_numbered_release(self):
        workflow = self.read_workflow()
        self.assertIn("RELEASE_COMMIT_MESSAGE: ${{ github.event.head_commit.message || '' }}", workflow)
        self.assertIn('r"Release:\\s*v?', workflow)
        self.assertIn("release_requested = True", workflow)
        self.assertIn("release_title = release_match.group(2).strip()", workflow)
        self.assertIn("release_title: ${{ steps.version.outputs.release_title }}", workflow)
        self.assertIn("RELEASE_TITLE: ${{ needs.prepare.outputs.release_title }}", workflow)
        self.assertIn("if: needs.prepare.outputs.release_version != ''", workflow)
        self.assertIn('--title "$RELEASE_TITLE"', workflow)

    def test_release_version_requires_semver_and_must_increase(self):
        workflow = self.read_workflow()
        self.assertIn(
            're.fullmatch(r"(0|[1-9]\\d*)\\.(0|[1-9]\\d*)\\.(0|[1-9]\\d*)", requested_version)',
            workflow,
        )
        self.assertIn("if selected <= latest:", workflow)
        self.assertIn("version = f\"{latest[0]}.{latest[1]}.{latest[2] + 1}\"", workflow)


if __name__ == "__main__":
    unittest.main()
