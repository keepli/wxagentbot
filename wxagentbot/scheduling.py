"""Parse explicit scheduling requests without running a model or waiting."""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import re
from zoneinfo import ZoneInfo


class ScheduleError(ValueError):
    """A scheduling request needs correction or clarification from the user."""


@dataclasses.dataclass(frozen=True)
class SchedulePlan:
    due_at: float
    prompt: str
    timezone: str
    recurrence: dict | None = None


@dataclasses.dataclass(frozen=True)
class ScheduledResult:
    raw: str
    notify: bool
    message: str


SCHEDULE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"notify": {"type": "boolean"}, "message": {"type": "string"},
                   "reason": {"type": "string"}},
    "required": ["notify", "message", "reason"],
    "additionalProperties": False,
}

SCHEDULE_RESULT_RULES = (
    "本次为宿主触发的到期任务。最终输出必须符合指定 JSON 结构，宿主只会发送 message 字段。"
    "先执行任务、获取必要的最新数据，再严格按用户指定的与/或关系判断通知条件；条件成立时 notify=true。"
    "仅在用户明确要求条件不满足时不通知，且已确认所有通知条件均不满足时，才设 notify=false、message为空。"
    "reason 简要记录判断依据，供本地审计；message 使用适合微信的纯文本。"
    "任务没有设置通知条件时正常发送结果。查询失败、数据缺失或无法确认条件时不要当作条件不满足，"
    "应 notify=true 并在 message 中说明无法完成检查，不编造天气或结论。"
    "任务中的“明天”“第二天”相对于宿主提供的本次执行时间解析。"
)


def parse_scheduled_result(raw: str) -> ScheduledResult:
    def unique_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("预约结果包含重复字段")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError) as e:
        raise ValueError("预约执行结果不是有效 JSON") from e
    if (not isinstance(value, dict) or set(value) != {"notify", "message", "reason"}
            or not isinstance(value["notify"], bool)
            or not isinstance(value["message"], str) or not isinstance(value["reason"], str)
            or (value["notify"] and not value["message"].strip())):
        raise ValueError("预约执行结果缺少有效的通知决定和消息")
    return ScheduledResult(raw, value["notify"], value["message"])


_NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+|半)"
_NUMBER = re.compile(_NUM)
_UNIT = re.compile(rf"({_NUM})\s*(?:个\s*)?(半)?(秒钟?|分钟|小时|天)")
_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_WEEKDAYS = {char: day for day, char in enumerate("一二三四五六日")}
_WEEKDAYS["天"] = 6
_PERIODS = ("凌晨", "午夜", "半夜", "早上", "早晨", "上午", "中午", "下午", "傍晚", "晚上")

EXECUTION_RULES = (
    "未来时间的预约、周期任务由宿主程序登记和触发。当前模型调用只处理现在需要执行的任务。"
    "不要通过 sleep、等待工具、循环检查时间或后台进程等待未来时刻，也不要自行启动定时器。"
    "若用户要求未来执行但宿主未识别，请立即解释需要明确的时间与任务，建议格式为"
    "“明天13:50播报广州天气”或“10分钟后提醒我喝水”；不要声称已经创建预约。"
    "收到宿主标注“预约已到期”时，只执行其任务内容并返回最终结果。"
    "查询或取消宿主预约请使用 /schedules 或 /cancel-schedule 编号，不要编造预约状态。"
)


def _number(text):
    if text == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    total, digit = 0, 0
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif char in "十百":
            total += (digit or 1) * (10 if char == "十" else 100)
            digit = 0
        else:
            raise ScheduleError("时间中的数字无法识别，请使用阿拉伯数字。")
    return total + digit


