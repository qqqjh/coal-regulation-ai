"""待审文档通用 chunk packing 规则 v9。

这个模块只处理已经抽取好的结构 chunk，不关心输入来自 MinerU JSON、docx
还是其他解析器。调用方需要先给出初始 chunk：

    {
        "content": "...",
        "chapter": "...",
        "section": "...",
        "article": "...",
        "chunk_level": "paragraph",
        "source_blocks": [0, 1, 2],          # 可选，docx 前端定位用
        "source_units": [                   # 可选，更精确地保留来源映射
            {"text": "...", "source_blocks": [0]},
        ],
    }

核心规则来自 pending_doc_chunking_v9.py：
- 小标题碎片先并入后续内容；
- 按 一、/1./（一）/（1）/① 等行首编号识别结构单元；
- 相邻同父级结构单元打包到约 1000 字；
- 普通块 2000 字硬上限，表格块 2400 字上限；
- 超长块优先继续按结构拆，最后才按段落/句子兜底；
- 表格尽量保持完整。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple


TARGET_CHUNK_SIZE = 1000
MAX_CHUNK_SIZE = 2000
TABLE_MAX_CHUNK_SIZE = 2400
MIN_CHUNK_SIZE = 300
TINY_CHUNK_SIZE = 120
MIN_SPLIT_PARTS = 2
FIRST_LEVEL_MIN_BODY_CHARS = 80
SECOND_LEVEL_MIN_BODY_CHARS = 80
THIRD_LEVEL_MIN_BODY_CHARS = 60
FOURTH_LEVEL_MIN_BODY_CHARS = 50


def _unique_ints(values: List[Any]) -> List[int]:
    seen = set()
    result: List[int] = []
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class PendingChunkPackerV9:
    """结构化二次切分 + 相邻单元打包，可复用于 docx 和 MinerU JSON。"""

    def marker_levels(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "first_level_cn",
                "pattern": r"^[ \t]*([一二三四五六七八九十百]+[、.．])[ \t]*(?!\d+[、.．])",
                "min_body": FIRST_LEVEL_MIN_BODY_CHARS,
            },
            {
                "name": "second_level_arabic_dot",
                "pattern": r"^[ \t]*(\d+[.．、])(?=[ \t]*[\u4e00-\u9fffA-Za-z（(])[\t ]*",
                "min_body": SECOND_LEVEL_MIN_BODY_CHARS,
            },
            {
                "name": "third_level_cn_parenthesized",
                "pattern": r"^[ \t]*([（(][一二三四五六七八九十百]+[）)])[ \t]*",
                "min_body": THIRD_LEVEL_MIN_BODY_CHARS,
            },
            {
                "name": "fourth_level_num_parenthesized",
                "pattern": r"^[ \t]*([（(]\d+[）)])[ \t]*",
                "min_body": FOURTH_LEVEL_MIN_BODY_CHARS,
            },
            {
                "name": "fifth_level_circled_num",
                "pattern": r"^[ \t]*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])[ \t]*",
                "min_body": FOURTH_LEVEL_MIN_BODY_CHARS,
            },
        ]

    def split_by_line_markers(self, content: str, marker_pattern: str) -> Tuple[str, List[Tuple[str, str]]]:
        """只按行首编号切分，避免句中编号或数值范围误触发。"""
        matches = list(re.finditer(marker_pattern, content, flags=re.MULTILINE))
        if not matches:
            return "", []

        preamble = content[:matches[0].start()].strip()
        parts = []
        for i, match in enumerate(matches):
            marker = match.group(1).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            body = content[start:end].strip()
            if body:
                parts.append((marker, body))
        return preamble, parts

    def split_by_chinese_numbers(self, content: str) -> List[Tuple[str, str]]:
        preamble, parts = self.split_by_line_markers(content, self.marker_levels()[0]["pattern"])
        if not parts:
            return [("", content)]
        return ([("", preamble)] if preamble else []) + parts

    def split_by_arabic_numbers(self, content: str) -> List[Tuple[str, str]]:
        preamble, parts = self.split_by_line_markers(content, self.marker_levels()[1]["pattern"])
        if not parts:
            return [("", content)]
        return ([("", preamble)] if preamble else []) + parts

    def _context_lines_for_sub_chunk(self, chunk: Dict[str, Any], preamble: str = "") -> List[str]:
        context_lines = []
        if preamble and len(preamble) <= 300:
            context_lines.extend(line.strip() for line in preamble.splitlines() if line.strip())
        else:
            for key in ["part", "chapter", "section", "article"]:
                value = chunk.get(key, "")
                if value and value not in context_lines:
                    context_lines.append(value)
        return context_lines

    def _extract_sub_title(self, body: str) -> str:
        first_line = body.strip().splitlines()[0] if body.strip() else ""
        return first_line.strip()[:80]

    def _has_table(self, content: str) -> bool:
        return "【表格】" in content or "\n|" in content or " | " in content

    def _max_size_for_content(self, content: str) -> int:
        return TABLE_MAX_CHUNK_SIZE if self._has_table(content) else MAX_CHUNK_SIZE

    def _content_from_unit_group(self, units: List[Dict[str, Any]]) -> str:
        if not units:
            return ""
        lines = list(units[0]["context"])
        lines.extend(unit["item"] for unit in units if unit.get("item"))
        return "\n".join(line for line in lines if line).strip()

    def _unit_group_len(self, units: List[Dict[str, Any]]) -> int:
        return len(self._content_from_unit_group(units))

    def _make_unit(
        self,
        context_lines: List[str],
        item: str,
        strategy: str,
        marker: str,
        title: str,
    ) -> Dict[str, Any]:
        return {
            "context": tuple(line for line in context_lines if line),
            "item": item.strip(),
            "strategy": strategy,
            "marker": marker.strip(),
            "title": title.strip(),
        }

    def _split_content_to_units(
        self,
        chunk: Dict[str, Any],
        content: str,
        context_lines: List[str],
        level_index: int = 0,
    ) -> List[Dict[str, Any]]:
        levels = self.marker_levels()
        chosen = None

        for idx in range(level_index, len(levels)):
            preamble, parts = self.split_by_line_markers(content, levels[idx]["pattern"])
            meaningful = [
                (marker, body)
                for marker, body in parts
                if len(marker + body) >= levels[idx]["min_body"]
            ]
            if len(meaningful) >= MIN_SPLIT_PARTS:
                chosen = (idx, levels[idx], preamble, parts)
                break

        if chosen is None:
            item = content.strip()
            return [self._make_unit(context_lines, item, "unsplit", "", self._extract_sub_title(item))]

        idx, level, preamble, parts = chosen
        base_context = list(context_lines)
        preamble = preamble.strip()

        units: List[Dict[str, Any]] = []
        if preamble:
            if len(preamble) <= MIN_CHUNK_SIZE:
                base_context.extend(line.strip() for line in preamble.splitlines() if line.strip())
            else:
                units.append(self._make_unit(
                    context_lines,
                    preamble,
                    f"{level['name']}_preamble",
                    "",
                    self._extract_sub_title(preamble),
                ))
        elif not base_context:
            base_context = self._context_lines_for_sub_chunk(chunk)

        for marker, body in parts:
            item = (marker + body).strip()
            full_text = "\n".join(base_context + [item]).strip()
            limit = self._max_size_for_content(full_text)

            if len(full_text) > limit and idx < len(levels):
                child_context = list(base_context)
                child_context.append(marker.strip())
                child_units = self._split_content_to_units(chunk, body, child_context, idx)
                if len(child_units) > 1 or child_units[0]["strategy"] != "unsplit":
                    units.extend(child_units)
                    continue

            units.append(self._make_unit(
                base_context,
                item,
                level["name"],
                marker,
                self._extract_sub_title(body),
            ))

        return units

    def _source_blocks_for_content(self, chunk: Dict[str, Any], content: str) -> List[int]:
        """用 source_units 尽量缩小子 chunk 来源；兜底继承父 chunk source_blocks。"""
        parent_blocks = _unique_ints(chunk.get("source_blocks", []))
        units = chunk.get("source_units") or []
        if not units:
            return parent_blocks

        hits: List[Any] = []
        for unit in units:
            text = str(unit.get("text", "")).strip()
            if not text:
                continue
            if text in content:
                hits.extend(unit.get("source_blocks", []))

        return _unique_ints(hits) or parent_blocks

    def _source_units_for_content(self, chunk: Dict[str, Any], content: str) -> List[Dict[str, Any]]:
        units = chunk.get("source_units") or []
        if not units:
            return []
        matched = []
        for unit in units:
            text = str(unit.get("text", "")).strip()
            if text and text in content:
                matched.append(unit)
        return matched

    def _make_sub_chunk(
        self,
        chunk: Dict[str, Any],
        content: str,
        sub_marker: str,
        split_strategy: str,
        sub_title: str = "",
    ) -> Dict[str, Any]:
        new_chunk = {
            **chunk,
            "content": content,
            "char_count": len(content),
            "sub_marker": sub_marker,
            "sub_title": sub_title,
            "is_sub_chunk": True,
            "split_strategy": split_strategy,
            "parent_chunk_level": chunk.get("chunk_level", ""),
            "parent_char_count": chunk.get("char_count", len(chunk.get("content", ""))),
        }
        source_blocks = self._source_blocks_for_content(chunk, content)
        if source_blocks:
            new_chunk["source_blocks"] = source_blocks
        source_units = self._source_units_for_content(chunk, content)
        if source_units:
            new_chunk["source_units"] = source_units
            new_chunk["source_pdf_blocks"] = source_units
        elif "source_units" in new_chunk:
            new_chunk.pop("source_units", None)
            new_chunk.pop("source_pdf_blocks", None)
        return new_chunk

    def _merge_chunk_sources(self, target: Dict[str, Any], source: Dict[str, Any]) -> None:
        target["source_blocks"] = _unique_ints(
            list(target.get("source_blocks", [])) + list(source.get("source_blocks", []))
        )
        if target.get("source_units") or source.get("source_units"):
            target["source_units"] = list(target.get("source_units", [])) + list(source.get("source_units", []))
            target["source_pdf_blocks"] = list(target["source_units"])

    def _pack_units_to_chunks(self, chunk: Dict[str, Any], units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []

        for unit in units:
            if not current:
                current = [unit]
                continue

            same_context = current[0]["context"] == unit["context"]
            candidate = current + [unit]
            candidate_text = self._content_from_unit_group(candidate)
            candidate_limit = self._max_size_for_content(candidate_text)
            current_len = self._unit_group_len(current)

            should_pack = (
                same_context
                and len(candidate_text) <= candidate_limit
                and (len(candidate_text) <= TARGET_CHUNK_SIZE or current_len < MIN_CHUNK_SIZE)
            )

            if should_pack:
                current.append(unit)
            else:
                groups.append(current)
                current = [unit]

        if current:
            groups.append(current)

        chunks = []
        for group in groups:
            content = self._content_from_unit_group(group)
            first, last = group[0], group[-1]
            strategy = first["strategy"]
            if len(group) > 1:
                strategy = f"{strategy}_packed"
            marker = first["marker"] if len(group) == 1 else f"{first['marker']}~{last['marker']}"
            title = first["title"] if len(group) == 1 else f"{first['title']} / {last['title']}"
            new_chunk = self._make_sub_chunk(chunk, content, marker, strategy, title)
            if self._has_table(content):
                new_chunk["has_table"] = True
            chunks.append(new_chunk)

        return chunks

    def _merge_small_sub_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(chunks) <= 1:
            return chunks

        merged = []
        for chunk in chunks:
            if not merged:
                merged.append(chunk)
                continue

            prev = merged[-1]
            same_parent = (
                prev.get("part") == chunk.get("part")
                and prev.get("chapter") == chunk.get("chapter")
                and prev.get("section") == chunk.get("section")
                and prev.get("article") == chunk.get("article")
            )
            merged_content = prev["content"] + "\n" + chunk["content"]
            merged_limit = self._max_size_for_content(merged_content)

            if same_parent and (
                chunk["char_count"] < MIN_CHUNK_SIZE or prev["char_count"] < TINY_CHUNK_SIZE
            ) and len(merged_content) <= merged_limit:
                prev["content"] = merged_content
                prev["char_count"] = len(merged_content)
                prev["sub_marker"] = prev.get("sub_marker", "") or chunk.get("sub_marker", "")
                prev["merged_sub_title"] = True
                self._merge_chunk_sources(prev, chunk)
            else:
                merged.append(chunk)

        return merged

    def _post_split_cleanup(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        chunks = self._merge_small_sub_chunks(chunks)
        final_chunks = []
        for chunk in chunks:
            limit = self._max_size_for_content(chunk["content"])
            if chunk["char_count"] > limit and not chunk.get("oversized_checked"):
                checked = dict(chunk)
                checked["oversized_checked"] = True
                final_chunks.extend(self.split_large_chunk(checked))
            else:
                final_chunks.append(chunk)
        return final_chunks

    def _fallback_split_by_paragraph_or_sentence(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        text = chunk["content"]
        segments = [seg.strip() for seg in re.split(r"\n{2,}", text) if seg.strip()]
        if len(segments) <= 1:
            segments = [seg.strip() for seg in text.splitlines() if seg.strip()]
        if len(segments) <= 1:
            segments = [seg for seg in re.split(r"(?<=[。！？])", text) if seg.strip()]

        parts = []
        buf = ""
        for seg in segments:
            candidate = f"{buf}\n{seg}".strip() if buf else seg
            if buf and len(candidate) > TARGET_CHUNK_SIZE:
                parts.append(buf)
                buf = seg
            else:
                buf = candidate
        if buf:
            parts.append(buf)

        result = [
            self._make_sub_chunk(
                chunk,
                part,
                chunk.get("sub_marker", ""),
                "fallback_paragraph_sentence",
                self._extract_sub_title(part),
            )
            for part in parts
        ]
        return self._merge_small_sub_chunks(result)

    def split_structured_chunk(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        content = chunk["content"]
        units = self._split_content_to_units(chunk, content, [])

        if len(units) == 1 and units[0]["strategy"] == "unsplit":
            limit = self._max_size_for_content(content)
            if len(content) > limit:
                return self.split_large_chunk(chunk)
            return [chunk]

        packed = self._pack_units_to_chunks(chunk, units)
        return self._post_split_cleanup(packed)

    def split_large_chunk(self, chunk: Dict[str, Any]) -> List[Dict[str, Any]]:
        content = chunk["content"]
        limit = self._max_size_for_content(content)

        if len(content) <= limit:
            return [chunk]

        if self._has_table(content):
            marked = dict(chunk)
            marked["oversized_table_chunk"] = True
            marked["split_strategy"] = marked.get("split_strategy", "table_preserved_oversized")
            return [marked]

        units = self._split_content_to_units(chunk, content, [])
        if len(units) > 1 or units[0]["strategy"] != "unsplit":
            packed = self._pack_units_to_chunks(chunk, units)
            if len(packed) > 1:
                return self._post_split_cleanup(packed)

        return self._fallback_split_by_paragraph_or_sentence(chunk)

    def merge_small_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """合并纯标题类小 chunk；保留 source_blocks/source_units 并更新 char_count。"""
        if not chunks:
            return chunks

        merged = []
        pending_chunks: List[Dict[str, Any]] = []

        for chunk in chunks:
            normalized = dict(chunk)
            normalized["char_count"] = len(normalized.get("content", ""))
            if normalized.get("chunk_level") == "table" or normalized.get("oversized_table_chunk"):
                if pending_chunks:
                    merged.extend(pending_chunks)
                    pending_chunks = []
                merged.append(normalized)
                continue
            if normalized["char_count"] < TINY_CHUNK_SIZE:
                pending_chunks.append(normalized)
                continue

            if pending_chunks:
                all_chunks = pending_chunks + [normalized]
                all_contents = [item["content"] for item in all_chunks if item.get("content")]
                merged_content = "\n".join(all_contents)
                merged_chunk = {
                    **normalized,
                    "content": merged_content,
                    "char_count": len(merged_content),
                    "merged_from_title": True,
                }
                merged_chunk["source_blocks"] = _unique_ints([
                    block
                    for item in all_chunks
                    for block in item.get("source_blocks", [])
                ])
                source_units = [
                    unit
                    for item in all_chunks
                    for unit in item.get("source_units", [])
                ]
                if source_units:
                    merged_chunk["source_units"] = source_units
                    merged_chunk["source_pdf_blocks"] = source_units
                first_page = str(pending_chunks[0].get("page_range", "")).split("-")[0]
                last_page = str(normalized.get("page_range", "")).split("-")[-1]
                if first_page or last_page:
                    merged_chunk["page_range"] = f"{first_page}-{last_page}".strip("-")
                merged.append(merged_chunk)
                pending_chunks = []
            else:
                merged.append(normalized)

        if pending_chunks:
            if len(pending_chunks) == 1:
                merged.append(pending_chunks[0])
            else:
                merged_content = "\n".join(item["content"] for item in pending_chunks if item.get("content"))
                merged_chunk = {
                    **pending_chunks[-1],
                    "content": merged_content,
                    "char_count": len(merged_content),
                    "merged_from_title": True,
                    "source_blocks": _unique_ints([
                        block
                        for item in pending_chunks
                        for block in item.get("source_blocks", [])
                    ]),
                }
                source_units = [
                    unit
                    for item in pending_chunks
                    for unit in item.get("source_units", [])
                ]
                if source_units:
                    merged_chunk["source_units"] = source_units
                    merged_chunk["source_pdf_blocks"] = source_units
                merged.append(merged_chunk)

        return merged

    def pack_chunks(self, chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged = self.merge_small_chunks(chunks)
        final_chunks: List[Dict[str, Any]] = []
        for chunk in merged:
            normalized = dict(chunk)
            normalized["char_count"] = len(normalized.get("content", ""))
            final_chunks.extend(self.split_structured_chunk(normalized))
        return final_chunks


def pack_pending_chunks_v9(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """对初始待审 chunk 应用 v9 packing 规则。"""
    return PendingChunkPackerV9().pack_chunks(chunks)
