from docx import Document
from docx.shared import RGBColor
from typing import List, Dict, Any
import re
from pathlib import Path


class DocumentModifier:
    """Word文档修改服务"""

    def __init__(self):
        pass

    def apply_changes_to_document(
        self,
        original_file_path: Path,
        output_file_path: Path,
        changes: List[Dict[str, Any]],
        accepted_change_ids: List[int]
    ) -> Dict[str, Any]:
        """
        应用选定的修改到Word文档

        Args:
            original_file_path: 原始文档路径
            output_file_path: 输出文档路径
            changes: 所有修改建议列表
            accepted_change_ids: 接受的修改ID列表

        Returns:
            应用结果统计
        """
        # 加载原始文档
        doc = Document(original_file_path)

        # 筛选出被接受的修改
        accepted_changes = [
            change for change in changes
            if change.get('id') in accepted_change_ids
        ]

        # 按类型分组修改
        typo_changes = [c for c in accepted_changes if c.get('type') == 'typo']
        fluency_changes = [c for c in accepted_changes if c.get('type') == 'fluency']
        compliance_changes = [c for c in accepted_changes if c.get('type') == 'compliance']

        applied_count = 0
        failed_count = 0

        # 应用错别字修正
        for change in typo_changes:
            original_text = change.get('original', '')
            corrected_text = change.get('corrected', '')

            if original_text and corrected_text:
                success = self._replace_text_in_document(doc, original_text, corrected_text)
                if success:
                    applied_count += 1
                else:
                    failed_count += 1

        # 应用语义通顺性改进
        for change in fluency_changes:
            original_text = change.get('original', '')
            improved_text = change.get('improved', '')

            if original_text and improved_text:
                success = self._replace_text_in_document(doc, original_text, improved_text)
                if success:
                    applied_count += 1
                else:
                    failed_count += 1

        # 应用合规性修正
        for change in compliance_changes:
            original_text = change.get('original', '')
            suggestion_text = change.get('suggestion', '')

            if original_text and suggestion_text:
                success = self._replace_text_in_document(doc, original_text, suggestion_text)
                if success:
                    applied_count += 1
                else:
                    failed_count += 1

        # 保存修改后的文档
        doc.save(output_file_path)

        return {
            "total_accepted": len(accepted_change_ids),
            "applied_count": applied_count,
            "failed_count": failed_count,
            "output_path": str(output_file_path)
        }

    def _replace_text_in_document(self, doc: Document, old_text: str, new_text: str) -> bool:
        """
        在文档中替换文本

        Args:
            doc: Word文档对象
            old_text: 要替换的文本
            new_text: 新文本

        Returns:
            是否成功替换
        """
        replaced = False

        # 遍历所有段落
        for paragraph in doc.paragraphs:
            if old_text in paragraph.text:
                # 替换段落中的文本
                inline = paragraph.runs
                for run in inline:
                    if old_text in run.text:
                        run.text = run.text.replace(old_text, new_text, 1)  # 只替换第一次出现
                        # 标记修改部分为绿色
                        run.font.color.rgb = RGBColor(0, 128, 0)
                        replaced = True
                        break

                if replaced:
                    break

        # 如果段落中没找到，尝试在表格中查找
        if not replaced:
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if old_text in cell.text:
                            for paragraph in cell.paragraphs:
                                if old_text in paragraph.text:
                                    for run in paragraph.runs:
                                        if old_text in run.text:
                                            run.text = run.text.replace(old_text, new_text, 1)
                                            run.font.color.rgb = RGBColor(0, 128, 0)
                                            replaced = True
                                            break
                                if replaced:
                                    break
                        if replaced:
                            break
                    if replaced:
                        break
                if replaced:
                    break

        return replaced

    def create_comparison_document(
        self,
        original_file_path: Path,
        output_file_path: Path,
        changes: List[Dict[str, Any]]
    ) -> Path:
        """
        创建带有所有修改标记的对比文档（用于预览）

        Args:
            original_file_path: 原始文档路径
            output_file_path: 输出文档路径
            changes: 所有修改建议列表

        Returns:
            输出文档路径
        """
        doc = Document(original_file_path)

        # 标记所有需要修改的地方为红色
        for change in changes:
            original_text = change.get('original', '')
            if original_text:
                self._highlight_text_in_document(doc, original_text, RGBColor(255, 0, 0))

        doc.save(output_file_path)
        return output_file_path

    def _highlight_text_in_document(self, doc: Document, text: str, color: RGBColor) -> bool:
        """
        在文档中高亮文本

        Args:
            doc: Word文档对象
            text: 要高亮的文本
            color: 高亮颜色

        Returns:
            是否成功高亮
        """
        highlighted = False

        for paragraph in doc.paragraphs:
            if text in paragraph.text:
                for run in paragraph.runs:
                    if text in run.text:
                        run.font.color.rgb = color
                        highlighted = True
                        break
                if highlighted:
                    break

        return highlighted


# 创建全局实例
document_modifier = DocumentModifier()
