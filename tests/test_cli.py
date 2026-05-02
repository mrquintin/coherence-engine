"""Tests for the CLI interface."""

import subprocess
import sys
import os
import json

ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def run_cli(*args, input_text=None):
    """Run coherence_engine as a subprocess and capture output."""
    cmd = [sys.executable, "-m", "coherence_engine"] + list(args)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        input=input_text,
        cwd=ENGINE_DIR,
        timeout=120,
    )
    return result


class TestVersionCommand:
    def test_version_output(self):
        result = run_cli("version")
        assert result.returncode == 0
        assert "Coherence Engine" in result.stdout
        assert "v2.0.0" in result.stdout

    def test_dependency_listing(self):
        result = run_cli("version")
        assert "sentence-transformers" in result.stdout
        assert "transformers" in result.stdout


class TestLayersCommand:
    def test_layers_output(self):
        result = run_cli("layers")
        assert result.returncode == 0
        assert "Contradiction" in result.stdout
        assert "Argumentation" in result.stdout
        assert "Embedding" in result.stdout
        assert "Compression" in result.stdout
        assert "Structural" in result.stdout


class TestAnalyzeCommand:
    def test_analyze_inline_text(self):
        result = run_cli(
            "analyze",
            "The economy is growing. Employment is rising. Therefore we conclude things are improving.",
        )
        assert result.returncode == 0
        assert "Composite Score" in result.stdout

    def test_analyze_file(self):
        path = os.path.join(FIXTURES, "coherent_essay.txt")
        result = run_cli("analyze", path)
        assert result.returncode == 0
        assert "Composite Score" in result.stdout

    def test_analyze_json_format(self):
        result = run_cli(
            "analyze",
            "Point one is valid. Point two supports it. Thus the conclusion follows.",
            "--format", "json",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "composite_score" in data

    def test_analyze_markdown_format(self):
        result = run_cli(
            "analyze",
            "The argument is sound. Evidence backs it up. Therefore the conclusion holds.",
            "--format", "markdown",
        )
        assert result.returncode == 0
        assert "# Coherence Engine" in result.stdout

    def test_analyze_stdin(self):
        result = run_cli("analyze", input_text="Stdin test sentence one. Stdin test sentence two has more words.")
        assert result.returncode == 0

    def test_analyze_empty_fails(self):
        result = run_cli("analyze", "")
        assert result.returncode != 0

    def test_custom_weights(self):
        result = run_cli(
            "analyze",
            "First point is clear. Second point follows. Thus the conclusion.",
            "--weights", "0.40,0.15,0.15,0.15,0.15",
        )
        assert result.returncode == 0

    def test_analyze_force_parallel_delegation_json(self):
        result = run_cli(
            "analyze",
            (
                "Section one has detailed requirements and assumptions.\n\n"
                "Section two adds dependencies and execution constraints.\n\n"
                "Section three defines acceptance criteria and rollout strategy."
            ),
            "--format", "json",
            "--force-parallel", "2",
            "--agent-list", "planner,builder",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "delegation" in data
        assert "runs" in data
        assert data["parallel_agents_used"] == 2


class TestDelegateCommand:
    def test_delegate_json_output(self):
        result = run_cli(
            "delegate",
            (
                "Section one has details and constraints.\n\n"
                "Section two has implementation requirements and dependencies.\n\n"
                "Section three defines acceptance criteria and rollout notes."
            ),
            "--format", "json",
            "--force-parallel", "2",
            "--agent-list", "planner,builder",
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "delegation" in data
        assert "runs" in data
        assert data["parallel_agents_used"] == 2


class TestNoCommand:
    def test_no_command_shows_help(self):
        result = run_cli()
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "help" in result.stdout.lower()
