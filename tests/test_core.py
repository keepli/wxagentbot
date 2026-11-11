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

    def test_unidentified_group_sender_can_use_shared_context(self):
        r = route(kind="group", self_names=("小小🤖",), mention_mode="text", missing_sender_policy="shared")
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

    def test_unidentified_sender_uses_group_context_independent_of_event(self):
        r = route(kind="group", missing_sender_policy="shared")
        m = Message("任务", direction="incoming")
        self.assertEqual(context_key(r, m, event_key="g:1"), r.id)
        self.assertEqual(context_key(r, m, event_key="g:2"), r.id)
        self.assertEqual(context_key(r, m), r.id)

    def test_context_shared_by_group_members_but_separate_between_groups(self):
        r = route(kind="group")
        self.assertEqual(context_key(r, Message("x", "A")), context_key(r, Message("x", "B")))
        other = dataclasses.replace(r, id="other")
        self.assertNotEqual(context_key(r, Message("x", "A")), context_key(other, Message("x", "A")))

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
                                    mention_mode="text", missing_sender_policy="shared")
        history = Message("历史", direction="incoming")
        mention = Message("@我 你好", direction="incoming")
        self.store.ingest(group, [history])
        self.assertEqual(self.store.ingest(group, [history, mention, mention])[0], 2)
        tasks = self.store.recent("tasks")
        self.assertEqual({t["context"] for t in tasks}, {group.id})
        self.assertEqual(len({t["event_key"] for t in tasks}), 2)
        self.assertTrue(all(t["sender"] == "" and t["prompt"] == "你好" for t in tasks))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.ingest(group, [history, mention, mention])[0], 0)

    def group(self, **changes):
        return dataclasses.replace(self.r, kind="group", self_names=("小小🤖",),
                                   mention_mode="text", missing_sender_policy="shared", **changes)

    def test_direction_change_of_old_reply_does_not_swallow_first_mention(self):
        group = self.group()
        reply = Message("上一条长回复", direction="outgoing")
        mention = Message("@小小🤖\u2005骨王有新作消息吗", direction="incoming")
        self.store.ingest(group, [reply])
        shifted = dataclasses.replace(reply, direction="incoming")
        self.assertEqual(self.store.ingest(group, [shifted, mention]), (1, None))
        self.assertEqual(self.store.ingest(group, [shifted, mention, mention]), (1, None))
        self.assertEqual(self.store.ingest(group, [reply, mention, mention]), (0, None))
        self.assertEqual([t["prompt"] for t in self.store.recent("tasks")], ["骨王有新作消息吗"] * 2)

    def test_pending_direction_survives_restart_and_runs_once_when_resolved(self):
        group = self.group()
        history = Message("历史", direction="outgoing")
        pending = Message("@小小🤖 帮忙")
        self.store.ingest(group, [history])
        self.assertEqual(self.store.ingest(group, [history, pending]), (0, None))
        self.store.close()
        self.store = Store(self.path)
        incoming = dataclasses.replace(pending, direction="incoming")
        self.assertEqual(self.store.ingest(group, [history, incoming]), (1, None))
        self.assertEqual(self.store.ingest(group, [history, incoming]), (0, None))
        task = self.store.recent("tasks")[0]
        self.assertEqual((task["event_key"], task["prompt"]), ("alice:1", "帮忙"))
        self.assertEqual(self.store.db.execute('SELECT seq FROM snapshots').fetchone()[0], 1)

    def test_two_identical_pending_rows_keep_separate_events(self):
        group = self.group()
        history, pending = Message("历史"), Message("@小小🤖 帮忙")
        incoming = dataclasses.replace(pending, direction="incoming")
        self.store.ingest(group, [history])
        self.store.ingest(group, [history, pending, pending])
        self.assertEqual(self.store.ingest(group, [history, incoming, pending])[0], 1)
        self.assertEqual(self.store.ingest(group, [history, incoming, incoming])[0], 1)
        self.assertEqual(self.store.ingest(group, [history, incoming, incoming])[0], 0)
        self.assertEqual({t['event_key'] for t in self.store.recent('tasks')}, {'alice:1', 'alice:2'})

    def test_pending_row_resolved_as_outgoing_never_becomes_a_task(self):
        group = self.group()
        history, pending = Message("历史"), Message("@小小🤖 帮忙")
        self.store.ingest(group, [history])
        self.store.ingest(group, [history, pending])
        self.assertEqual(self.store.ingest(group, [history, dataclasses.replace(pending, direction='outgoing')])[0], 0)
        # Subsequent visual misclassification must not turn an own reply into input.
        self.assertEqual(self.store.ingest(group, [history, dataclasses.replace(pending, direction='incoming')])[0], 0)
        self.assertFalse(self.store.recent('tasks'))

    def test_unknown_baseline_rows_are_not_replayed_when_they_become_incoming(self):
        group = self.group()
        old = Message("@小小🤖 历史任务")
        self.store.ingest(group, [old])
        self.assertEqual(self.store.ingest(group, [dataclasses.replace(old, direction='incoming')]), (0, None))

    def test_sender_and_mention_metadata_can_resolve_without_changing_identity(self):
        group = dataclasses.replace(self.group(), mention_mode='metadata', allowed_senders=('Alice',))
        history = Message("历史")
        pending = Message("任务", direction='incoming')
        self.store.ingest(group, [history])
        self.assertEqual(self.store.ingest(group, [history, pending])[0], 0)
        with_sender = dataclasses.replace(pending, sender='Alice')
        self.assertEqual(self.store.ingest(group, [history, with_sender])[0], 0)
        ready = dataclasses.replace(with_sender, mentions=('小小🤖',), mention_verified=True)
        self.assertEqual(self.store.ingest(group, [history, ready]), (1, None))
        self.assertEqual(self.store.ingest(group, [history, ready]), (0, None))
        self.assertEqual(self.store.recent('tasks')[0]['sender'], 'Alice')

    def test_content_anchor_is_stable_when_optional_metadata_changes(self):
        original = Message('正文')
        updated = dataclasses.replace(original, sender='Alice', direction='incoming',
                                      mentions=('我',), mention_verified=True)
        self.assertEqual(original.content_fingerprint(), updated.content_fingerprint())
        self.assertNotEqual(original.fingerprint(), updated.fingerprint())

    def test_legacy_snapshot_adopts_cursor_without_replaying_history(self):
        old = Message('旧消息', 'Alice', 'incoming')
        new = Message('新任务', 'Alice', 'incoming')
        import json
        with self.store.db:
            self.store.db.execute('INSERT INTO snapshots VALUES(?,?,?)',
                                  (self.r.id, json.dumps([old.fingerprint()]), 8))
        self.assertEqual(self.store.ingest(self.r, [old, new]), (1, None))
        self.assertEqual(self.store.recent('tasks')[0]['event_key'], 'alice:9')
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.ingest(self.r, [dataclasses.replace(old, direction='unknown'), new]), (0, None))

    def test_pending_messages_leaving_the_window_are_reported_without_replay(self):
        group = self.group()
        history, pending = Message('历史'), Message('@小小🤖 帮忙')
        self.store.ingest(group, [history])
        self.store.ingest(group, [history, pending])
        count, warning = self.store.ingest(group, [Message('新窗口')])
        self.assertEqual(count, 0)
        self.assertIn('待识别消息已移出', warning)
        self.assertFalse(self.store.recent('tasks'))

    def test_logs_message_content_and_decision_once_not_every_poll(self):
        group = self.group()
        history = Message('历史')
        pending = Message('@小小🤖 帮忙\n第二行')
        self.store.ingest(group, [history])
        with self.assertLogs('wxagentbot.store', level='INFO') as captured:
            self.store.ingest(group, [history, pending])
            self.store.ingest(group, [history, pending])
            incoming = dataclasses.replace(pending, direction='incoming')
            self.store.ingest(group, [history, incoming])
            self.store.ingest(group, [history, incoming])
        self.assertEqual(len(captured.output), 2)
        self.assertIn('微信新消息', captured.output[0])
        self.assertIn('收发方向待识别', captured.output[0])
        self.assertIn('@小小🤖 帮忙\\n第二行', captured.output[0])
        self.assertIn('微信消息识别更新', captured.output[1])
        self.assertIn('已入队', captured.output[1])

    def test_unmentioned_group_messages_are_logged_as_ignored(self):
        group = self.group()
        history = Message('历史')
        self.store.ingest(group, [history])
        with self.assertLogs('wxagentbot.store', level='INFO') as captured:
            self.assertEqual(self.store.ingest(group, [history, Message('普通聊天', direction='incoming')])[0], 0)
        self.assertIn('普通聊天', captured.output[0])
        self.assertIn('未提及本账号', captured.output[0])

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
        for value in ('"member"', '3'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(self.write_config('conversations:\n  - id: g\n    name: G\n    kind: group\n    missing_sender_policy: ' + value + '\n'))

    def test_shared_and_legacy_missing_sender_policies_are_accepted(self):
        for value in ('shared', 'isolated', 'ignore'):
            with self.subTest(value=value):
                s = load_settings(self.write_config('conversations:\n  - id: g\n    name: G\n    kind: group\n    missing_sender_policy: ' + value + '\n'))
                self.assertEqual(s.conversations[0].missing_sender_policy, value)

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

    def test_parallel_conversation_limit_requires_bounded_integer(self):
        for value in ('0', '17', 'true', '1.5', '"3"'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(self.write_config('codex:\n  max_parallel_conversations: ' + value + '\n'))
        settings = load_settings(self.write_config('codex:\n  max_parallel_conversations: 2\n'))
        self.assertEqual(settings.max_parallel_conversations, 2)

    def test_model_and_reasoning_effort_configuration(self):
        settings = load_settings(self.write_config('codex:\n  model: gpt-6-luna\n  reasoning_effort: medium\n'))
        self.assertEqual(settings.model, 'gpt-6-luna')
        self.assertEqual(settings.reasoning_effort, 'medium')
        self.assertIsNone(load_settings(self.write_config('codex: {}\n')).reasoning_effort)
        for value in ('null', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'):
            with self.subTest(value=value):
                settings = load_settings(self.write_config('codex:\n  reasoning_effort: ' + value + '\n'))
                self.assertEqual(settings.reasoning_effort, None if value == 'null' else value)

    def test_invalid_reasoning_effort_is_rejected(self):
        for value in ('""', '"Medium"', '"中等"', '"automatic"', 'true', '3', '[]', '{}'):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_settings(self.write_config('codex:\n  reasoning_effort: ' + value + '\n'))

    def test_scheduler_timezone_and_polling_configuration(self):
        settings = load_settings(self.write_config('scheduler:\n  timezone: UTC\n  poll_seconds: 2\n  misfire_grace_seconds: 60\n'))
        self.assertEqual(settings.schedule_timezone, 'UTC')
        self.assertEqual(settings.schedule_poll_seconds, 2)
        self.assertEqual(settings.schedule_grace_seconds, 60)
        for text in ('scheduler:\n  timezone: Not/A_Timezone\n', 'scheduler:\n  timezone: 3\n',
                     'scheduler:\n  poll_seconds: 0\n', 'scheduler:\n  misfire_grace_seconds: "300"\n',
                     'scheduler:\n  unknown: true\n', 'scheduler: []\n'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_settings(self.write_config(text))
