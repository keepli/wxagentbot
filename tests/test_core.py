import dataclasses
import tempfile
import unittest
from pathlib import Path

from wxagentbot.config import ConfigError, Conversation, Settings, load_settings
from wxagentbot.domain import HistoryGap, Message, context_key, new_tail, split_reply, task_prompt
from wxagentbot.store import Conflict, Store


def route(**kwargs):
    values = dict(id="alice", name="Alice", kind="private", workspace=Path("/tmp/alice"), enabled=True, allow_push=True)
    values.update(kwargs)
    return Conversation(**values)


class RoutingTests(unittest.TestCase):
    def test_private_incoming_only(self):
        self.assertEqual(task_prompt(route(), Message("任务", "Alice", "incoming")), "任务")
        for direction in ("outgoing", "unknown", "system"):
            self.assertIsNone(task_prompt(route(), Message("任务", "Alice", direction)))

    def test_disabled_and_media_are_ignored(self):
        self.assertIsNone(task_prompt(route(enabled=False), Message("任务", "Alice", "incoming")))
        self.assertIsNone(task_prompt(route(), Message("[图片]", "Alice", "incoming", "media")))

    def test_group_requires_verified_mention(self):
        r = route(kind="group", self_names=("小明",))
        self.assertIsNone(task_prompt(r, Message("@小明 帮忙", "张三", "incoming")))
        m = Message("@小明 帮忙", "张三", "incoming", mentions=("小明",), mention_verified=True)
        self.assertEqual(task_prompt(r, m), "帮忙")
        self.assertIsNone(task_prompt(r, dataclasses.replace(m, mentions=("其他人",))))

    def test_group_sender_required_and_allowlist(self):
        r = route(kind="group", self_names=("我",), mention_mode="text", allowed_senders=("Alice",))
        for sender in ("", "Bob"):
            self.assertIsNone(task_prompt(r, Message("@我 任务", sender, "incoming")))
        self.assertEqual(task_prompt(r, Message("@我 任务", "Alice", "incoming")), "任务")

    def test_text_mention_has_name_boundary(self):
        r = route(kind="group", self_names=("小明",), mention_mode="text")
        self.assertIsNone(task_prompt(r, Message("@小明同学 任务", "Alice", "incoming")))
        self.assertEqual(task_prompt(r, Message("@小明\u2005任务", "Alice", "incoming")), "任务")

    def test_unidentified_group_sender_can_use_isolated_tasks(self):
        r = route(kind="group", self_names=("小小🤖",), mention_mode="text", missing_sender_policy="isolated")
        self.assertEqual(task_prompt(r, Message("@小小🤖\u2005你好", direction="incoming")), "你好")
        for message in (Message("普通聊天", direction="incoming"),
                        Message("@小小🤖\u2005", direction="incoming"),
                        Message("@小小🤖 你好", direction="outgoing"),
                        Message("@小小🤖 你好"),
                        Message("@小小🤖 你好", direction="incoming", kind="media")):
            self.assertIsNone(task_prompt(r, message))
        restricted = dataclasses.replace(r, allowed_senders=("Alice",))
        self.assertIsNone(task_prompt(restricted, Message("@小小🤖 你好", direction="incoming")))
        self.assertEqual(task_prompt(restricted, Message("@小小🤖 你好", "Alice", "incoming")), "你好")

    def test_unidentified_sender_needs_a_unique_event_for_context(self):
        r = route(kind="group", missing_sender_policy="isolated")
        m = Message("任务", direction="incoming")
        self.assertNotEqual(context_key(r, m, event_key="g:1"), context_key(r, m, event_key="g:2"))
        with self.assertRaises(ValueError):
            context_key(r, m)
        with self.assertRaises(ValueError):
            context_key(dataclasses.replace(r, missing_sender_policy="ignore"), m, event_key="g:1")

    def test_context_isolated_by_group_sender(self):
        r = route(kind="group")
        self.assertNotEqual(context_key(r, Message("x", "A")), context_key(r, Message("x", "B")))

    def test_same_text_is_not_collapsed(self):
        self.assertEqual(new_tail(["A"], ["A", "A"]), [1])
        self.assertEqual(new_tail(["A", "A"], ["A", "A", "A"]), [2])

    def test_scroll_alignment_and_history_prepend(self):
        self.assertEqual(new_tail(["A", "B"], ["B", "C"]), [1])
        self.assertEqual(new_tail(["A", "B"], ["old", "A", "B", "C"]), [3])
        self.assertEqual(new_tail(["A", "B"], ["old", "A", "B"]), [])

    def test_gap_is_not_replayed(self):
        with self.assertRaises(HistoryGap):
            new_tail(["old"], ["unrelated", "history"])

    def test_split_preserves_text_and_limits(self):
        text = "你好\n" * 1500
        chunks = split_reply(text, 100)
        self.assertEqual("".join(chunks), text.strip())
        self.assertTrue(all(0 < len(c) <= 100 for c in chunks))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.r = route()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_baseline_restart_duplicates_and_own_reply(self):
        a = Message("一样", "Alice", "incoming")
        self.assertEqual(self.store.ingest(self.r, [a])[0], 0)
        self.assertEqual(self.store.ingest(self.r, [a, a])[0], 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.ingest(self.r, [a, a])[0], 0)
        self.assertEqual(self.store.ingest(self.r, [a, a, Message("回复", "我", "outgoing")])[0], 0)
        self.assertEqual(len(self.store.recent("tasks")), 1)

    def test_gap_updates_baseline_without_task(self):
        self.store.ingest(self.r, [Message("A")])
        count, warning = self.store.ingest(self.r, [Message("Z", "Alice", "incoming")])
        self.assertEqual(count, 0)
        self.assertTrue(warning)
        self.assertEqual(self.store.ingest(self.r, [Message("Z", "Alice", "incoming")])[0], 0)

    def test_identical_unidentified_mentions_create_separate_tasks_and_deduplicate(self):
        group = dataclasses.replace(self.r, kind="group", self_names=("我",),
                                    mention_mode="text", missing_sender_policy="isolated")
        history = Message("历史", direction="incoming")
        mention = Message("@我 你好", direction="incoming")
        self.store.ingest(group, [history])
        self.assertEqual(self.store.ingest(group, [history, mention, mention])[0], 2)
        tasks = self.store.recent("tasks")
        self.assertEqual(len({t["context"] for t in tasks}), 2)
        self.assertTrue(all(t["sender"] == "" and t["prompt"] == "你好" for t in tasks))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.ingest(group, [history, mention, mention])[0], 0)

    def test_empty_reload_does_not_erase_cursor(self):
        old = Message("A", "Alice", "incoming")
        self.store.ingest(self.r, [old])
        self.store.ingest(self.r, [])
        self.assertEqual(self.store.ingest(self.r, [old, old])[0], 1)

    def test_idempotency_and_conflicting_retries(self):
        first = self.store.enqueue(self.r, "推送", 100, "key")
        self.assertEqual(first["id"], self.store.enqueue(self.r, "推送", 100, "key")["id"])
        with self.assertRaises(Conflict):
            self.store.enqueue(self.r, "别的推送", 100, "key")
        with self.assertRaises(Conflict):
            self.store.enqueue(route(id="bob", name="Bob"), "推送", 100, "key")

    def test_interrupted_tasks_and_sends_are_not_retried(self):
        a = Message("A", "Alice", "incoming")
        self.store.ingest(self.r, [a])
        self.store.ingest(self.r, [a, a])
        task = self.store.claim("tasks")
        push = self.store.enqueue(self.r, "out", 100)
        self.store.claim("deliveries")
        self.store.recover()
        self.assertEqual(self.store.get("tasks", task["id"])["status"], "interrupted")
        self.assertEqual(self.store.get("deliveries", push["id"])["status"], "unknown")
        self.assertIsNone(self.store.claim("deliveries"))

    def test_partial_send_resumes_only_unsent_chunks(self):
        row = self.store.enqueue(self.r, "abcdef", 3)
        self.store.claim("deliveries")
        self.store.part_sent(row["id"])
        next_row = self.store.claim("deliveries")
        self.assertEqual(next_row["next_part"], 1)
        self.store.part_sent(row["id"])
        self.assertEqual(self.store.get("deliveries", row["id"])["status"], "sent")

    def test_retry_reuses_saved_result_and_refuses_unknown_delivery(self):
        row = self.store.enqueue(self.r, "已生成的回复", 100)
        self.store.delivery_error(row["id"], "failed", "未发送")
        retried = self.store.retry_delivery(row["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["content"], "已生成的回复")
        self.assertEqual(len(self.store.recent("deliveries")), 1)
        self.assertEqual(len(self.store.recent("tasks")), 0)
        self.store.delivery_error(row["id"], "unknown", "可能已发送")
        with self.assertRaises(Conflict):
            self.store.retry_delivery(row["id"])

    def test_retry_after_restart_is_durable_and_only_retries_requested_failures(self):
        first = self.store.enqueue(self.r, "已有回复", 100)
        other = self.store.enqueue(self.r, "其他失败", 100)
        for row in (first, other):
            self.store.delivery_error(row["id"], "failed", "未点击")
        self.store.retry_delivery(first["id"], after_restart=True)
        self.store.retry_delivery(first["id"], after_restart=True)
        self.assertIsNone(self.store.claim("deliveries"))
        self.store.close()
        self.store = Store(self.path)
        self.store.recover()
        retried = self.store.claim("deliveries")
        self.assertEqual(retried["id"], first["id"])
        self.assertEqual(retried["content"], "已有回复")
        self.assertEqual(self.store.get("deliveries", other["id"])["status"], "failed")
        self.assertEqual(len(self.store.recent("tasks")), 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM delivery_retries").fetchone()[0], 0)

    def test_delayed_retry_is_skipped_if_status_becomes_uncertain_or_sent(self):
        for status in ("unknown", "sent"):
            row = self.store.enqueue(self.r, status, 100)
            self.store.delivery_error(row["id"], "failed", "未点击")
            self.store.retry_delivery(row["id"], after_restart=True)
            self.store.delivery_error(row["id"], status, "已核对")
        self.store.recover()
        self.assertIsNone(self.store.claim("deliveries"))

    def test_uncertain_delivery_cannot_be_staged_for_restart(self):
        row = self.store.enqueue(self.r, "不确定", 100)
        self.store.delivery_error(row["id"], "unknown", "可能已发")
        with self.assertRaises(Conflict):
            self.store.retry_delivery(row["id"], after_restart=True)

    def test_reset_uses_literal_ids(self):
        self.store.save_thread("a_b:policy", "one")
        self.store.save_thread("axb:policy", "two")
        self.store.reset("a_b")
        self.assertIsNone(self.store.thread_id("a_b:policy"))
        self.assertEqual(self.store.thread_id("axb:policy"), "two")


class ConfigTests(unittest.TestCase):
    def write_config(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.yml"
        path.write_text(text)
        return path

    def test_example_is_valid_and_inactive(self):
        s = load_settings(Path(__file__).parents[1] / "config.example.yml")
        self.assertTrue(s.conversations)
        self.assertFalse(any(c.enabled or c.allow_push for c in s.conversations))

    def test_alias_collision_rejected(self):
        p = self.write_config('conversations:\n  - id: a\n    name: b\n    kind: private\n  - id: b\n    name: c\n    kind: private\n')
        with self.assertRaises(ConfigError):
            load_settings(p)

    def test_overlapping_workspaces_rejected(self):
        p = self.write_config('conversations:\n  - id: a\n    name: A\n    kind: private\n    enabled: true\n    workspace: work\n  - id: b\n    name: B\n    kind: private\n    enabled: true\n    workspace: work/sub\n')
        with self.assertRaises(ConfigError):
            load_settings(p)

    def test_invalid_group_and_unknown_options(self):
        for text in ('codex:\n  provider: claude\n', 'conversations:\n  - id: g\n    name: G\n    kind: group\n    enabled: true\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))

    def test_invalid_missing_sender_policy_is_rejected(self):
        for value in ('"shared"', '3'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(self.write_config('conversations:\n  - id: g\n    name: G\n    kind: group\n    missing_sender_policy: ' + value + '\n'))

    def test_invalid_listen_mode_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_settings(self.write_config('wechat:\n  listen_mode: preview\n'))

    def test_invalid_prompt_file_or_reply_format_is_rejected(self):
        for text in ('codex:\n  instructions_file: missing.txt\n',
                     'codex:\n  instructions_file: 3\n', 'codex:\n  reply_format: html\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))

    def test_noninteger_chunk_size_and_string_aliases_rejected(self):
        for text in ('wechat:\n  max_reply_chars: 100.5\n', 'conversations:\n  - id: g\n    name: G\n    kind: group\n    self_names: Alice\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))

    def test_yaml_syntax_duplicate_keys_and_unsafe_tags_rejected(self):
        for text in ('wechat: [broken\n', 'server: {}\nserver: {}\n',
                     'server:\n  port: 8765\n  port: 9000\n',
                     'codex: !!python/object/apply:builtins.str [unsafe]\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))

    def test_yaml_structure_is_validated(self):
        for text in ('- server\n', 'just text\n', 'conversations: {}\n',
                     'conversations: [invalid]\n', '3: invalid\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))

    def test_yaml_relative_paths_and_multiline_instructions(self):
        path = self.write_config('server:\n  token_file: secrets/token\nstorage:\n  database: state/db\ncodex:\n  instructions: |\n    用纯文本回答。\n    保留换行。\nconversations:\n  - id: friend\n    name: 好友\n    kind: private\n    workspace: work/friend\n')
        settings = load_settings(path)
        self.assertEqual(settings.token_file, path.parent / 'secrets/token')
        self.assertEqual(settings.database, path.parent / 'state/db')
        self.assertEqual(settings.conversations[0].workspace, path.parent / 'work/friend')
        self.assertEqual(settings.instructions, '用纯文本回答。\n保留换行。\n')

    def test_yaml_extension_is_supported_too(self):
        path = self.write_config('server:\n  port: 9000\n')
        renamed = path.rename(path.with_suffix('.yaml'))
        self.assertEqual(load_settings(renamed).port, 9000)
