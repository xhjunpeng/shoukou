"""Mixed task changes must be separated conservatively during daily closeout."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SKILL = (ROOT / "SKILL.md").read_text()
POLICY = (ROOT / "references/mixed-changes.md").read_text()
PROMPT = (ROOT / "agents/openai.yaml").read_text()


class MixedChangesPolicyTests(unittest.TestCase):
    def test_daily_flow_reads_mixed_change_policy(self):
        self.assertIn("references/mixed-changes.md", SKILL)

    def test_independent_changes_must_be_saved_first(self):
        self.assertIn("独立成果必须优先单独保存", SKILL)
        self.assertIn("必须优先单独保存", POLICY)

    def test_mixed_file_requires_a_bounded_split_attempt(self):
        self.assertIn("必须先尝试用独立临时索引和补丁分块保存", SKILL)
        self.assertIn("必须先做一次分块拆分尝试", POLICY)

    def test_temporary_index_preserves_the_real_index(self):
        self.assertIn("独立临时索引", POLICY)
        self.assertIn("不改用户现有暂存区", POLICY)
        self.assertIn("不要直接对用户现有索引运行", POLICY)

    def test_incomplete_acceptance_does_not_block_safe_local_save(self):
        self.assertIn("测试未完成不阻止安全的本地保存", SKILL)
        self.assertIn("代码可以先安全保存为本地提交", POLICY)

    def test_inseparable_hunk_does_not_block_other_files(self):
        self.assertIn("仍应保存其他独立文件", POLICY)

    def test_other_task_changes_are_preserved(self):
        self.assertIn("其他任务的暂存和未提交成果保持原样", SKILL)
        self.assertIn("保持索引原样", POLICY)

    def test_saved_tested_merged_pushed_are_separate_states(self):
        self.assertIn("saved, tested, merged, pushed, and deployed", PROMPT)
        for word in ("已单独保存", "已通过", "合并", "推送", "上线"):
            self.assertIn(word, POLICY)


if __name__ == "__main__":
    unittest.main()
