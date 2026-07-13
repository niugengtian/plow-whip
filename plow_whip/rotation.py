"""File rotation enforcement for plow-whip collab memory."""

from __future__ import annotations

import os
import re
from datetime import datetime

from .io_utils import atomic_write_text

COMMS_KEEP_RECENT_BLOCKS = 5
COMMS_BLOCK_HEADER = re.compile(r"^### \[")
ARCHIVE_POINTER = re.compile(r"^<!-- Previous messages archived to: .*?-->\s*", re.MULTILINE)


def split_message_blocks(content: str) -> tuple[str, list[str]]:
    """Split AGENT_COMMS into preamble + `### [agent]` message blocks."""
    content = ARCHIVE_POINTER.sub("", content).lstrip()
    parts = re.split(r"(?=^### \[)", content, flags=re.MULTILINE)
    preamble_parts: list[str] = []
    blocks: list[str] = []
    for part in parts:
        if not part.strip():
            continue
        if part.lstrip().startswith("### ["):
            blocks.append(part.strip())
        else:
            preamble_parts.append(part)
    return "".join(preamble_parts).rstrip(), blocks


def block_title(block: str) -> str:
    first = block.strip().splitlines()[0] if block.strip() else "(empty)"
    return first.lstrip("#").strip()


def comms_block_count(filepath: str) -> int:
    if not os.path.exists(filepath):
        return 0
    with open(filepath, encoding="utf-8") as f:
        _, blocks = split_message_blocks(f.read())
    return len(blocks)


def file_stats(filepath: str) -> dict:
    if not os.path.exists(filepath):
        return {"exists": False, "lines": 0, "bytes": 0, "blocks": 0}
    size = os.path.getsize(filepath)
    with open(filepath, encoding="utf-8") as f:
        lines = f.readlines()
    _, blocks = split_message_blocks("".join(lines))
    return {
        "exists": True,
        "lines": len(lines),
        "bytes": size,
        "blocks": len(blocks),
    }


def comms_needs_block_rotation(
    filepath: str,
    max_lines: int,
    max_kb: int,
    keep_blocks: int = COMMS_KEEP_RECENT_BLOCKS,
) -> tuple[bool, dict]:
    stats = file_stats(filepath)
    if not stats["exists"]:
        return False, stats
    over_lines = stats["lines"] > max_lines
    over_size = stats["bytes"] > max_kb * 1024
    over_blocks = stats["blocks"] > keep_blocks
    return over_lines or over_size or over_blocks, stats


def build_comms_carry_forward(archived_blocks: list[str]) -> str:
    lines = ["## Carry Forward", f"- Archived messages: {len(archived_blocks)}"]
    for block in archived_blocks[-8:]:
        lines.append(f"- {block_title(block)}")
    lines.append("")
    return "\n".join(lines)


def archive_comms_by_blocks(
    project: str,
    filepath: str,
    archive_dir: str,
    keep_blocks: int = COMMS_KEEP_RECENT_BLOCKS,
    topic: str = "AGENT_COMMS",
) -> tuple[bool, str | None]:
    """Archive old AGENT_COMMS message blocks; keep recent N blocks."""
    if not os.path.exists(filepath):
        return False, None

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    pointers = ARCHIVE_POINTER.findall(content)
    preamble, blocks = split_message_blocks(content)
    if len(blocks) <= keep_blocks:
        if len(pointers) > 1:
            compacted = pointers[0].strip() + "\n\n" + ARCHIVE_POINTER.sub("", content).lstrip()
            atomic_write_text(filepath, compacted)
            return True, None
        return False, None

    archived_blocks = blocks[:-keep_blocks]
    kept_blocks = blocks[-keep_blocks:]
    if not archived_blocks:
        return False, None

    os.makedirs(archive_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive_path = os.path.join(archive_dir, f"{timestamp}_{topic.replace('/', '_')}.md")

    stats = file_stats(filepath)
    with open(archive_path, "w", encoding="utf-8") as f:
        f.write("# Archived: AGENT_COMMS.md\n")
        f.write(f"**Project:** {project}\n")
        f.write(f"**Archived:** {datetime.now().isoformat(timespec='seconds')}\n")
        f.write(
            f"**Trigger:** block-rotation ({stats['lines']} lines, "
            f"{stats['blocks']} blocks → keep {keep_blocks})\n\n"
        )
        f.write(build_comms_carry_forward(archived_blocks))
        f.write("\n## Archived Messages\n\n")
        f.write("\n\n".join(archived_blocks))
        f.write("\n")

    kept_content = preamble
    if kept_content:
        kept_content += "\n\n"
    kept_content += "\n\n".join(kept_blocks)
    if not kept_content.endswith("\n"):
        kept_content += "\n"

    atomic_write_text(filepath, f"<!-- Previous messages archived to: {archive_path} -->\n\n{kept_content}")

    return True, archive_path