def _duration(text, *, allow_without_after=False):
    seconds, pos = 0, 0
    while match := _UNIT.match(text, pos):
        seconds += (_number(match[1]) + (0.5 if match[2] else 0)) * {
            "秒": 1, "秒钟": 1, "分钟": 60, "小时": 3600, "天": 86400}[match[3]]
        pos = match.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
    after = re.match(r"(?:之后|以后|后)", text[pos:])
    if not pos or (not after and not allow_without_after):
        return None
    if not math.isfinite(seconds) or not 1 <= seconds <= 3660 * 86400:
        raise ScheduleError("延迟时间需要在 1 秒到 10 年之间。")
    return seconds, pos + (after.end() if after else 0)


def _clock(text):
    match = re.match(rf"({_NUM})[:：](\d{{1,2}})(?:[:：](\d{{1,2}}))?", text)
    if match:
        return _number(match[1]), int(match[2]), int(match[3] or 0), match.end()
    match = re.match(rf"({_NUM})(?:点|时)", text)
    if not match:
        return None
    hour, minute, second, pos = _number(match[1]), 0, 0, match.end()
    fraction = re.match(r"半|一刻|三刻", text[pos:])
    if fraction:
        minute = {"半": 30, "一刻": 15, "三刻": 45}[fraction[0]]
        pos += fraction.end()
    elif number := _NUMBER.match(text, pos):
        value = _number(number[0])
        pos = number.end()
        if text[pos:].startswith("秒"):
            second, pos = value, pos + 1
        else:
            minute = value
            suffix = re.match(r"分钟|分", text[pos:])
            if suffix:
                pos += suffix.end()
                seconds = re.match(rf"({_NUM})秒", text[pos:])
                if seconds:
                    second, pos = _number(seconds[1]), pos + seconds.end()
    if text[pos:].startswith("整"):
        pos += 1
    return hour, minute, second, pos


def _action(text, *, reminder=False, clock_tail=False):
    text = text.strip(" ，,：:。")
    text = re.sub(r"^(?:(?:执行|运行|触发)的?|的)?(?:定时任务|定时提醒|任务)(?:[，,：:\s]+|$)", "", text)
    text = re.sub(r"^(?:的时候|之后|以后|时|后)(?:[，,\s]*)", "", text)
    text = re.sub(r"^(?:再|就)(?:[，,\s]*)", "", text).strip()
    if not text or re.fullmatch(r"(?:提醒我|提醒|通知我|通知|播报|执行|帮我)", text):
        raise ScheduleError("请把执行时间和任务内容写在同一条消息，例如：10分钟后提醒我喝水。")
    if clock_tail and re.match(r"[\d秒分:+\-]|的", text):
        raise ScheduleError("时间格式不明确，请使用“明天13:50播报广州天气”这样的格式。")
    if reminder and not text.startswith(("提醒", "通知")):
        text = "提醒我" + text
    return text


def _local_time(day, hour, minute, second, zone):
    try:
        naive = dt.datetime.combine(day, dt.time(hour, minute, second))
        aware = naive.replace(tzinfo=zone)
        if dt.datetime.fromtimestamp(aware.timestamp(), zone).replace(tzinfo=None) != naive:
            raise ValueError("nonexistent local time")
        return aware
    except ValueError as e:
        raise ScheduleError("日期或时间无效，请检查小时、分钟和日期。") from e


def next_occurrence(recurrence: dict | None, after: float, timezone: str, *, previous: float | None = None):
    if recurrence is None:
        return None
    if recurrence["kind"] == "interval":
        interval = recurrence["seconds"]
        if not math.isfinite(interval) or interval < 60:
            raise ScheduleError("重复提醒的间隔至少为 60 秒。")
        anchor = after if previous is None else previous
        return anchor + (max(0, math.floor((after - anchor) / interval)) + 1) * interval
    zone = ZoneInfo(timezone)
    local = dt.datetime.fromtimestamp(after, zone)
    days = recurrence.get("weekdays", list(range(7)))
    for offset in range(15):
        day = local.date() + dt.timedelta(days=offset)
        if day.weekday() not in days:
            continue
        try:
            due = _local_time(day, recurrence["hour"], recurrence["minute"], recurrence["second"], zone)
        except ScheduleError:
            continue  # Skip a local clock time that does not exist across DST.
        if due.timestamp() > after:
            return due.timestamp()
    raise ScheduleError("无法确定下一次执行时间。")


