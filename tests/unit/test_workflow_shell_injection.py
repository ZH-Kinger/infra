import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOWS = (
    ROOT / ".github/workflows/dataset-release.yml",
    ROOT / ".github/workflows/_delivery.yml",
)


def _run_blocks(path: Path) -> list[str]:
    """Return the literal contents of every GitHub Actions ``run: |`` block."""
    lines = path.read_text(encoding="utf-8").splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        match = re.match(r"^(\s*)run:\s*\|\s*$", lines[index])
        if not match:
            index += 1
            continue
        base_indent = len(match.group(1))
        index += 1
        block: list[str] = []
        while index < len(lines):
            line = lines[index]
            if line.strip() and len(line) - len(line.lstrip()) <= base_indent:
                break
            block.append(line)
            index += 1
        blocks.append("\n".join(block))
    return blocks


def test_dispatch_inputs_are_not_interpolated_inside_shell_blocks():
    for workflow in WORKFLOWS:
        shell = "\n".join(_run_blocks(workflow))
        assert "${{ inputs." not in shell, workflow


def test_shell_input_references_have_a_workflow_env_bridge():
    for workflow in WORKFLOWS:
        text = workflow.read_text(encoding="utf-8")
        env_names = set(re.findall(r"^  (INPUT_[A-Z_]+): \$\{\{ inputs\.", text, re.M))
        shell = "\n".join(_run_blocks(workflow))
        used_names = set(re.findall(r"\$\{(INPUT_[A-Z_]+)\}", shell))
        assert used_names <= env_names, (workflow, used_names - env_names)


def test_dataset_workflow_keeps_user_values_quoted_after_env_expansion():
    shell = "\n".join(_run_blocks(ROOT / ".github/workflows/dataset-release.yml"))
    # A quoted environment expansion is data. An unquoted expansion would let
    # whitespace/globbing alter the command even after removing expressions.
    for line in shell.splitlines():
        without_double_quoted = re.sub(r'"(?:\\.|[^"\\])*"', "", line)
        assert not re.search(r"\$\{INPUT_[A-Z_]+\}", without_double_quoted), line
