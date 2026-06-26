"""Data models for annual direction agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.annual_direction.mode import AgentMode


@dataclass
class DataGap:
    key: str
    label: str
    importance: str  # high / medium / low
    in_db: bool = False
    value: Any = None
    source: str | None = None  # db / web / missing
    note: str | None = None


@dataclass
class EtfCandidate:
    ts_code: str
    extname: str
    index_name: str | None
    list_date: str | None
    exchange: str | None
    theme_hint: str | None = None


@dataclass
class AnnualContext:
    apply_year: int
    agent_mode: AgentMode | None = None
    cewc: dict[str, Any] | None = None
    macro_snapshot: dict[str, Any] | None = None
    macro_brief: str = ""
    etf_universe_count: int = 0
    etf_candidates: list[EtfCandidate] = field(default_factory=list)
    gaps: list[DataGap] = field(default_factory=list)
    supplemented: dict[str, Any] = field(default_factory=dict)
    still_missing: list[str] = field(default_factory=list)
    sector_signals: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "apply_year": self.apply_year,
            "cewc": self.cewc,
            "macro_snapshot": self.serialize_snapshot(self.macro_snapshot),
            "macro_brief": self.macro_brief,
            "etf_universe_count": self.etf_universe_count,
            "etf_candidates": [
                {
                    "ts_code": e.ts_code,
                    "extname": e.extname,
                    "index_name": e.index_name,
                    "list_date": e.list_date,
                    "theme_hint": e.theme_hint,
                }
                for e in self.etf_candidates
            ],
            "sw_sector_signals": self.sector_signals,
            "supplemented_from_web": self.supplemented,
            "still_missing": self.still_missing,
            "data_gaps": [
                {
                    "key": g.key,
                    "label": g.label,
                    "importance": g.importance,
                    "source": g.source,
                    "value": g.value,
                    "note": g.note,
                }
                for g in self.gaps
            ],
        }
        if self.agent_mode:
            out["agent_mode"] = self.agent_mode.to_prompt_dict()
        return out

    @staticmethod
    def serialize_snapshot(snap: dict[str, Any] | None) -> dict[str, Any] | None:
        if not snap:
            return None
        out = {}
        for k, v in snap.items():
            if k in ("created_at", "updated_at"):
                continue
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            else:
                out[k] = v
        return out
