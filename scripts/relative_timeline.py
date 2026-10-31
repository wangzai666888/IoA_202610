"""将合并后的 Docker 时间线转换为从任务开始计时的时间线。"""
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

STAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z) (.*)$"
)

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

def utc_time(value: str) -> datetime:
    return datetime.fromisoformat(
        value.strip().replace("Z", "+00:00")
    ).astimezone(timezone.utc)

def relative_label(start: datetime, event: datetime) -> str:
    milliseconds = round((event - start).total_seconds() * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"T+{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "用法: python3 scripts/relative_timeline.py "
            "meta.txt 原始timeline.log 输出timeline.log"
        )

    meta_path, raw_path, output_path = map(Path, sys.argv[1:])
    if raw_path.resolve() == output_path.resolve():
        raise SystemExit("输入和输出不能是同一个文件")

    start_line = next(
        (line for line in meta_path.read_text(encoding="utf-8").splitlines()
         if line.startswith("start_utc=")),
        None,
    )
    if start_line is None:
        raise SystemExit(f"{meta_path} 缺少 start_utc=")
    start = utc_time(start_line.split("=", 1)[1])

    events = []
    skipped_control = 0

    for number, line in enumerate(
        raw_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        line = ANSI.sub("", line)  # 去掉终端颜色、光标移动等控制字符
        if not line.strip():       # 只跳过没有实际文字的控制行
            skipped_control += 1
            continue

        match = STAMP.match(line)
        if not match:
            raise SystemExit(
  		f"{raw_path} 第 {number} 行没有预期的时间戳:"
                f"{line[:100]!r}"
            )

        event_time = utc_time(match.group(1))
        if event_time >= start:  # 排除任务开始前的容器旧日志
            events.append((event_time, number, match.group(2)))

    events.sort(key=lambda item: (item[0], item[1]))
    with output_path.open("w", encoding="utf-8") as output:
        output.write("T+00:00:00.000 [experiment] simulation_start\n")
        for event_time, _, message in events:
            output.write(f"{relative_label(start, event_time)} {message}\n")

    print(f"已写入 {output_path}，共 {len(events)} 条容器日志")

if __name__ == "__main__":
    main()
