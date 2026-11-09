import unittest

from wxagentbot.formatting import wechat_plain_text


class PlainTextTests(unittest.TestCase):
    def test_weather_prose_heading_bold_bullets_and_links(self):
        text = "## **广州天气**\n- 明天 **13～20℃**\n> 带件外套\n[中国天气网](https://www.weather.com.cn/weather/101280101.shtml)"
        self.assertEqual(wechat_plain_text(text), "广州天气\n• 明天 13～20℃\n带件外套\n中国天气网：https://www.weather.com.cn/weather/101280101.shtml")

    def test_tables_become_lines_with_column_names(self):
        text = "| 日期 | 天气 | 气温 |\n|---|:---:|---:|\n| 9日 | 多云 | **13～23℃** |\n| 10日 | 晴 | 13～20℃ |"
        self.assertEqual(wechat_plain_text(text), "日期：9日；天气：多云；气温：13～23℃\n日期：10日；天气：晴；气温：13～20℃")

    def test_fenced_and_inline_code_keep_operators_and_indentation(self):
        text = "代码：\n```python\n    value = 2 ** 3\n    def f(**kwargs):\n        return a_b * c\n```\n说明：`**kwargs` 与 `a | b`。"
        result = wechat_plain_text(text)
        self.assertIn("    value = 2 ** 3\n    def f(**kwargs):\n        return a_b * c", result)
        self.assertIn("说明：**kwargs 与 a | b。", result)
        self.assertNotIn("```", result)

    def test_plain_math_paths_and_bare_urls_are_preserved(self):
        text = "2 ** 3 = 8\n2**3**2 = 512\nx*y*z\na__b__c\nsnake_case\n/tmp/a_b_c.py\nhttps://example.test/a__b?q=**literal**#part"
        self.assertEqual(wechat_plain_text(text), text)

    def test_link_destinations_with_parentheses_or_stars_are_preserved(self):
        text = "[文档](https://example.test/wiki/A_(B)?q=**x**)"
        self.assertEqual(wechat_plain_text(text), "文档：https://example.test/wiki/A_(B)?q=**x**")

    def test_code_pipes_and_escaped_pipes_in_tables(self):
        text = "| 项目 | 内容 |\n|---|---|\n| 表达式 | `a | b` |\n| 字符 | a\\|b |"
        self.assertEqual(wechat_plain_text(text), "项目：表达式；内容：a | b\n项目：字符；内容：a|b")
