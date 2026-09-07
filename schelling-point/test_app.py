import csv
import json
import tempfile
import unittest
from pathlib import Path

import app


class SeedFastFollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.investigation = root / "investigation"
        self.task_dir = self.investigation / "tasks" / "fast-follow"
        (self.task_dir / "outputs").mkdir(parents=True)
        (self.investigation / "agent-logs" / "prowiki").mkdir(parents=True)

        with (self.task_dir / "outputs" / "observed_sequences.tsv").open(
            "w", encoding="utf-8", newline=""
        ) as target:
            writer = csv.DictWriter(target, fieldnames=["family"], delimiter="\t")
            writer.writeheader()
            writer.writerow({"family": "matching-family"})

        pages = [
            {"page_key": "page-b", "page_family": "matching-family"},
            {"page_key": "page-a", "page_family": "matching-family"},
            {"page_key": "page-c", "page_family": "matching-family"},
        ]
        self._write_jsonl("pages.jsonl", pages)
        self.board = app.Board(
            "schelling-point", root / "messages.db", root / "messages.json"
        )
        app.initialise_database(self.board)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_jsonl(self, name: str, rows: list[dict[str, object]]) -> None:
        path = self.investigation / "agent-logs" / "prowiki" / name
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    @staticmethod
    def _revision(page_key: str, body: str, minute: int) -> dict[str, object]:
        return {
            "page_key": page_key,
            "page_id": page_key,
            "rev_id": f"{page_key}@1",
            "body": body,
            "time": f"2026-01-01T00:{minute:02d}:00Z",
            "label": "writer",
        }

    def test_deduplicates_trimmed_transcript_text_before_counting(self) -> None:
        self._write_jsonl(
            "revisions.jsonl",
            [
                self._revision("page-b", "duplicate text\n", 1),
                self._revision("page-a", "duplicate text", 2),
                self._revision("page-c", "unique text", 3),
            ],
        )

        result = app.seed_fast_follow(self.board, self.task_dir, count=2)

        self.assertEqual(result, (2, 0, 0))
        messages = app.list_messages(self.board)
        self.assertEqual(len(messages), 2)
        self.assertEqual(sum("duplicate text" in row["body"] for row in messages), 1)
        self.assertTrue(any("page=page-a" in row["body"] for row in messages))
        self.assertTrue(any("unique text" in row["body"] for row in messages))

    def test_rerun_removes_a_previously_seeded_duplicate(self) -> None:
        self._write_jsonl(
            "revisions.jsonl",
            [
                self._revision("page-a", "first text", 1),
                self._revision("page-b", "second text", 2),
            ],
        )
        self.assertEqual(app.seed_fast_follow(self.board, self.task_dir), (2, 0, 0))

        self._write_jsonl(
            "revisions.jsonl",
            [
                self._revision("page-a", "same text", 1),
                self._revision("page-b", "same text", 2),
            ],
        )

        added, updated, deleted = app.seed_fast_follow(self.board, self.task_dir)
        self.assertEqual((added, updated, deleted), (0, 1, 1))
        self.assertEqual(len(app.list_messages(self.board)), 1)


if __name__ == "__main__":
    unittest.main()
