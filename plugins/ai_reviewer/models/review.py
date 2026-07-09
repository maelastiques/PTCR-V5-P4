from dataclasses import dataclass, field
from typing import List


@dataclass
class ReviewMessage:
    level: str
    title: str
    details: str


@dataclass
class ReviewResult:
    summary: str
    messages: List[ReviewMessage] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = ["# AI Review", "", self.summary, ""]
        for message in self.messages:
            lines.append(f"- [{message.level}] {message.title}: {message.details}")
        return "\n".join(lines)