def parse_schedule(text: str, *, now: float, timezone: str = "Asia/Shanghai") -> SchedulePlan | None:
    """Return None for immediate requests, or a plan/clarification for timers."""
    text = text.strip()
    if "```" in text or text.startswith(('"', "“", "'", "‘")):
        return None
    original = text
    while match := re.match(r"^(?:请帮我|请你帮我|请你|麻烦你|能不能|能否|帮我|请|麻烦)\s*", text):
        text = text[match.end():]
    first_clause = re.split(r"[，,。；;\n]", text, maxsplit=1)[0]
    if (re.match(r"^(?:创建|生成)", text) and "定时任务" not in first_clause
            and re.search(r"程序|脚本|代码", first_clause)):
        return None
    time_lead = rf"(?:每天|每日|每晚|每早|每个工作日|工作日|每周|每星期|每隔|今天|明天|后天|今晚|明晚|明早|今早|下周|下星期|{_NUM}(?:[:：点时]|秒|分钟|小时|天)|凌晨|早上|上午|中午|下午|傍晚|晚上)"
    create = rf"(?:设置|创建|安排)(?:一个|个|一条|条)?(?:(?:定时任务|定时提醒|提醒|闹钟)(?:在|为|[：:])?|(?:在|于)?(?={time_lead}))"
    intro = re.match(rf"^(?:{create}|定时(?:在)?|提醒我(?:在)?|等到|到了|到|在|于|再过|过)\s*", text)
    explicit = intro is not None
    reminder = bool(intro and intro[0].startswith("提醒我"))
    allow_without_after = bool(intro and intro[0].strip() in ("过", "再过"))
    if intro:
        text = text[intro.end():]
    interval = re.match(r"^(?:每隔|每)\s*", text)
    if interval:
        duration = _duration(text[interval.end():], allow_without_after=True)
        if duration:
            seconds, end = duration
            if seconds < 60:
                raise ScheduleError("重复提醒的间隔至少为 60 秒。")
            return SchedulePlan(now + seconds, _action(text[interval.end() + end:]), timezone,
                                {"kind": "interval", "seconds": seconds})
    if duration := _duration(text, allow_without_after=allow_without_after):
        seconds, end = duration
        if text[end:].lstrip().startswith("的"):
            return None
        return SchedulePlan(now + seconds, _action(text[end:], reminder=reminder), timezone)

    zone = ZoneInfo(timezone)
    local = dt.datetime.fromtimestamp(now, zone)
    day, period, weekdays = local.date(), "", None
    recurring = re.match(r"^(每天|每日|每晚|每早|每个工作日|工作日|每周[一二三四五六日天]|每星期[一二三四五六日天])\s*", text)
    if recurring:
        word = recurring[1]
        period = {"每晚": "晚上", "每早": "早上"}.get(word, "")
        if "工作日" in word:
            weekdays = list(range(5))
        elif word.startswith(("每周", "每星期")):
            weekdays = [_WEEKDAYS[word[-1]]]
        text = text[recurring.end():]
        explicit = True
    else:
        relative_day = re.match(r"^(今天|明天|大后天|后天|今晚|明晚|明早|今早)\s*", text)
        weekday = re.match(r"^(下下|下|本|这)?(?:周|星期)([一二三四五六日天])\s*", text)
        date = re.match(r"^(?:(\d{4})(?:年|[-/]))?(\d{1,2})(?:月|[-/])(\d{1,2})(?:日|号)?\s*", text)
        if relative_day:
            word = relative_day[1]
            day += dt.timedelta(days={"今天": 0, "明天": 1, "后天": 2, "大后天": 3,
                                       "今晚": 0, "明晚": 1, "明早": 1, "今早": 0}[word])
            period = "晚上" if word.endswith("晚") else "早上" if word.endswith("早") else ""
            text = text[relative_day.end():]
        elif weekday:
            target = _WEEKDAYS[weekday[2]]
            if weekday[1]:
                weeks = {"下下": 2, "下": 1, "本": 0, "这": 0}[weekday[1]]
                day += dt.timedelta(days=weeks * 7 + target - day.weekday())
            else:
                day += dt.timedelta(days=(target - day.weekday()) % 7)
            text = text[weekday.end():]
        elif date:
            try:
                day = dt.date(int(date[1] or local.year), int(date[2]), int(date[3]))
            except ValueError as e:
                raise ScheduleError("日期无效，请检查年月日。") from e
            text = text[date.end():].lstrip("T ")
    for candidate in _PERIODS:
        if text.startswith(candidate):
            period, text = candidate, text[len(candidate):].lstrip()
            break
    clock = _clock(text)
    if clock is None:
        weak_intro = intro and intro[0].strip() in ("在", "于", "到", "到了", "过", "再过")
        if weak_intro and not re.search(r"今天|明天|后天|提醒|定时|每天|每周|\d", text):
            return None
        if explicit or re.match(r"^(?:每天|每周|每隔|定时|稍后|过一会|晚点)", original) or re.match(r"^(?:提醒我|通知我)", text):
            raise ScheduleError("请给出明确的时间，例如：明天13:50播报广州天气，或10分钟后提醒我喝水。")
        return None
    hour, minute, second, end = clock
    tail = text[end:].strip()
    # A time used as a noun ("13:50的天气/日志") is not an instruction to wait.
    if tail.startswith("的") and not tail.startswith("的时候") and not explicit:
        return None
    if re.match(r"有什么|是什么|为什么|怎么样|是否|多少", tail):
        return None
    if any(not math.isfinite(value) or value != int(value) for value in (hour, minute, second)):
        raise ScheduleError("钟点时间需要使用整数小时和分钟。")
    hour, minute, second = int(hour), int(minute), int(second)
    if period in ("下午", "傍晚", "晚上") and 1 <= hour < 12:
        hour += 12
    elif period == "中午" and 1 <= hour < 11:
        hour += 12
    elif period in ("凌晨", "午夜", "半夜") and hour == 12:
        hour = 0
    elif period == "晚上" and hour == 12:
        raise ScheduleError("晚上12点的日期容易混淆，请明确写成某一天的00:00。")
    due = _local_time(day, hour, minute, second, zone).timestamp()
    action = _action(tail, reminder=reminder, clock_tail=True)
    recurrence = None
    if recurring:
        recurrence = {"kind": "calendar", "hour": hour, "minute": minute, "second": second}
        if weekdays is not None:
            recurrence["weekdays"] = weekdays
        due = next_occurrence(recurrence, now, timezone)
    elif due <= now:
        raise ScheduleError("这个时间已经过去，请明确指定未来的日期，例如“明天13:50播报广州天气”。")
    return SchedulePlan(due, action, timezone, recurrence)


def describe_time(timestamp: float, timezone: str) -> str:
    local = dt.datetime.fromtimestamp(timestamp, ZoneInfo(timezone))
    label = "北京时间" if timezone == "Asia/Shanghai" else timezone
    return f"{local:%Y-%m-%d %H:%M:%S}（{label}）"


def schedule_command(text: str):
    text = text.strip().rstrip("。！!？?")
    text = re.sub(r"^(?:请帮我|帮我|请|麻烦你)\s*", "", text)
    if text in ("/schedules", "查看定时任务", "列出定时任务", "查看预约", "列出预约"):
        return "list", None
    match = re.fullmatch(r"(?:/cancel-schedule|取消定时任务|取消预约)\s*([a-fA-F0-9]{8,32})", text)
    if match:
        return "cancel", match[1].lower()
    if text.startswith(("/cancel-schedule", "取消定时任务", "取消预约")):
        raise ScheduleError("请发送“/schedules”查看编号，再发送“/cancel-schedule 编号”取消预约。")
    return None
